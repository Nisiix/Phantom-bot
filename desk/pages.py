"""Public read-only copy of the dashboard on GitHub Pages.

Every `interval_minutes` the desk writes a snapshot of what the local dashboard shows (state,
3D activity, Controller report, recap) next to a copy of dashboard.html in `data/pages/`, and
force-pushes that folder as a single commit to the `gh-pages` branch. GitHub Pages then serves
it at https://<owner>.github.io/<repo>/; the page reads the snapshot files instead of the local API.

Built so that it can never disturb the desk:
  * it only reads what the dashboard already computes (no new queries on the trading path);
  * git runs in a worker thread with a timeout, so a slow network or a credential prompt never
    blocks the event loop;
  * every error is caught and reported once in the feed; the trading loop never sees it;
  * the branch always holds exactly one commit (amend + force push), so the repo doesn't grow.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .config import ROOT
from .models import Event

MIN_INTERVAL_MIN = 6.0           # GitHub Pages builds from a branch are soft-limited to ~10/hour
GIT_TIMEOUT_S = 120
GC_EVERY = 50                    # drop the amended commits' old objects every N publishes
MARKER = "<!--PAGES-->"
FILES = {"state": "state.json", "activity": "activity.json", "controller": "controller.txt", "recap": "recap.txt"}


def pages_url(remote: str) -> str:
    """https://github.com/Owner/Repo(.git) or git@github.com:Owner/Repo.git -> https://owner.github.io/Repo/"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", remote.strip())
    if not m:
        return ""
    owner, repo = m.group(1).lower(), m.group(2)
    if repo.lower() == f"{owner}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def static_page(page_html: str, interval_s: float, published: float) -> str:
    """The local dashboard switched to snapshot mode (reads data/*.json, no local API)."""
    cfg = json.dumps({"interval": interval_s, "published": published})
    head = (f'<meta name="robots" content="noindex, nofollow">\n'
            f"<script>window.PHANTOM_STATIC = {cfg};</script>")
    if MARKER not in page_html:
        raise ValueError("dashboard.html has no static-mode marker")
    return page_html.replace(MARKER, head, 1)


def write_site(site: Path, page_html: str, data: dict[str, bytes], interval_s: float) -> None:
    published = time.time()
    (site / "data").mkdir(parents=True, exist_ok=True)
    for key, name in FILES.items():
        (site / "data" / name).write_bytes(data.get(key, b""))
    (site / "index.html").write_text(static_page(page_html, interval_s, published), encoding="utf-8")
    (site / ".nojekyll").write_bytes(b"")      # serve the files as they are, no Jekyll build


def _git(site: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ident = ("-c", "user.name=Phantom Desk", "-c", "user.email=phantom-desk@users.noreply.github.com",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false")
    return subprocess.run(["git", *ident, *args], cwd=site, env=env, capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, encoding="utf-8", errors="replace")


def origin_url(repo: Path = ROOT) -> str:
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def push_site(site: Path, remote: str, branch: str, publishes: int) -> None:
    """Commit the folder as the only commit of `branch` and force-push it. Raises RuntimeError on failure."""
    if not (site / ".git").exists():
        for args in (("init", "-q"), ("symbolic-ref", "HEAD", f"refs/heads/{branch}")):
            r = _git(site, *args)
            if r.returncode:
                raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:200]}")
    _git(site, "add", "-A")
    has_head = _git(site, "rev-parse", "-q", "--verify", "HEAD").returncode == 0
    msg = "Dashboard snapshot " + time.strftime("%Y-%m-%d %H:%M:%S")
    r = _git(site, "commit", "-q", "--allow-empty", *(["--amend"] if has_head else []), "-m", msg)
    if r.returncode:
        raise RuntimeError(f"git commit: {(r.stderr or r.stdout).strip()[:200]}")
    r = _git(site, "push", "-q", "--force", remote, f"HEAD:refs/heads/{branch}")
    if r.returncode:
        raise RuntimeError(f"git push: {r.stderr.strip()[:300]}")
    if publishes and publishes % GC_EVERY == 0:
        _git(site, "reflog", "expire", "--expire=now", "--all")
        _git(site, "gc", "-q", "--prune=now")


class PagesPublisher:
    def __init__(self, desk, dashboard) -> None:
        self.desk = desk
        self.dash = dashboard
        self.cfg = desk.cfg.pages
        self.site = desk.cfg.data_dir / "pages"
        self.interval_s = max(MIN_INTERVAL_MIN, float(self.cfg.interval_minutes)) * 60
        self.publishes = 0
        self.last_error = ""
        self.url = ""

    def _say(self, kind: str, msg: str) -> None:
        self.desk.hub.emit(Event("Logger", kind, msg))

    def snapshot(self) -> dict[str, bytes]:
        d = self.dash
        ctl = self.desk.controller
        report = (ctl.report or "Il Controllore non ha ancora completato un ciclo.") if ctl is not None \
            else "Controllore non attivo."
        try:
            recap = d.recap_text()
        except Exception as e:  # noqa: BLE001
            recap = f"Recap non disponibile ({e.__class__.__name__}).".encode()
        return {"state": d.state_json(),
                "activity": json.dumps(d.activity(), default=str, allow_nan=False).encode(),
                "controller": report.encode(), "recap": recap}

    async def publish_once(self, remote: str) -> None:
        data = self.snapshot()
        page = self.dash_page()
        await asyncio.to_thread(write_site, self.site, page, data, self.interval_s)
        await asyncio.to_thread(push_site, self.site, remote, self.cfg.branch, self.publishes + 1)
        self.publishes += 1

    def dash_page(self) -> str:
        from .web import PAGE
        return PAGE.read_text(encoding="utf-8")

    async def run(self) -> None:
        if self.desk.cfg.mode == "live" and not self.cfg.publish_live:
            self._say("warn", "GitHub Pages: live mode, nothing is published ([pages] publish_live = false)")
            return
        remote = self.cfg.remote or await asyncio.to_thread(origin_url)
        if not remote:
            self._say("warn", "GitHub Pages: no git remote (this folder isn't a git clone; set [pages] remote)")
            return
        self.url = pages_url(remote)
        await asyncio.sleep(20)                 # let the desk warm up first
        while not self.desk.stop.is_set():
            try:
                await self.publish_once(remote)
                if self.publishes == 1 or self.last_error:
                    self._say("info", f"GitHub Pages: dashboard published{' at ' + self.url if self.url else ''}")
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - publishing must never take the desk down
                err = str(e) or e.__class__.__name__
                if isinstance(e, subprocess.TimeoutExpired):
                    err = "git timed out (network or credentials?)"
                if err != self.last_error:
                    self._say("warn", f"GitHub Pages: publish failed, retrying later: {err}")
                self.last_error = err
            try:
                await asyncio.wait_for(self.desk.stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass


def publish_from_running_desk(cfg) -> str:
    """`python run.py pages`: publish once now, reading the running desk's local dashboard (a separate process,
    so the desk itself is untouched). Returns a line for the terminal; raises RuntimeError on failure."""
    import httpx
    if cfg.mode == "live" and not cfg.pages.publish_live:
        raise RuntimeError("live mode: nothing is published ([pages] publish_live = false)")
    remote = cfg.pages.remote or origin_url()
    if not remote:
        raise RuntimeError("no git remote: this folder isn't a git clone (set [pages] remote in config.toml)")
    data: dict[str, bytes] = {}
    for port in range(cfg.dashboard.port, cfg.dashboard.port + 10):
        base = f"http://127.0.0.1:{port}/api/"
        try:
            r = httpx.get(base + "state", timeout=5)
            if r.status_code != 200 or b'"mode"' not in r.content:
                continue
            data["state"] = r.content
            for key in ("activity", "controller", "recap"):
                data[key] = httpx.get(base + key, timeout=30).content
            break
        except httpx.HTTPError:
            continue
    if "state" not in data:
        raise RuntimeError("the desk isn't running (no dashboard on 127.0.0.1:"
                           f"{cfg.dashboard.port}-{cfg.dashboard.port + 9}): start it first")
    from .web import PAGE
    site = cfg.data_dir / "pages"
    interval = max(MIN_INTERVAL_MIN, float(cfg.pages.interval_minutes)) * 60
    write_site(site, PAGE.read_text(encoding="utf-8"), data, interval)
    push_site(site, remote, cfg.pages.branch, 1)
    url = pages_url(remote)
    return (f"Published to branch {cfg.pages.branch}" + (f": {url} (first time: enable Pages in the repo settings,"
            f" source = branch {cfg.pages.branch}, folder /)" if url else "."))
