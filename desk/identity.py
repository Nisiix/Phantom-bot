"""Real token or copycat? Identity check for a token whose name matches a rugged one.

Seen in paper (2026-11-10/11): fake tokens are launched with the name of a coming project before its official
launch (or right after it), they rug, and the rugged-name ledger then blocked the REAL token too: DACITY
"GTAVI AudaCity" (+260%, $107k liquidity) was killed as a relaunch of the rugged AUDCTY "AudaCity", the real
"Leonida" as a relaunch of LEONIDA. A name alone can't tell them apart; the project's identity can:

  * contract / creator: the same dev wallet as the rugged token = a real relaunch -> killed for good;
  * own channels: an own X account (x.com/<handle>, not a link to somebody else's post), an own website
    (not a link shortener, a news article or a pump.fun page), a Telegram group;
  * no borrowed identity: none of those channels may belong to the rugged token (copycats link the real
    project's X account to look official);
  * proof of ownership: the website shows THIS token's contract address (a copycat can link the real site,
    but the real site shows the real CA), the site links the same X account, the team paid a DexScreener profile.

Evidence points (default need 3): CA on the website +2, DexScreener profile +1, own X account +1, website
links that X account +1, Telegram group/channel that exists with >= identity_tg_min_members +1; a Telegram link
that leads nowhere -1 (read from the public t.me preview page, no Telegram account or API key needed). Of the 34 name-blocked pump.fun tokens with a DexScreener profile up to 2026-11-11,
9 were alive the day after (26%); of the 3,108 without one, 7 (0.2%).
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from .website import SOCIAL_HOSTS, _Extract, _norm_handle

# Links that never identify a project: shorteners, content/news sites, launchpads, explorers.
NOT_OWN_SITE = ("tinyurl.com", "bit.ly", "t.co", "linktr.ee", "knowyourmeme.com", "wikipedia.org", "reddit.com",
                "youtube.com", "youtu.be", "twitch.tv", "instagram.com", "tiktok.com", "facebook.com",
                "pump.fun", "dexscreener.com", "solscan.io", "birdeye.so", "gmgn.ai", "github.com", "medium.com",
                "google.com", "apple.com", "otcdesks.cash", "x.com", "twitter.com", "t.me")
# X paths that are not an account: a post, a community, a search...
X_NOT_HANDLE = ("i", "home", "search", "intent", "hashtag", "share", "explore", "messages", "notifications")


def _url(u: str) -> str:
    u = (u or "").strip()
    return u if u.startswith("http") else "https://" + u


def x_link(u: str) -> tuple[str, str] | None:
    """('account'|'post'|'community', handle) for an X/Twitter link, None otherwise."""
    p = urlparse(_url(u))
    host = (p.hostname or "").lower().replace("www.", "").replace("mobile.", "")
    if host not in ("x.com", "twitter.com"):
        return None
    parts = [s for s in p.path.split("/") if s]
    if not parts:
        return None
    if parts[0].lower() == "i":
        return ("community", "/".join(parts[1:3]).lower()) if len(parts) > 1 else None
    if parts[0].lower() in X_NOT_HANDLE:
        return None
    handle = parts[0].lstrip("@").lower()
    if len(parts) > 1 and parts[1].lower() == "status":
        return ("post", handle)
    return ("account", handle)


def own_site(u: str) -> str | None:
    """The host of a link that can be a project's own website (root or one level deep), else None."""
    p = urlparse(_url(u))
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if any(host == h or host.endswith("." + h) for h in NOT_OWN_SITE + SOCIAL_HOSTS):
        return None
    if len([s for s in p.path.split("/") if s]) > 1:    # /de/aktuelles/news/1527/... = an article, not a homepage
        return None
    return host


def tg_link(u: str) -> str | None:
    p = urlparse(_url(u))
    host = (p.hostname or "").lower()
    if host not in ("t.me", "telegram.me", "www.t.me"):
        return None
    parts = [s for s in p.path.split("/") if s]
    if not parts or parts[0].startswith("+") or parts[0].lower() == "joinchat":
        return "tg-invite:" + "/".join(parts) if parts else None     # invite hashes are case-sensitive
    return parts[0].lower()


def channels(websites: list[str], socials: dict[str, str], meta: dict | None = None) -> dict:
    """The token's own channels from DexScreener info (paid profile) and its metadata JSON."""
    out: dict = {"x": None, "x_kind": None, "site": None, "site_url": None, "tg": None, "tg_url": None,
                 "profile": bool(websites or socials)}
    cand_x = [v for v in (socials or {}).values() if v] + [(meta or {}).get("twitter") or ""]
    for u in cand_x:
        x = x_link(u)
        if x and (out["x"] is None or (out["x_kind"] != "account" and x[0] == "account")):
            out["x_kind"], out["x"] = x
    for u in list(websites or []) + [(meta or {}).get("website") or ""]:
        if u and own_site(u):
            out["site"], out["site_url"] = own_site(u), _url(u)
            break
    for u in [v for v in (socials or {}).values() if v] + [(meta or {}).get("telegram") or ""]:
        t = tg_link(u) if u else None
        if t:
            out["tg"] = t
            out["tg_url"] = "https://t.me/" + (t.split(":", 1)[1] if t.startswith("tg-invite:") else t)
            break
    return out


def link_keys(ch: dict) -> list[str]:
    """Identity keys stored per mint, to see which mints claim the same channel."""
    keys = []
    if ch.get("x") and ch.get("x_kind") == "account":
        keys.append("x:" + ch["x"])
    if ch.get("site"):
        keys.append("site:" + ch["site"])
    if ch.get("tg") and not str(ch["tg"]).startswith("tg-invite:"):
        keys.append("tg:" + ch["tg"])
    return keys


def parse_tg_page(html: str) -> dict:
    """t.me preview -> {"exists", "kind": group|channel|account, "members", "title"}."""
    import html as _h
    import re
    title = re.search(r'class="tgme_page_title"[^>]*>\s*(?:<span[^>]*>)?([^<]*)', html)
    extra = re.search(r'class="tgme_page_extra"[^>]*>([^<]*)', html)
    if not title or not title.group(1).strip():
        return {"exists": False, "kind": None, "members": None, "title": None}
    ex = _h.unescape(extra.group(1)).strip().lower() if extra else ""
    m = re.search(r"(\d[\d\s,.\u00a0\u202f']*)\s*(members|subscribers)", ex)
    members = int(re.sub(r"\D", "", m.group(1))) if m else None
    kind = "group" if "member" in ex else "channel" if "subscriber" in ex else "account"
    return {"exists": True, "kind": kind, "members": members, "title": _h.unescape(title.group(1)).strip()[:80]}


class SiteProbe:
    """Fetches a project's website once per (host, mint) and tells whether it shows the mint and which socials
    it links. Cached for 6 h, failures for 10 min."""

    def __init__(self, client=None) -> None:
        self._client = client
        self.cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                         "Accept": "text/html,application/xhtml+xml"})
        return self._client

    async def metadata(self, uri: str | None) -> dict:
        if not uri:
            return {}
        key = ("meta", uri)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        out: dict = {}
        try:
            r = await self.client().get(uri, headers={"Accept": "application/json"})
            if r.status_code == 200 and len(r.content) < 200_000:
                d = r.json()
                ext = d.get("extensions") or {}
                out = {k: d.get(k) or ext.get(k) for k in ("website", "twitter", "telegram")
                       if d.get(k) or ext.get(k)}
        except Exception:  # noqa: BLE001 - metadata is optional evidence
            out = {}
        self.cache[key] = (time.time(), out)
        return out

    async def telegram(self, url: str) -> dict:
        """Does the linked Telegram exist and is it reachable? Read from the public t.me preview page:
        a group/channel shows its title and "N members" / "N subscribers"; a dead link shows no title."""
        key = ("tg", url)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < (3600 if hit[1].get("ok") else 600):
            return hit[1]
        res: dict = {"ok": False, "exists": False, "kind": None, "members": None}
        try:
            r = await self.client().get(url)
            if r.status_code == 200:
                res = {"ok": True, **parse_tg_page(r.text[:300_000])}
            else:
                res["error"] = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            res["error"] = e.__class__.__name__
        self.cache[key] = (time.time(), res)
        return res

    async def site(self, url: str, mint: str) -> dict:
        host = own_site(url) or ""
        key = (host, mint)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < (6 * 3600 if hit[1].get("ok") else 600):
            return hit[1]
        res: dict = {"ok": False, "ca": False, "x": set()}
        try:
            r = await self.client().get(url)
            if r.status_code == 200:
                html = r.text[:1_500_000]
                p = _Extract()
                try:
                    p.feed(html)
                except Exception:  # noqa: BLE001
                    pass
                xs = set()
                for h in p.links:
                    x = x_link(h) if isinstance(h, str) else None
                    if x and x[0] == "account":
                        xs.add(x[1])
                res = {"ok": True, "ca": mint.lower() in html.lower(), "x": xs}
            else:
                res["error"] = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            res["error"] = e.__class__.__name__
        self.cache[key] = (time.time(), res)
        if len(self.cache) > 5000:
            self.cache.clear()
        return res


async def verify(store, probe: SiteProbe, mint: str, creator: str | None, hit: dict, websites: list[str],
                 socials: dict[str, str], metadata_uri: str | None, min_score: float,
                 min_tg_members: int = 20) -> dict:
    """Is `mint` the rugged token's relaunch (or a copycat of it), or a different, real project?

    Returns {"ok", "permanent", "score", "detail", "channels"}."""
    rug_name = hit.get("symbol") or hit.get("key")
    rug_mint = str(hit.get("mint") or "")
    tag = f"same name as rugged {rug_name} ({rug_mint[:6]}…)"
    rug_creator = hit.get("creator")
    if creator and rug_creator and creator == rug_creator:
        return {"ok": False, "permanent": True, "score": 0, "channels": {},
                "detail": f"{tag}, same dev wallet {creator[:6]}: relaunch"}
    meta = await probe.metadata(metadata_uri) if metadata_uri else {}
    ch = channels(websites, socials, meta)
    keys = link_keys(ch)
    store.add_token_links(mint, keys)
    if not keys:
        return {"ok": False, "permanent": False, "score": 0, "channels": ch,
                "detail": f"{tag}; no own website / X account / Telegram to tell it apart"}
    rugged_mints = store.rugged_mints()
    site = await probe.site(ch["site_url"], mint) if ch.get("site_url") else {}
    shared = []
    for k in keys:
        others = [m for m in store.link_mints(k) if m != mint]
        if any(m in rugged_mints or m == rug_mint for m in others):
            if site.get("ca") and (k.startswith("site:") or (k.startswith("x:") and k[2:] in site.get("x", ()))):
                continue    # the copycat borrowed the real channels: the site shows OUR contract and links that X
            shared.append(k)
    if shared:
        return {"ok": False, "permanent": False, "score": 0, "channels": ch,
                "detail": f"{tag}; uses the channels of a rugged token ({', '.join(shared)})"}
    score, why = 0.0, []
    if site.get("ca"):
        score += 2
        why.append(f"contract address on {ch['site']}")
    elif ch.get("site"):
        why.append(f"{ch['site']} " + ("does not show the contract" if site.get("ok")
                                       else f"unreachable ({site.get('error', '?')})"))
    if ch.get("profile"):
        score += 1
        why.append("DexScreener profile")
    if ch.get("x_kind") == "account":
        crowd = len([m for m in store.link_mints("x:" + ch["x"]) if m != mint])
        if crowd >= 2 and not site.get("ca"):
            # A famous account (or the real project's, linked by its copycats): not proof of anything.
            why.append(f"X @{ch['x']} also linked by {crowd} other tokens")
        else:
            score += 1
            why.append(f"own X @{ch['x']}")
            if ch["x"] in (site.get("x") or set()):
                score += 1
                why.append("site links that X account")
    elif ch.get("x"):
        why.append(f"X link is a {ch['x_kind']}, not an account")
    tg = await probe.telegram(ch["tg_url"]) if ch.get("tg_url") else {}
    if tg.get("ok") and not tg.get("exists"):
        score -= 1          # a Telegram link that leads nowhere: decoration, not a community
        why.append(f"Telegram {ch['tg']} does not exist")
    elif tg.get("exists") and tg.get("kind") in ("group", "channel"):
        n = tg.get("members") or 0
        unit = "members" if tg["kind"] == "group" else "subscribers"
        if n >= min_tg_members:
            score += 1
            why.append(f"Telegram {tg['kind']} reachable, {n} {unit}")
        else:
            why.append(f"Telegram {tg['kind']} reachable but only {n} {unit}")
    elif tg.get("exists"):
        why.append("Telegram link is a personal account / bot, not a community")
    elif ch.get("tg"):
        why.append(f"Telegram not checked ({tg.get('error', '?')})")
    if creator and rug_creator:
        why.append(f"other dev ({creator[:6]})")
    ok = score >= min_score
    return {"ok": ok, "permanent": False, "score": score, "channels": {**ch, "site_ca": bool(site.get("ca")), "telegram": tg},
            "detail": f"{tag}; {'DIFFERENT PROJECT' if ok else 'not proven different'}: "
                      f"{', '.join(why) or 'no evidence'} (identity {score:.0f}/{min_score:.0f})"}
