"""GitHub Pages copy: static page, snapshot files, single-commit force push, never in live by default."""
from __future__ import annotations

import asyncio
import json
import subprocess

from desk.pages import PagesPublisher, pages_url, push_site, static_page, write_site
from desk.web import PAGE
from tests.test_web import make_dash


def test_pages_url():
    assert pages_url("https://github.com/Nisiix/Phantom-bot.git") == "https://nisiix.github.io/Phantom-bot/"
    assert pages_url("git@github.com:Nisiix/Phantom-bot.git") == "https://nisiix.github.io/Phantom-bot/"
    assert pages_url("https://github.com/nisiix/nisiix.github.io") == "https://nisiix.github.io/"
    assert pages_url("https://gitlab.com/a/b.git") == ""


def test_static_page_switches_to_snapshot_files():
    html = PAGE.read_text(encoding="utf-8")
    out = static_page(html, 600, 123.0)
    assert "window.PHANTOM_STATIC" in out and '"interval": 600' in out and "noindex" in out
    assert "<!--PAGES-->" not in out
    # every API call of the page goes through api(), which maps to data/*.json|txt in static mode
    assert 'fetch("/api/' not in html and html.count('fetch(api("') == 4


def test_snapshot_and_push(tmp_path):
    dash = make_dash(tmp_path)
    pub = PagesPublisher(dash.desk, dash)
    data = pub.snapshot()
    assert json.loads(data["state"])["mode"] == "paper" and b'"events"' in data["activity"]
    assert data["controller"] and data["recap"]

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    site = tmp_path / "site"
    for n in (1, 2):
        write_site(site, PAGE.read_text(encoding="utf-8"), data, 600)
        push_site(site, str(bare), "gh-pages", n)
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "gh-pages"], cwd=bare, capture_output=True,
                           text=True, check=True).stdout.split()
    assert set(files) == {".nojekyll", "index.html", "data/state.json", "data/activity.json",
                          "data/controller.txt", "data/recap.txt"}
    count = subprocess.run(["git", "rev-list", "--count", "gh-pages"], cwd=bare, capture_output=True,
                           text=True, check=True).stdout.strip()
    assert count == "1"   # amended + force-pushed: the branch never grows


def test_disabled_by_default_and_never_live(tmp_path):
    dash = make_dash(tmp_path)
    assert dash.desk.cfg.pages.enabled is False and dash.desk.cfg.pages.publish_live is False
    dash.desk.cfg.mode = "live"
    pub = PagesPublisher(dash.desk, dash)
    asyncio.run(asyncio.wait_for(pub.run(), timeout=2))   # returns at once, publishes nothing
    assert pub.publishes == 0 and not (dash.desk.cfg.data_dir / "pages").exists()
