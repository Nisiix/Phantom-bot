"""Execution speed and robustness: parallel pre-checks, immediate exit retries, parallel exits,
per-transaction fills (Jupiter V2 result / tx metadata), signing of multi-signer (gasless/RFQ) orders."""
from __future__ import annotations

import asyncio
import base64

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.null_signer import NullSigner
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from desk.agents.executor import Executor
from desk.agents.risk import Risk
from desk.brokers.base import Fill
from desk.brokers.live import LiveBroker, order_fees_sol, sign_swap
from desk.brokers.paper import PaperBroker
from desk.clients.solana_rpc import SolanaRPC, parse_fill
from desk.config import Config, enforce_hard_limits
from desk.models import ExitOrder
from tests.test_rules import make_desk, position, signed_verdict, snap


def unsigned_tx(signers: list[Keypair], fee_payer: Keypair) -> str:
    ix = Instruction(Pubkey.default(), b"", [AccountMeta(k.pubkey(), True, True) for k in signers])
    msg = MessageV0.try_compile(fee_payer.pubkey(), [ix], [], Hash.default())
    keys = msg.account_keys[:msg.header.num_required_signatures]
    return base64.b64encode(bytes(VersionedTransaction(msg, [NullSigner(k) for k in keys]))).decode()


def test_sign_swap_fills_only_our_slot_on_gasless_orders():
    me, payer = Keypair(), Keypair()
    for fee_payer in (me, payer):
        out = VersionedTransaction.from_bytes(base64.b64decode(sign_swap(unsigned_tx([me], fee_payer), me)))
        i = list(out.message.account_keys).index(me.pubkey())
        assert out.signatures[i].verify(me.pubkey(), to_bytes_versioned(out.message))


def test_order_fees_count_only_what_we_pay():
    order = {"signatureFeeLamports": 10_000, "signatureFeePayer": "MM", "prioritizationFeeLamports": 5_000,
             "prioritizationFeePayer": "ME", "rentFeeLamports": 2_000_000, "rentFeePayer": "ME"}
    assert order_fees_sol(order, "ME") == 2_005_000 / 1e9


def test_fill_read_from_transaction_metadata():
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "ME"}, {"pubkey": "POOL"}]}},
          "meta": {"preBalances": [2_000_000_000, 0], "postBalances": [1_499_000_000, 0],
                   "preTokenBalances": [],
                   "postTokenBalances": [{"owner": "ME", "mint": "M", "uiTokenAmount": {"uiAmountString": "1234.5"}},
                                         {"owner": "POOL", "mint": "M", "uiTokenAmount": {"uiAmountString": "9"}}]}}
    sol, tok = parse_fill(tx, "ME", "M")
    assert sol == -0.501 and tok == 1234.5


def test_live_v2_buy_uses_jupiter_result_not_wallet_deltas():
    me = Keypair()

    class FakeJup:
        async def execute(self, signed, request_id):
            assert request_id == "req-1"
            VersionedTransaction.from_bytes(base64.b64decode(signed))      # a valid signed tx was sent
            return {"status": "Success", "signature": "SIG", "inputAmountResult": "500000000",
                    "outputAmountResult": "1500000000"}

    class NoRPC:                                                          # must not be touched
        def __getattr__(self, name):
            raise AssertionError(f"RPC {name} called")

    b = LiveBroker(NoRPC(), FakeJup(), str(me), 200_000, 0.05)
    order = {"transaction": unsigned_tx([me], me), "requestId": "req-1", "inAmount": "500000000",
             "outAmount": "1500000000", "mode": "ultra", "router": "jupiterz",
             "rentFeeLamports": 2_000_000, "rentFeePayer": b.pubkey}
    fill = asyncio.run(b.buy("M", 0.5, 6, order))
    assert fill.ok and fill.tx == "SIG" and fill.tokens == 1500.0
    assert abs(fill.sol - 0.502) < 1e-12 and fill.route == "jupiter-v2 ultra/jupiterz"


def test_live_v2_failed_execute_is_reported():
    me = Keypair()

    class FakeJup:
        async def execute(self, signed, request_id):
            return {"status": "Failed", "code": -1005, "error": "Transaction expired", "signature": "S"}

    b = LiveBroker(None, FakeJup(), str(me), 200_000, 0.05)
    fill = asyncio.run(b.sell("M", 10, 6, {"transaction": unsigned_tx([me], me), "requestId": "r"}))
    assert not fill.ok and "-1005" in fill.error and "expired" in fill.error


def test_rpc_path_rebroadcasts_until_landed():
    calls = []

    class FakeHttp:
        async def post(self, url, json=None, retries=0, **kw):
            m = json["method"]
            calls.append(m)
            if m == "sendTransaction":
                assert json["params"][1]["skipPreflight"] is True and json["params"][1]["maxRetries"] == 0
                return {"result": "SIG"}
            if m == "getBlockHeight":
                return {"result": 100}
            if m == "getSignatureStatuses":
                landed = calls.count("getSignatureStatuses") >= 4
                return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None} if landed else None]}}
            raise AssertionError(m)

    ok, sig, _ = asyncio.run(SolanaRPC(FakeHttp(), "x").send_and_confirm("TX", 200, poll=0.01, rebroadcast=0.015))
    assert ok and sig == "SIG" and calls.count("sendTransaction") >= 2


def test_entry_fetches_quote_decimals_and_market_in_parallel(tmp_path):
    desk = make_desk(tmp_path)
    started = set()
    both = asyncio.Event()

    async def mark(name):
        started.add(name)
        if {"quote", "snapshot"} <= started:
            both.set()
        await asyncio.wait_for(both.wait(), 1)       # deadlocks (-> timeout) if run one after the other

    class FakeJup:
        async def quote(self, i, o, amount, bps, lane=""):
            if i.startswith("So111"):
                await mark("quote")
                return {"inAmount": str(amount), "outAmount": str(int(amount / 1e9 / 0.00001 * 1e6))}
            return {"outAmount": str(int(int(amount) * 0.00001 / 1e6 * 1e9 * 0.98))}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            await mark("snapshot")
            return snap(mint=mint)

    desk.jup, desk.dex = FakeJup(), FakeDex()
    ex = Executor(desk, PaperBroker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert "M" in desk.portfolio.positions
    assert desk.exec_stats[-1]["side"] == "buy" and "exec paper" in desk.portfolio.positions["M"].entry_reason


def test_entry_rejects_when_jupiter_slippage_estimate_is_above_cap(tmp_path):
    desk = make_desk(tmp_path)

    class Broker(PaperBroker):
        async def quote_buy(self, jup, mint, lamports, bps):
            return {"inAmount": str(lamports), "outAmount": str(int(lamports / 1e9 / 0.00001 * 1e6)),
                    "slippageBps": 900}

    class FakeDex:
        async def snapshot(self, mint, fresh=True):
            return snap(mint=mint)

    desk.dex = FakeDex()
    ex = Executor(desk, Broker(desk.portfolio), Risk(desk))
    v = signed_verdict(desk)
    v.clearance.report.rug["decimals"] = 6
    asyncio.run(ex.handle_verdict(v))
    assert "M" not in desk.portfolio.positions and ex.state.counters.get("declined") == 1


def test_failed_sell_is_retried_immediately_at_wider_slippage(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    asked = []

    class Broker(PaperBroker):
        async def quote_sell(self, jup, mint, raw, bps):
            asked.append(bps)
            return {"outAmount": str(9 * 10**8)}

        async def sell(self, mint, tokens, decimals, quote):
            if len(asked) == 1:
                return Fill(False, error="slippage exceeded")
            return await super().sell(mint, tokens, decimals, quote)

    desk.portfolio.open(position())
    ex = Executor(desk, Broker(desk.portfolio), risk)
    risk.exiting["M"] = 0
    asyncio.run(ex.handle_exit(ExitOrder("M", "HARD_STOP", "test")))
    assert asked == [None, 800] and "M" not in desk.portfolio.positions
    assert "M" not in risk.exiting and desk.exec_stats[-1]["attempts"] == 2


def test_exit_gives_back_to_risk_after_whole_ladder_fails(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)

    class Broker(PaperBroker):
        async def quote_sell(self, jup, mint, raw, bps):
            return {"outAmount": "1"}

        async def sell(self, mint, tokens, decimals, quote):
            return Fill(False, error="no")

    desk.portfolio.open(position())
    risk.exiting["M"] = 0
    asyncio.run(Executor(desk, Broker(desk.portfolio), risk).handle_exit(ExitOrder("M", "HARD_STOP")))
    assert "M" in desk.portfolio.positions and "M" not in risk.exiting


def test_live_retry_books_a_sale_that_landed_on_an_earlier_attempt(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)

    class Broker(PaperBroker):
        mode = "live"

        async def cash(self):
            return 5.0

        async def quote_sell(self, jup, mint, raw, bps):
            return {"outAmount": str(9 * 10**8), "otherAmountThreshold": str(8 * 10**8)}

        async def sell(self, mint, tokens, decimals, quote):
            return Fill(False, error="response lost")          # ...but it actually landed

        async def token_holdings(self, mint):
            return 0.0

    desk.portfolio.open(position())
    asyncio.run(Executor(desk, Broker(desk.portfolio), risk).handle_exit(ExitOrder("M", "HARD_STOP")))
    trades = desk.store.trades_between(0, 2e9)
    assert "M" not in desk.portfolio.positions and abs(trades[-1]["proceeds_sol"] - 0.8) < 1e-9


def test_exits_run_in_parallel(tmp_path):
    desk = make_desk(tmp_path)
    risk = Risk(desk)
    inside, gate = set(), asyncio.Event()

    class Broker(PaperBroker):
        async def quote_sell(self, jup, mint, raw, bps):
            return {"outAmount": str(10**9)}

        async def sell(self, mint, tokens, decimals, quote):
            inside.add(mint)
            if len(inside) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)   # times out if the second sell waits for the first
            return await super().sell(mint, tokens, decimals, quote)

    async def go():
        for m in ("A", "B"):
            p = position()
            p.mint = m
            desk.portfolio.open(p)
            await desk.exits.put(ExitOrder(m, "HARD_STOP"))
        ex = Executor(desk, Broker(desk.portfolio), risk)
        task = asyncio.create_task(ex.exit_loop())
        for _ in range(200):
            if not desk.portfolio.positions:
                break
            await asyncio.sleep(0.01)
        desk.stop.set()
        await asyncio.wait_for(task, 3)

    asyncio.run(go())
    assert not desk.portfolio.positions and inside == {"A", "B"}


def test_execution_mode_is_validated():
    cfg = Config()
    cfg.executor.execution = "yolo"
    cfg.executor.exit_slippage_ladder_bps = [99_999]
    enforce_hard_limits(cfg)
    assert cfg.executor.execution == "jupiter_v2"
    assert cfg.executor.exit_slippage_ladder_bps == [800, 1500, 3000]
