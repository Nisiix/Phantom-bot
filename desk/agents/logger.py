"""LOGGER — records the reason behind every trade and every rejection, and
delivers the full recap every 4 hours (file + Telegram and/or Discord)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from ..longwatch import LongWatch
from ..recap import build_recap, next_recap_time
from ..shadow import ShadowTracker
from .base import Agent


class Logger(Agent):
    name = "Logger"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.dir = self.cfg.data_dir / self.cfg.mode
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.data_dir / "reports").mkdir(parents=True, exist_ok=True)
        self.logfile = (self.dir / "desk.log").open("a", buffering=1, encoding="utf-8")
        self.last_recap_path: str = ""
        self.next_recap: float = 0.0
        self.shadow = ShadowTracker(desk, self.emit)
        desk.shadow = self.shadow
        self.longwatch = LongWatch(desk)
        desk.longwatch = self.longwatch

    async def run(self) -> None:
        self.status("working")
        hours = self.cfg.recap.interval_hours
        last = self.desk.store.kv_get("last_recap_ts", 0.0)
        self.next_recap = next_recap_time(max(time.time(), last), hours)
        self.emit("info", f"Logger online: decisions -> {self.desk.store.path.name}, recap every {hours:g}h "
                          f"(next {datetime.fromtimestamp(self.next_recap, timezone.utc):%H:%M} UTC), "
                          f"telegram={'on' if self._tg() else 'off'} discord={'on' if self._dc() else 'off'}")
        await asyncio.gather(self.supervised("persist", self.persist_loop),
                             self.supervised("recap", self.recap_loop),
                             self.supervised("shadow", self.shadow_loop),
                             self.supervised("longwatch", self.longwatch_loop))

    async def longwatch_loop(self) -> None:
        """Rejected graduated tokens followed for 24 h (desk/longwatch.py): the Controller reports them per rule."""
        if not self.cfg.longwatch.enabled:
            return
        task = asyncio.create_task(self.longwatch.loop())
        while not self.desk.stop.is_set() and not task.done():
            self.state.counters["long_followed"] = len(self.longwatch.rules)
            await asyncio.sleep(5)
        await task

    async def shadow_loop(self) -> None:
        if not self.cfg.quant.shadow_tracking:
            return
        self.emit("info", f"Shadow tracker on: following every evaluated token for "
                          f"{self.cfg.quant.horizon_minutes:g} min (desk +12/-6, model +60/-30)"
                          + (f", resumed {self.shadow.restored} follow-ups from before the restart"
                             if self.shadow.restored else ""))
        task = asyncio.create_task(self.shadow.loop())
        while not self.desk.stop.is_set() and not task.done():
            self.state.counters["shadowing"] = len(self.shadow.active)
            self.state.counters["shadow_done"] = self.shadow.completed
            await asyncio.sleep(2)
        await task

    def _tg(self) -> bool:
        s = self.cfg.secrets
        return bool(s.telegram_bot_token and s.telegram_chat_id)

    def _dc(self) -> bool:
        return bool(self.cfg.secrets.discord_webhook_url)

    async def persist_loop(self) -> None:
        q = self.hub.log_queue
        store = self.desk.store
        while not self.desk.stop.is_set() or not q.empty():
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            self.state.bump("logged")
            # Shallow market rejections are already in the decisions table; keep the event log lean.
            if not (ev.kind == "reject" and not ev.data.get("deep", True)):
                store.add_event(ev.ts, ev.agent, ev.kind, ev.mint, ev.symbol, ev.message, ev.data)
            if ev.kind != "reject" or ev.data.get("deep"):
                stamp = datetime.fromtimestamp(ev.ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                self.logfile.write(f"{stamp} [{ev.agent:<8}] {ev.kind.upper():<6} "
                                   f"{(ev.symbol or '')[:12]:<12} {ev.message}\n")
            if ev.kind == "trade":
                with (self.dir / "trades.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": ev.ts, "symbol": ev.symbol, "mint": ev.mint,
                                         "message": ev.message, **ev.data}, default=str) + "\n")

    async def recap_loop(self) -> None:
        while not self.desk.stop.is_set():
            wait = self.next_recap - time.time()
            if wait > 0:
                if not await self.sleep(min(wait, 30)):
                    return
                continue
            await self.send_recap()

    async def send_recap(self, t1: float | None = None) -> str:
        t1 = t1 or time.time()
        hours = self.cfg.recap.interval_hours
        t0 = self.desk.store.kv_get("last_recap_ts", 0.0) or (t1 - hours * 3600)
        text = build_recap(self.desk.store, self.desk.portfolio, self.cfg.mode, t0, t1, self.hub.agents,
                           self.desk.quant)
        stamp = datetime.fromtimestamp(t1, timezone.utc).strftime("%Y%m%d_%H%M")
        path = self.cfg.data_dir / "reports" / f"recap_{self.cfg.mode}_{stamp}.md"
        path.write_text(text, encoding="utf-8")
        self.last_recap_path = str(path)
        self.desk.store.kv_set("last_recap_ts", t1)
        self.next_recap = next_recap_time(t1 + 1, hours)
        delivered = await self.deliver(text)
        self.state.bump("recaps")
        self.emit("recap", f"{hours:g}h recap written to {path.name}"
                           + (f", sent via {', '.join(delivered)}" if delivered else ""), path=str(path))
        return text

    async def deliver(self, text: str) -> list[str]:
        s = self.cfg.secrets
        sent = []
        async with httpx.AsyncClient(timeout=20) as c:
            if self._tg():
                try:
                    for chunk in _chunks(text, 3900):
                        r = await c.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                                         json={"chat_id": s.telegram_chat_id, "text": chunk,
                                               "disable_web_page_preview": True})
                        r.raise_for_status()
                    sent.append("Telegram")
                except Exception as e:  # noqa: BLE001
                    self.emit("error", f"Telegram delivery failed: {e.__class__.__name__}")
            if self._dc():
                try:
                    for chunk in _chunks(text, 1900):
                        r = await c.post(s.discord_webhook_url, json={"content": chunk})
                        r.raise_for_status()
                        await asyncio.sleep(0.6)
                    sent.append("Discord")
                except Exception as e:  # noqa: BLE001
                    self.emit("error", f"Discord delivery failed: {e.__class__.__name__}")
        return sent


def _chunks(text: str, n: int) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > n:
            out.append(cur)
            cur = ""
        cur += line[:n]
    if cur:
        out.append(cur)
    return out
