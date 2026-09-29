import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from logging import getLogger

from qate.core.model import ExchangeName, Side
from qate.core.symbol import Symbol

LOG = getLogger(__name__)

_MIN_ALLOCATION_JPY = 5_000.0


@dataclass
class Allocation:
    quote: float
    base: float


class RebalanceCase(Enum):
    OK = "a"  # all exchanges meet target, start trading
    SELL_BASE = "b"  # exchange(s) are XRP-heavy / JPY-light, sell XRP first
    BUY_BASE = "c"  # exchange(s) are JPY-heavy / XRP-light, buy XRP first
    INSUFFICIENT = "d"  # total value on some exchange < allocated_value_jpy, cannot reach target


@dataclass
class RebalanceStep:
    exchange: ExchangeName
    symbol: Symbol
    side: Side
    amount: float  # in base units


@dataclass
class AllocationPlan:
    allocations: dict[ExchangeName, Allocation]
    case: RebalanceCase
    steps: list[RebalanceStep] = field(default_factory=list)


def plan_allocation(
    symbol: Symbol,
    balances: dict[ExchangeName, dict],  # {exchange: {"base": float, "quote": float}}
    prices: dict[ExchangeName, float],
    allocated_value_jpy: float,
    min_jpy_to_keep: dict[str, float] | None = None,
    order_size: float = 0.0,
) -> AllocationPlan:
    # Algorithm:
    # Inputs: XRP(a), JPY(a), XRP(b), JPY(b), price(a), price(b), N (allocation per exchange)
    #
    # Step 1: Can-start check — all four must hold:
    # - total_xrp_value >= N/2  (enough XRP across both exchanges combined)
    # - total_jpy >= N/2        (enough JPY across both exchanges combined)
    # - value(a) >= N           (exchange A has enough total value to supply N)
    # - value(b) >= N           (exchange B has enough total value to supply N)
    # If any fails → INSUFFICIENT: stop, log needed amount. Manual transfer required.
    #
    # Step 2: Count component values (xrp_value and jpy per exchange) below N/2.
    # 0 below N/2 → balanced: allocate N/2 JPY + N/2 XRP value per exchange. Start trading.
    # 1 below N/2 → single binding constraint: use it fully, derive the other. Start trading.
    # 2 below N/2 (always on different exchanges — same-exchange pair would fail value >= N):
    # - one is XRP, other is JPY → take the lesser as binding, derive the rest. Start trading.
    # - both are XRP → return BUY_BASE step to buy XRP on cheaper exchange, then restart.
    # - both are JPY → return SELL_BASE step to sell XRP on more expensive exchange, then restart.

    _keep = min_jpy_to_keep or {}
    half_n = allocated_value_jpy / 2.0
    exchanges = list(balances.keys())
    ex_a, ex_b = exchanges[0], exchanges[1]

    jpy = {ex: max(balances[ex]["quote"] - _keep.get(ex.name, 0.0), 0.0) for ex in exchanges}
    xrp = {ex: balances[ex]["base"] for ex in exchanges}
    xrp_value = {ex: xrp[ex] * prices[ex] for ex in exchanges}
    total_value = {ex: xrp_value[ex] + jpy[ex] for ex in exchanges}

    # Step 1: Can-start check
    if (
        total_value[ex_a] < allocated_value_jpy
        or total_value[ex_b] < allocated_value_jpy
    ):
        _log_insufficient(balances, jpy, xrp_value, total_value, allocated_value_jpy, _keep, symbol)
        fallback = {ex: Allocation(quote=half_n, base=half_n / prices[ex]) for ex in exchanges}
        return AllocationPlan(allocations=fallback, case=RebalanceCase.INSUFFICIENT)

    # Step 2: find which of the four component values are below N/2
    # low entries: (value, exchange, is_xrp_side)
    low = []
    for ex in exchanges:
        if xrp_value[ex] < half_n:
            low.append((xrp_value[ex], ex, True))
        if jpy[ex] < half_n:
            low.append((jpy[ex], ex, False))

    if len(low) == 0:
        allocs = {ex: Allocation(quote=half_n, base=half_n / prices[ex]) for ex in exchanges}
        _log_allocs(exchanges, symbol, balances, jpy, xrp, allocs)
        return AllocationPlan(allocations=allocs, case=RebalanceCase.OK)

    if len(low) == 1:
        # Case 1: single binding constraint — use it fully, derive the rest
        allocs = _derive_allocs(low[0], allocated_value_jpy, exchanges, prices, jpy, xrp)
        _log_allocs(exchanges, symbol, balances, jpy, xrp, allocs)
        return AllocationPlan(allocations=allocs, case=RebalanceCase.OK)

    # len(low) == 2: two values < N/2
    # They must be on different exchanges (same-exchange pair would fail value(ex) >= N in step 1)
    _, _, is_xrp_0 = low[0]
    _, _, is_xrp_1 = low[1]

    if is_xrp_0 != is_xrp_1:
        # Case 3.1: complementary — one XRP-light, one JPY-light on different exchanges
        binding = min(low, key=lambda x: x[0])
        allocs = _derive_allocs(binding, allocated_value_jpy, exchanges, prices, jpy, xrp)
        _log_allocs(exchanges, symbol, balances, jpy, xrp, allocs)
        return AllocationPlan(allocations=allocs, case=RebalanceCase.OK)

    if is_xrp_0:
        # Case 3.2: both XRP-light → BUY_BASE on cheaper exchange
        cheaper = min(exchanges, key=lambda ex: prices[ex])
        deficit = (half_n - xrp_value[cheaper]) / prices[cheaper]
        LOG.info(f"REBALANCE {cheaper.name} {symbol.name} BUY {deficit:.4f} (both exchanges XRP-light)")
        steps = [RebalanceStep(exchange=cheaper, symbol=symbol, side=Side.BUY, amount=deficit)]
        fallback = {ex: Allocation(quote=half_n, base=half_n / prices[ex]) for ex in exchanges}
        return AllocationPlan(allocations=fallback, case=RebalanceCase.BUY_BASE, steps=steps)

    # Case 3.3: both JPY-light → SELL_BASE on more expensive exchange
    expensive = max(exchanges, key=lambda ex: prices[ex])
    total_jpy_shortfall = sum(max(half_n - jpy[ex], 0.0) for ex in exchanges)
    sell_amount = total_jpy_shortfall / prices[expensive]
    LOG.info(f"REBALANCE {expensive.name} {symbol.name} SELL {sell_amount:.4f} (both exchanges JPY-light)")
    steps = [RebalanceStep(exchange=expensive, symbol=symbol, side=Side.SELL, amount=sell_amount)]
    fallback = {ex: Allocation(quote=half_n, base=half_n / prices[ex]) for ex in exchanges}
    return AllocationPlan(allocations=fallback, case=RebalanceCase.SELL_BASE, steps=steps)


def _derive_allocs(
    binding: tuple,
    allocated_value_jpy: float,
    exchanges: list,
    prices: dict,
    jpy: dict,
    xrp: dict,
) -> dict:
    val, ex_bind, is_xrp = binding
    ex_other = next(e for e in exchanges if e != ex_bind)
    n = allocated_value_jpy

    if is_xrp:
        # XRP-light on ex_bind: use all its XRP (= val), fill the rest with JPY to reach N.
        # ex_other mirrors: its JPY matches ex_bind's XRP capacity (= val), rest is XRP.
        alloc_jpy_bind = n - val
        alloc_jpy_other = val
        alloc_xrp_val_other = n - val
        return {
            ex_bind: Allocation(quote=alloc_jpy_bind, base=xrp[ex_bind]),
            ex_other: Allocation(quote=alloc_jpy_other, base=alloc_xrp_val_other / prices[ex_other]),
        }
    else:
        # JPY-light on ex_bind: use all its JPY (= val), fill the rest with XRP to reach N.
        # ex_other mirrors: its XRP matches ex_bind's JPY capacity (= val), rest is JPY.
        alloc_xrp_val_bind = n - val
        alloc_xrp_val_other = val
        alloc_jpy_other = n - val
        return {
            ex_bind: Allocation(quote=val, base=alloc_xrp_val_bind / prices[ex_bind]),
            ex_other: Allocation(quote=alloc_jpy_other, base=alloc_xrp_val_other / prices[ex_other]),
        }


def _log_allocs(exchanges, symbol, balances, jpy, xrp, allocs):
    for ex in exchanges:
        a = allocs[ex]
        LOG.info(
            f"ALLOCATION {ex.name} {symbol.name}"
            f" balance: quote={jpy[ex]:.0f} base={xrp[ex]:.4f}"
            f" → alloc: quote={a.quote:.0f} base={a.base:.4f}"
        )


def _log_insufficient(balances, jpy, xrp_value, total_value, allocated_value_jpy, keep, symbol):
    LOG.error(f"ALLOCATION INSUFFICIENT {symbol.name}: need N={allocated_value_jpy:.0f} per exchange")
    for ex in balances:
        bal = balances[ex]
        LOG.error(
            f"  {ex.name}: base={bal['base']:.4f} quote={bal['quote']:.0f}"
            f" (after keep={keep.get(ex.name, 0.0):.0f})"
            f" xrp_value={xrp_value[ex]:.0f} jpy={jpy[ex]:.0f}"
            f" total={total_value[ex]:.0f} need={allocated_value_jpy:.0f}"
        )


class MultiRebalanceCase(Enum):
    OK = "ok"
    REBALANCE = "rebalance"        # steps written to rebalance.jsonl; run corvus then restart
    INSUFFICIENT = "insufficient"  # total value too low; manual transfer needed


@dataclass
class MultiAllocationPlan:
    case: MultiRebalanceCase
    allocations: dict[ExchangeName, dict[Symbol, Allocation]] = field(default_factory=dict)
    steps: list[RebalanceStep] = field(default_factory=list)


def plan_multi_allocation(
    pairs: list,  # list[PairConfig]
    maker_exchange: ExchangeName,
    taker_exchange: ExchangeName,
    jpy: dict[ExchangeName, float],
    base: dict[ExchangeName, dict[Symbol, float]],
    prices: dict[ExchangeName, dict[Symbol, float]],
    min_jpy_to_keep: dict[str, float] | None = None,
) -> MultiAllocationPlan:
    # Calls plan_allocation for each symbol in order, deducting allocated JPY between calls.
    # Earlier symbols claim JPY first; order the pairs list by priority.
    exchanges = [maker_exchange, taker_exchange]
    keep = min_jpy_to_keep or {}

    remaining_jpy = {ex: max(jpy[ex] - keep.get(ex.name, 0.0), 0.0) for ex in exchanges}
    all_allocs: dict[ExchangeName, dict[Symbol, Allocation]] = {ex: {} for ex in exchanges}
    all_steps: list[RebalanceStep] = []

    for pc in pairs:
        sym = pc.symbol
        symbol_balances = {ex: {"quote": remaining_jpy[ex], "base": base[ex].get(sym, 0.0)} for ex in exchanges}
        symbol_prices = {ex: prices[ex][sym] for ex in exchanges}

        # min_jpy_to_keep already deducted from remaining_jpy above; don't pass again
        plan = plan_allocation(sym, symbol_balances, symbol_prices, pc.allocated_value_jpy, order_size=pc.order_size)

        if plan.case == RebalanceCase.INSUFFICIENT:
            return MultiAllocationPlan(case=MultiRebalanceCase.INSUFFICIENT)

        for ex in exchanges:
            all_allocs[ex][sym] = plan.allocations[ex]
            remaining_jpy[ex] -= plan.allocations[ex].quote

        all_steps.extend(plan.steps)

    if all_steps:
        _log_multi_rebalance(all_steps, prices)
        return MultiAllocationPlan(case=MultiRebalanceCase.REBALANCE, steps=all_steps, allocations=all_allocs)

    _log_multi_allocs(pairs, exchanges, all_allocs)
    return MultiAllocationPlan(case=MultiRebalanceCase.OK, allocations=all_allocs)


def write_rebalance_file(steps: list[RebalanceStep], path: str = "rebalance.jsonl"):
    with open(path, "w") as f:
        f.writelines(json.dumps({
                "exchange": step.exchange.name,
                "symbol": step.symbol.name,
                "side": step.side.name,
                "amount": round(step.amount, 8),
            }) + "\n" for step in steps)
    LOG.error(f"Rebalance steps written to {path} — review it, then execute it with corvus")


def _log_multi_allocs(pairs, exchanges, allocs):
    for ex in exchanges:
        for pc in pairs:
            a = allocs[ex][pc.symbol]
            LOG.info(f"ALLOCATION {ex.name} {pc.symbol.name} quote={a.quote:.0f} base={a.base:.6f}")


def _log_multi_rebalance(steps, prices):
    for step in steps:
        jpy = step.amount * prices[step.exchange][step.symbol]
        LOG.warning(
            f"REBALANCE {step.exchange.name} {step.symbol.name}"
            f" {step.side.name} {step.amount:.6f} (~{jpy:.0f} JPY)"
        )
    LOG.warning("Review rebalance.jsonl, then execute it with corvus")


def compute_pair_allocations(
    jpy: float,
    base_per_symbol: dict[Symbol, float],
    prices: dict[Symbol, float],
    max_jpy_per_symbol: dict[Symbol, float],
    min_jpy_to_keep: float = 0.0,
    order_size_per_symbol: dict[Symbol, float] | None = None,
) -> dict[Symbol, Allocation]:
    """
    Allocate quote/base for multiple symbol pairs sharing one exchange JPY pool.

    jpy:                total JPY on the exchange
    base_per_symbol:    {symbol: base_amount}
    prices:             {symbol: mid_price}
    max_jpy_per_symbol: {symbol: jpy_cap per pair}
    min_jpy_to_keep:    JPY ring-fenced before any allocation (e.g. reserved for other strategies)
    """
    if order_size_per_symbol is None:
        order_size_per_symbol = {}

    available_jpy = max(jpy - min_jpy_to_keep, 0.0)
    total_requested = sum(max_jpy_per_symbol.values())

    result: dict[Symbol, Allocation] = {}
    failed: list[str] = []

    for symbol, max_jpy in max_jpy_per_symbol.items():
        base = base_per_symbol.get(symbol, 0.0)
        price = prices[symbol]
        order_size = order_size_per_symbol.get(symbol, 0.0)

        # Pro-rate JPY across pairs when total requested exceeds what's available
        if total_requested > 0 and available_jpy < total_requested:
            jpy_for_pair = available_jpy * (max_jpy / total_requested)
        else:
            jpy_for_pair = max_jpy

        total_value = base * price + jpy_for_pair
        if total_value <= 0:
            failed.append(f"{symbol.name}: zero total value")
            continue

        fraction = min(max_jpy / total_value, 1.0)
        alloc_quote = jpy_for_pair * fraction
        alloc_base = base * fraction

        if alloc_quote < _MIN_ALLOCATION_JPY and alloc_base < order_size:
            failed.append(
                f"{symbol.name}: allocation too small —"
                f" quote={alloc_quote:.2f} (min {_MIN_ALLOCATION_JPY:.0f})"
                f" base={alloc_base:.6f} (min {order_size})"
            )
            continue

        LOG.info(
            f"ALLOCATION {symbol.name}"
            f" quote={alloc_quote:.2f} base={alloc_base:.6f}"
            f" fraction={fraction:.3f} total={total_value:.2f} jpy_pool={jpy_for_pair:.2f}"
        )
        result[symbol] = Allocation(quote=alloc_quote, base=alloc_base)

    if failed:
        for msg in failed:
            LOG.error(f"ALLOCATION FAILED {msg}")
        sys.exit(1)

    return result


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)

    # ── helpers ──────────────────────────────────────────────────────────────

    BITBANK = ExchangeName.BITBANK
    GMO = ExchangeName.GMO

    def _show(label: str, balances: dict, prices: dict, allocations: dict | None = None):
        print(f"\n{label}")
        for ex, bal in balances.items():
            total = bal["base"] * prices[ex] + bal["quote"]
            line = f"  {ex.name:8s}  XRP={bal['base']:7.1f}" f"  JPY={bal['quote']:>12,.0f}" f"  total≈{total:>12,.0f}"
            if allocations and ex in allocations:
                a = allocations[ex]
                line += f"   →  alloc XRP={a.base:6.1f}  JPY={a.quote:>10,.0f}"
            print(line)

    def _run_session(
        session: int,
        balances: dict,
        prices: dict,
        allocated_value_jpy: float,
        order_size: float,
        orders_per_session: int,
        min_jpy_to_keep: dict[str, float] | None = None,
    ) -> bool:
        """Returns False when no further trading is possible."""
        print(f"\n{'─'*64}")
        print(f"SESSION {session}  (restart — fetching balances from exchange APIs)")

        _keep = min_jpy_to_keep or {}
        XRP = Symbol.XRP_SPOT
        allocs: dict[ExchangeName, Allocation] = {}
        try:
            for ex, bal in balances.items():
                result = compute_pair_allocations(
                    jpy=bal["quote"],
                    base_per_symbol={XRP: bal["base"]},
                    prices={XRP: prices[ex]},
                    max_jpy_per_symbol={XRP: allocated_value_jpy},
                    min_jpy_to_keep=_keep.get(ex.name, 0.0),
                    order_size_per_symbol={XRP: order_size},
                )
                allocs[ex] = result[XRP]
        except SystemExit:
            print("  *** Allocation failed — strategy would exit here ***")
            return False

        _show("  Balances + allocations:", balances, prices, allocs)

        # GMO price > Bitbank price → arb direction is always BUY on Bitbank, SELL on GMO.
        max_from_maker_jpy = int(allocs[BITBANK].quote / (order_size * prices[BITBANK]))
        max_from_taker_xrp = int(allocs[GMO].base / order_size)
        trades = min(max_from_maker_jpy, max_from_taker_xrp, orders_per_session)

        if trades == 0:
            print("  No trades possible with current allocation (inventory exhausted on one side)")
            return False

        traded_xrp = trades * order_size
        print(
            f"\n  Simulating {trades} orders × {order_size:.0f} XRP"
            f"  (limit: maker_jpy={max_from_maker_jpy}  taker_xrp={max_from_taker_xrp})"
        )

        balances[BITBANK]["quote"] -= traded_xrp * prices[BITBANK]
        balances[BITBANK]["base"] += traded_xrp
        balances[GMO]["quote"] += traded_xrp * prices[GMO]
        balances[GMO]["base"] -= traded_xrp
        return True

    # ── Scenario 1: basic, no min_jpy_to_keep ────────────────────────────────

    print("=" * 64)
    print("SCENARIO 1  —  basic, no min_jpy_to_keep")
    print("  allocated_value_jpy=100,000  order_size=100 XRP  orders_per_session=5")
    print("  GMO price (203) > Bitbank price (200) → always BUY on Bitbank")

    prices = {BITBANK: 200.0, GMO: 203.0}
    balances = {
        BITBANK: {"base": 1000.0, "quote": 200_000.0},
        GMO: {"base": 1000.0, "quote": 200_000.0},
    }
    _show("Initial state:", balances, prices)

    for s in range(1, 8):
        if not _run_session(s, balances, prices, 100_000.0, 100.0, 5):
            break

    _show("\nFinal state:", balances, prices)

    # ── Scenario 2: with min_jpy_to_keep ─────────────────────────────────────

    print("\n\n" + "=" * 64)
    print("SCENARIO 2  —  with min_jpy_to_keep=50,000 on each exchange")
    print("  Same setup, but 50k JPY is ring-fenced and invisible to pisces")

    prices = {BITBANK: 200.0, GMO: 203.0}
    balances = {
        BITBANK: {"base": 1000.0, "quote": 200_000.0},
        GMO: {"base": 1000.0, "quote": 200_000.0},
    }
    _show("Initial state:", balances, prices)

    for s in range(1, 8):
        if not _run_session(
            s, balances, prices, 100_000.0, 100.0, 5, min_jpy_to_keep={"BITBANK": 50_000.0, "GMO": 50_000.0}
        ):
            break

    _show("\nFinal state:", balances, prices)

    # ── Scenario 3: multi-pair (XRP + BTC) sharing one JPY pool ─────────────

    print("\n\n" + "=" * 64)
    print("SCENARIO 3  —  multi-pair: XRP + BTC, shared JPY pool per exchange")
    print("  allocated_value_jpy=100,000 per pair  order_size=100 XRP / 0.01 BTC")
    print("  GMO consistently higher on both pairs")

    XRP = Symbol.XRP_SPOT
    BTC = Symbol.BTC_SPOT

    mp_prices = {XRP: 200.0, BTC: 10_000_000.0}
    mp_prices_gmo = {XRP: 203.0, BTC: 10_030_000.0}

    multi_balances = {
        BITBANK: {XRP: 1000.0, BTC: 0.02, "jpy": 200_000.0},
        GMO: {XRP: 1000.0, BTC: 0.02, "jpy": 200_000.0},
    }

    def _show_multi(label, mb, maker_allocs=None, taker_allocs=None):
        print(f"\n{label}")
        for ex, b in mb.items():
            px = mp_prices if ex == BITBANK else mp_prices_gmo
            xrp_val = b[XRP] * px[XRP]
            btc_val = b[BTC] * px[BTC]
            print(
                f"  {ex.name:8s}  XRP={b[XRP]:7.1f}  BTC={b[BTC]:.4f}"
                f"  JPY={b['jpy']:>12,.0f}  total≈{xrp_val+btc_val+b['jpy']:>12,.0f}"
            )
            if maker_allocs and ex == BITBANK:
                for sym, a in maker_allocs.items():
                    print(f"           {sym.name:10s}  alloc: base={a.base:.4f}  quote={a.quote:>10,.0f}")
            if taker_allocs and ex == GMO:
                for sym, a in taker_allocs.items():
                    print(f"           {sym.name:10s}  alloc: base={a.base:.4f}  quote={a.quote:>10,.0f}")

    _show_multi("Initial state:", multi_balances)

    for session in range(1, 5):
        print(f"\n{'─'*64}")
        print(f"SESSION {session}  (restart)")

        maker_jpy = multi_balances[BITBANK]["jpy"]
        maker_base = {XRP: multi_balances[BITBANK][XRP], BTC: multi_balances[BITBANK][BTC]}
        try:
            maker_allocs = compute_pair_allocations(
                jpy=maker_jpy,
                base_per_symbol=maker_base,
                prices=mp_prices,
                max_jpy_per_symbol={XRP: 100_000.0, BTC: 100_000.0},
                order_size_per_symbol={XRP: 100.0, BTC: 0.01},
            )
        except SystemExit:
            print("  *** Maker allocation failed ***")
            break

        taker_jpy = multi_balances[GMO]["jpy"]
        taker_base = {XRP: multi_balances[GMO][XRP], BTC: multi_balances[GMO][BTC]}
        try:
            taker_allocs = compute_pair_allocations(
                jpy=taker_jpy,
                base_per_symbol=taker_base,
                prices=mp_prices_gmo,
                max_jpy_per_symbol={XRP: 100_000.0, BTC: 100_000.0},
                order_size_per_symbol={XRP: 100.0, BTC: 0.01},
            )
        except SystemExit:
            print("  *** Taker allocation failed ***")
            break

        _show_multi("  Balances + allocations:", multi_balances, maker_allocs, taker_allocs)

        for sym, order_size in [(XRP, 100.0), (BTC, 0.01)]:
            m = maker_allocs[sym]
            t = taker_allocs[sym]
            max_from_jpy = int(m.quote / (order_size * mp_prices[sym]))
            max_from_taker = int(t.base / order_size)
            trades = min(max_from_jpy, max_from_taker, 3)
            if trades == 0:
                print(f"  {sym.name}: no trades possible")
                continue
            traded = trades * order_size
            print(f"  {sym.name}: {trades} orders × {order_size} = {traded} traded")
            multi_balances[BITBANK]["jpy"] -= traded * mp_prices[sym]
            multi_balances[BITBANK][sym] += traded
            multi_balances[GMO]["jpy"] += traded * mp_prices_gmo[sym]
            multi_balances[GMO][sym] -= traded

    _show_multi("\nFinal state:", multi_balances)
