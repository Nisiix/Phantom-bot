"""DEV WATCH (user's rule 2026-11-11): the creator of a token and the wallets linked to it must not sell more than
[devwatch] max_sold_pct (10%) of what they held from the start.

  * no position open -> the Filter marks the token dangerous and drops it for good (permanent kill "dev_sold")
  * position open    -> Risk sells it at once (exit "DEV_SOLD"), whatever the P&L

Who is watched (the "group"):
  * the creator (pump.fun create event / RugCheck `creator`)
  * the wallets of every RugCheck insider network (token transfer graph) that contains the creator
  * the creator's SOL funder when GMGN knows it

What "from the start" means: the reference is the larger of
  * the creator's own buy in the create transaction (pump.fun `initialBuy`, seen live by the Scout; when the Scout
    didn't see the launch, read from the chain: the first successful transaction of the creator's token account,
    i.e. what the creator held right after it - user's request 2026-11-11), and
  * the largest total the group was ever seen holding.
Tokens moved between wallets of the group don't count as a sale (the total stays the same); tokens sent to a
wallet outside the group do (they left the group: in practice a sale through a fresh wallet).
A group whose reference is under min_hold_pct of the supply (dust) can't be judged: it passes.

Balances are read on-chain in ONE getMultipleAccounts call per check (the associated token account of every
wallet). RugCheck's `creatorBalance` is not used: measured 2026-11-11 it reads 0 for creators that still hold
their whole buy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def ata(owner: str, mint: str, token_program: str = SPL_TOKEN) -> str | None:
    """Associated token account of owner for mint (None if an address is malformed)."""
    try:
        from solders.pubkey import Pubkey
        addr, _ = Pubkey.find_program_address(
            [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program or SPL_TOKEN)),
             bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))
        return str(addr)
    except Exception:  # noqa: BLE001 - bad address from a feed
        return None


def linked_wallets(creator: str | None, graph: list | None, limit: int = 12) -> dict[str, str]:
    """Wallets of the insider networks (RugCheck /insiders/graph) that contain the creator -> role."""
    out: dict[str, str] = {}
    if not creator:
        return out
    for net in graph or []:
        nodes = [n.get("id") for n in (net.get("nodes") or []) if n.get("id")]
        if creator not in nodes:
            continue
        for w in nodes:
            if w != creator and w not in out and len(out) < limit:
                out[w] = f"linked ({net.get('network_type') or 'network'})"
    return out


@dataclass
class Group:
    mint: str
    creator: str = ""
    wallets: dict = field(default_factory=dict)     # wallet -> role ("creator", "linked (transfer)", "dev funder")
    initial: float = 0.0                            # creator's own buy at creation (UI tokens), 0 = unknown
    initial_src: str = ""                           # "launch" (Scout) | "chain" (first tx) | "none" (never held)
    peak_total: float = 0.0                         # largest group total ever seen (UI tokens)
    cur: dict = field(default_factory=dict)         # wallet -> last balance seen (UI tokens)
    supply: float = 0.0                             # UI tokens
    token_program: str = ""                         # SPL Token or Token-2022 (pump.fun uses both): "" = read it
    updated: float = 0.0
    sold_pct: float = 0.0

    def add(self, wallet: str, role: str) -> bool:
        if not wallet or wallet in self.wallets:
            return False
        self.wallets[wallet] = role
        return True

    def update(self, balances: dict[str, float], t: float | None = None) -> float:
        """New balances (UI tokens; a wallet missing from the dict = unchanged). Returns the sold % of the reference."""
        for w, b in balances.items():
            if w in self.wallets:
                self.cur[w] = max(0.0, float(b))
        total = sum(self.cur.get(w, 0.0) for w in self.wallets)
        self.peak_total = max(self.peak_total, total)
        ref = self.reference
        self.sold_pct = max(0.0, (ref - total) / ref * 100) if ref > 0 else 0.0
        self.updated = t if t is not None else time.time()
        return self.sold_pct

    @property
    def total(self) -> float:
        return sum(self.cur.get(w, 0.0) for w in self.wallets)

    @property
    def reference(self) -> float:
        return max(self.initial, self.peak_total)

    def judgeable(self, min_hold_pct: float) -> bool:
        """False when what the group held is dust (under min_hold_pct of the supply)."""
        if self.supply <= 0:
            return self.reference > 0
        return self.reference / self.supply * 100 >= min_hold_pct

    def describe(self) -> str:
        n = len(self.wallets)
        sh = (lambda x: f"{x / self.supply * 100:.2f}%" if self.supply > 0 else f"{x:,.0f}")
        return (f"creator {self.creator[:6]} + {n - 1} linked wallets hold {sh(self.total)} of supply, "
                f"held {sh(self.reference)} from the start ({'creator buy' if self.initial >= self.peak_total else 'peak seen'}): "
                f"sold {self.sold_pct:.1f}%")

    def to_dict(self) -> dict:
        return {"mint": self.mint, "creator": self.creator, "wallets": self.wallets, "initial": self.initial, "initial_src": self.initial_src,
                "peak_total": self.peak_total, "cur": self.cur, "supply": self.supply, "token_program": self.token_program, "updated": self.updated,
                "sold_pct": self.sold_pct}

    @classmethod
    def from_dict(cls, d: dict) -> "Group":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class DevWatch:
    """Groups per mint, their on-chain balances, persisted in the store (survives a restart)."""

    def __init__(self, store, rpc, cfg) -> None:
        self.store, self.rpc, self.c = store, rpc, cfg
        self.groups: dict[str, Group] = {}
        self.initial: dict[str, tuple[str, float]] = {}    # mint -> (creator, initial buy), from the Scout

    # ── creation data from the Scout ──
    def note_create(self, mint: str, creator: str | None, initial_buy: float | None) -> None:
        if not mint or not creator or not initial_buy:
            return
        self.initial[mint] = (creator, float(initial_buy))
        if len(self.initial) > 60000:                      # bounded: ~2 days of pump.fun launches
            for k in list(self.initial)[:20000]:
                self.initial.pop(k, None)

    def group(self, mint: str, creator: str | None = None, supply: float = 0.0,
              token_program: str | None = None) -> Group | None:
        g = self.groups.get(mint)
        if g is None:
            saved = self.store.dev_group(mint) if self.store is not None else None
            g = Group.from_dict(saved) if saved else None
        if g is None:
            init = self.initial.get(mint)
            creator = creator or (init[0] if init else None)
            if not creator:
                return None
            g = Group(mint, creator=creator)
            if init and init[0] == creator:
                g.initial, g.initial_src = init[1], "launch"
        if creator and not g.creator:
            g.creator = creator
        if g.creator:
            g.add(g.creator, "creator")
        if supply > 0:
            g.supply = supply
        if token_program:
            g.token_program = token_program
        self.groups[mint] = g
        return g

    async def refresh(self, g: Group) -> float:
        """Read every wallet's associated token account in one call and update the group."""
        if not g.token_program:
            info = await self.rpc.mint_info(g.mint)
            g.token_program = (info or {}).get("program") or SPL_TOKEN
        if not g.initial_src and g.creator:
            try:
                first = await self.first_holding(g.creator, g.mint, g.token_program)
            except Exception:  # noqa: BLE001 - history unreadable now: retried at the next check
                first = None
            if first is not None:
                g.initial, g.initial_src = max(g.initial, first), "chain" if first > 0 else "none"
        wallets = list(g.wallets)
        accounts = [ata(w, g.mint, g.token_program) for w in wallets]
        pairs = [(w, a) for w, a in zip(wallets, accounts) if a]
        if not pairs:
            return g.sold_pct
        res = await self.rpc.call("getMultipleAccounts",
                                  [[a for _, a in pairs], {"encoding": "jsonParsed", "commitment": "confirmed"}])
        balances = {}
        for (w, _), acc in zip(pairs, (res or {}).get("value") or []):
            if acc is None:
                balances[w] = 0.0                          # no account (never held, or closed after selling)
                continue
            info = ((acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            ta = info.get("tokenAmount") or {}
            balances[w] = float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
        sold = g.update(balances)
        self.save(g)
        return sold

    async def first_holding(self, owner: str, mint: str, token_program: str, max_pages: int = 5) -> float | None:
        """What owner held of mint right after the first successful transaction of its token account (for the
        creator: the create transaction with its own buy). 0.0 = the account has no history (never held);
        None = history too long to reach the start (more than max_pages x 1000 transactions)."""
        acc = ata(owner, mint, token_program)
        if not acc:
            return None
        before, page = None, []
        for _ in range(max_pages):
            opts = {"limit": 1000, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            page = await self.rpc.call("getSignaturesForAddress", [acc, opts]) or []
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        else:
            return None
        oks = [s["signature"] for s in reversed(page) if not s.get("err")]     # oldest first
        if not oks:
            return 0.0
        for sig in oks[:3]:
            tx = None
            for ver in (1, 0):        # v1 transactions exist since 2026; an older RPC only knows version 0
                try:
                    tx = await self.rpc.call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                                      "commitment": "confirmed",
                                                                      "maxSupportedTransactionVersion": ver}])
                    break
                except RuntimeError as e:
                    if ver == 0 or "version" not in str(e).lower():
                        raise
            for b in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
                if b.get("owner") == owner and b.get("mint") == mint:
                    ta = b.get("uiTokenAmount") or {}
                    amt = float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
                    if amt > 0:
                        return amt
        return 0.0

    def save(self, g: Group) -> None:
        if self.store is not None:
            self.store.save_dev_group(g.mint, g.to_dict())

    def breached(self, g: Group) -> bool:
        return g.judgeable(self.c.min_hold_pct) and g.sold_pct > self.c.max_sold_pct
