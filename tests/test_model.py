"""MODELLO: training on shadow rows, time-split verification, the gate only blocks once verified."""
from __future__ import annotations

import json
import random
import time

from desk.config import Config
from desk.model import ModelGate, report_section, train_round


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict = {}

    def kv_get(self, k, default=None):
        return self.kv.get(k, default)

    def kv_set(self, k, v) -> None:
        self.kv[k] = json.loads(json.dumps(v))      # same round trip as the real kv table


def _rows(n: int, signal: bool, seed: int = 1) -> list[dict]:
    """Synthetic analyst follow-ups. With signal: high buy share wins, low buy share dumps."""
    rng = random.Random(seed)
    t0 = time.time() - n * 60
    out = []
    for i in range(n):
        br = rng.random()
        f = {"buy_ratio_m5": br, "liq_usd": rng.uniform(1e4, 2e5), "score": rng.uniform(0.6, 0.9),
             "chg_h1": rng.gauss(0, 30), "mkt_meme_h1": rng.gauss(0, 2), "top10_pct": None}
        if signal:
            win = rng.random() < (0.8 if br > 0.5 else 0.15)
        else:
            win = rng.random() < 0.4
        ret = rng.uniform(5, 30) if win else rng.uniform(-12, -4)
        out.append({"start_ts": t0 + i * 60, "mint": f"m{i}pump", "score": f["score"], "decision": "approve",
                    "sim_net_pct": ret, "cost_pct": 2.0, "features": json.dumps(f), "path": None})
    return out


def _cfg(**model) -> Config:
    cfg = Config()
    cfg.model.min_rows = 400
    for k, v in model.items():
        setattr(cfg.model, k, v)
    return cfg


def test_waits_for_data():
    cfg = _cfg()
    res = train_round(_rows(100, True), cfg)
    assert res["verdict"] == "dati insufficienti" and "model" not in res
    gate = ModelGate(FakeStore(), cfg)
    assert gate.update(res, True) is None and not gate.active and gate.status() == "in attesa di dati"
    assert gate.evaluate({"buy_ratio_m5": 0.1}) is None
    assert "In attesa di dati" in report_section(res, gate, cfg)


def test_learns_a_real_signal_and_blocks_only_when_verified():
    cfg = _cfg()
    res = train_round(_rows(1500, True), cfg)
    assert res["auc_valid"] > 0.75
    assert res["verdict"] == "solida"
    assert res["importance"][0][0] == "buy_ratio_m5"
    store = FakeStore()
    gate = ModelGate(store, cfg)
    assert gate.update(res, may_apply=True).startswith("Modello ATTIVO")
    bad, good = gate.evaluate({"buy_ratio_m5": 0.1}), gate.evaluate({"buy_ratio_m5": 0.9})
    assert bad.block and not good.block and good.p > bad.p
    assert "MODEL: P(win)" in bad.reason
    # survives a restart
    again = ModelGate(store, cfg)
    assert again.active and again.evaluate({"buy_ratio_m5": 0.1}).block
    # no advantage any more -> the block switches itself off
    assert "in prova" in again.update({"verdict": "nessun vantaggio"}, True)
    assert not again.evaluate({"buy_ratio_m5": 0.1}).block


def test_never_blocks_when_not_allowed():
    cfg = _cfg()
    res = train_round(_rows(1500, True), cfg)
    gate = ModelGate(FakeStore(), cfg)
    gate.update(res, may_apply=False)                 # auto_apply off for this mode: log only
    v = gate.evaluate({"buy_ratio_m5": 0.1})
    assert v is not None and not v.block and "in prova" in v.short()
    cfg2 = _cfg(enforce=False)
    gate2 = ModelGate(FakeStore(), cfg2)
    gate2.update(train_round(_rows(1500, True), cfg2), may_apply=True)
    assert not gate2.active


def test_promettente_needs_confirm_rounds():
    cfg = _cfg()
    gate = ModelGate(FakeStore(), cfg)
    res = train_round(_rows(1500, True), cfg)
    res = {**res, "verdict": "promettente"}
    for _ in range(cfg.controller.confirm_rounds - 1):
        gate.update(res, True)
        assert not gate.active
    gate.update(res, True)
    assert gate.active


def test_noise_is_not_solid():
    cfg = _cfg()
    solid = sum(train_round(_rows(1500, False, seed=s), cfg)["verdict"] == "solida" for s in range(5))
    assert solid <= 1


def _desk_with_gate(tmp_path, active: bool):
    from tests.test_rules import make_desk
    desk = make_desk(tmp_path)
    cfg = desk.cfg
    cfg.model.min_rows = 400
    gate = ModelGate(desk.store, cfg)
    gate.update(train_round(_rows(1500, True), cfg), may_apply=active)
    desk.model = gate
    return desk


def _buy(desk, buy_ratio: float):
    import asyncio

    from desk.agents.executor import Executor
    from desk.agents.risk import Risk
    from desk.brokers.paper import PaperBroker
    from tests.test_rules import signed_verdict, snap

    class FakeJup:
        async def quote(self, i, o, amount, bps):
            if i.startswith("So111"):
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
            return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.features = {"buy_ratio_m5": buy_ratio}
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    return ex


def test_executor_blocks_low_p_win_when_active(tmp_path):
    desk = _desk_with_gate(tmp_path, active=True)
    ex = _buy(desk, 0.1)
    assert "M" not in desk.portfolio.positions and ex.state.counters.get("model_block") == 1


def test_executor_buys_and_logs_p_win_when_in_prova(tmp_path):
    desk = _desk_with_gate(tmp_path, active=False)
    _buy(desk, 0.1)
    pos = desk.portfolio.positions.get("M")
    assert pos is not None and "MODEL P(win)" in pos.entry_reason and "in prova" in pos.entry_reason


def test_controller_round_trains_the_model(tmp_path):
    import asyncio

    from desk.agents.controller import Controller
    from desk.controller import health_checks
    from tests.test_rules import make_desk
    desk = make_desk(tmp_path)
    desk.cfg.model.min_rows = 400
    desk.model = ModelGate(desk.store, desk.cfg)
    for r in _rows(1500, True):
        desk.store.add_shadow({"start_ts": r["start_ts"], "end_ts": r["start_ts"] + 3600, "mode": "paper",
                               "mint": r["mint"], "symbol": "T", "stage": "analyst", "decision": "approve",
                               "reason": "", "score": r["score"], "traded": 0, "ref_price": 1.0, "ref_liq": 1.0,
                               "cost_pct": 2.0, "desk_label": "up", "sim_net_pct": r["sim_net_pct"],
                               "features": r["features"], "path": None})
    ctl = Controller(desk)
    ctl.health = health_checks(ctl.collect())
    ctl.exp = asyncio.run(asyncio.to_thread(ctl._round))
    assert ctl.model_new["verdict"] == "solida"
    ctl.model_res = ctl.model_new
    desk.model.update(ctl.model_new, True)
    text = ctl.write_report()
    assert "Modello statistico" in text and "SOLIDA" in text and "attivo" in text
    assert ctl.summary()["model"]["status"] == "attivo"
