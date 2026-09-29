# Pisces — Design

Cross-exchange arbitrage, and one of the two worked strategies `qate` ships. Read
this for the *why*; `qate.strategy.pisces` is the code.

The trading idea is small. Nearly all of this document is about the operational
reality around it, which is the reason the strategy is worth reading: a working
strategy is mostly not its signal.

## 1 Motivation

Its predecessor required an `assets` field to be set by hand in config before every
start — after maintenance, after a restart, after any stretch of one-directional
trading had shifted the balance between venues. That is the kind of maintenance
burden that eventually loses money by being skipped. Pisces removes it by fetching
real balances at startup and computing its own allocation.

## 2 Core Mechanic

- **Maker** (Bitbank): posts limit orders, earns maker fee rebate.
- **Taker** (Coincheck or GMO): hedges with market orders on fill.
- Profit comes from capturing the spread between exchanges, net of fees.
- `suggest_side()` keeps inventory balanced: if maker is base-light → BUY; if taker is base-light
  → SELL; otherwise pick the side with the better spread.

## 3 Why It Runs Live

Pisces is deliberately run small and live, and that is a testing decision as much
as a trading one. A cross-exchange arbitrage only turns a profit when market data,
spread arithmetic, order placement and fill tracking are *all* correct, so a small
positive PnL is evidence that the whole stack works. Static test cases over a
fast-changing codebase would not give that. See
[01_philosophy.md §2](01_philosophy.md#2-dont-write-tests).

A second, unrelated reason to run one at all: some venues discount fees by traded
volume, so a low-margin strategy can pay for itself by moving an account into a
cheaper tier.

## 4 The Restart Problem

Exchanges don't support sub-accounts. Funds are shared with other strategies and personal
holdings. After trading, balances shift from their initial state:

**Example (BTC, allocated_value_jpy=100,000 per exchange):**

| | Start | After extended one-directional trading |
|---|---|---|
| Bitbank (maker) | 50k JPY + 50k BTC-worth | 80k JPY + 20k BTC-worth |
| GMO (taker) | 50k JPY + 50k BTC-worth | 20k JPY + 80k BTC-worth |

On restart with the skewed state:
- Bitbank is JPY-heavy → `suggest_side()` leans BUY on maker
- GMO is BTC-heavy → taker can SELL immediately

These align naturally. No explicit rebalancing is needed — the existing side-selection logic
handles it. The only requirement is correct initialization from actual balances.

## 5 Target Allocation

The ideal inventory per exchange is **balanced**: equal JPY and base value, each half of
`allocated_value_jpy`:

```
target_quote = allocated_value_jpy / 2
target_base  = target_quote / mid_price
```

A balanced allocation means the full budget is usable in either direction — there is no
stranded JPY or stranded base. With a proportional allocation (the naive approach), a JPY-heavy
exchange would allocate more JPY than base, leaving JPY idle because there isn't enough base on
the other side to hedge with.

## 6 Startup Rebalance Check

On startup, `plan_allocation()` in `adjust_inventory.py` fetches actual balances, deducts
`min_jpy_to_keep`, and compares available JPY and base against the target on each exchange.
Four outcomes:

| Case | Condition | Action |
|------|-----------|--------|
| **a** | All exchanges meet target (quote ≥ target and base ≥ target) | Start trading |
| **b** | All have enough total value but base-heavy / JPY-light on some exchange | Sell base first |
| **c** | All have enough total value but JPY-heavy / base-light on some exchange | Buy base first |
| **d** | Some exchange total value < `allocated_value_jpy` | Print status and exit |

Mixed case (one exchange needs to buy, another needs to sell): treated as **a** — side
selection naturally drives both exchanges toward balance as it trades.

For cases **b** and **c**, pisces writes a `rebalance.jsonl` file and exits. Steps are sorted by
price: sell at the highest-price exchange first; buy at the lowest-price exchange first.

## 7 Rebalance Flow

```
1. Start pisces
        ↓
   plan_allocation() detects case b or c
        ↓
   Writes rebalance.jsonl and exits, logging
   that the file needs executing
        ↓
2. Operator reviews the file, then executes it
        ↓
   The orders are placed at the touch and
   repriced until filled (05_corvus.md)
        ↓
3. Restart pisces → now case a → starts trading
```

**`rebalance.jsonl` format** (one JSON object per line):
```json
{"exchange": "BITBANK", "symbol": "XRP_SPOT", "side": "BUY", "amount": 50.0}
{"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 46.31}
```

## 8 Config and Params

One `Config` class is shared by both v1 (single pair) and v2 (multiple pairs):

```python
@dataclass
class PairConfig:
    symbol: Symbol
    order_size: float

@dataclass
class Config(BootConfig):
    maker_exchange: ExchangeName
    taker_exchange: ExchangeName
    pairs: list[PairConfig]
    allocated_value_jpy: float          # max JPY-equivalent value per pair per exchange
    min_jpy_to_keep: float = 0.0        # JPY ring-fenced before any allocation
    max_orders: int = 1
```

`allocated_value_jpy` is the cap per pair per exchange. With multiple pairs, each pair requests
this amount; when total requested exceeds available JPY, the pool is split proportionally.

`min_jpy_to_keep` is deducted from the actual exchange balance before any allocation is computed.
Use it to protect JPY used by other strategies running on the same exchange account.

Params come from `param_grid.json`, and are what an optimize sweep varies:

| Field | Default | Meaning |
|---|---|---|
| `default_profit_margin_percentage` | 0.0007 | Minimum spread to capture (0.07%) |
| `transfer_profit_margin_percentage` | 0.0001 | Reduced margin when the trade also rebalances inventory |
| `price_tolerance_percentage` | 0.0001 | Price drift tolerated before repricing |

Two limits are not configurable and worth knowing: an order book older than 1s
stops orders being placed at all — which turns a venue's maintenance window into a
non-event rather than a schedule rule — and the per-pair drawdown cap is 30% of
`allocated_value_jpy`.

## 9 Clean Shutdown

On `handle_stop()`:
- Set `stop_requested = True`, `trading_mode = NO_TRADING`
- On each subsequent `handle_order_book`: cancel all confirmed, uncancelled maker orders
- Wait for cancel confirmations (`handle_order_cancelled` removes from `maker_orders`)
- Once `maker_orders` is empty: raise `EventLoopExitException`
- Safety: if cancels haven't confirmed within 30s, exit anyway with a warning

## 10 Phases

### 10.1 Phase 1 — 1 crypto, 1 maker, 1 taker (`v1.py`)

One pair: `pairs` has a single `PairConfig`. The clearest read of the two variants.

**What it added over the version it replaced:**
- No `assets` field in config. Balances fetched at startup via `_init_from_balances()`.
- Balanced target allocation via `plan_allocation()`; rebalance.jsonl written if inventory
  is skewed beyond what side selection can self-correct.
- `min_jpy_to_keep` to ring-fence JPY used by other strategies.
- Shutdown timeout (30s) so a missed cancel confirmation doesn't hang forever.
- `handle_stop` accepts the event argument (v0 silently failed to stop).
- `transfer_profit_margin` actually used: applied when inventory forces the trade side.

### 10.2 Phase 2 — Multiple cryptos, 1 maker, 1 taker (`v2.py`)

Multiple `PairConfig` entries in `pairs`. Each pair runs independently with its own
`PairState` (separate order tracking, inventory, price vars). Gateways and JPY pool are shared.

A single `allocated_value_jpy` governs the cap per pair; the allocator pro-rates automatically
when total demand exceeds available JPY.

Order routing uses `_order_to_pair: dict[ctx_id, PairState]` so fill and cancel events reach
the correct pair regardless of which symbol triggered them.

### 10.3 Phase 3 — Multiple cryptos, multiple takers

Extend to support `takers: list[ExchangeName]`.

**New in phase 3:**
- `MakerOrderEntry` carries a `taker` reference (the `TakerSpotSource` that priced the order).
- `suggest_best_opportunity()`: iterate all active takers, evaluate both sides for each, return
  `(taker, side, price)` with the best margin. Inventory constraints applied first.
- On maker fill: hedge against the stored taker (not the "current best").
- Takers with stale order books (> 1s) are skipped automatically — GMO Saturday maintenance
  becomes a non-event rather than requiring an explicit schedule guard.
- Each taker gets its own `SpotInventory` with proportional allocation from its actual balance.
- One shared `PnlTracker` across maker + all takers (open/close pairs still match correctly).

## 11 What Pisces Does NOT Do

- Cross-exchange fund transfers (not supported by exchanges, not needed — the strategy is
  self-balancing over time).
- Sub-account isolation (exchanges don't support it; `allocated_value_jpy` is the isolation
  mechanism).
- Automatic rebalance execution. Pisces detects and describes the problem; executing the fix is
  a separate step, after a human has read the numbers ([05_corvus.md](05_corvus.md)). Moving
  funds is the one thing worth looking at first, and a strategy that silently repositioned an
  account would be much harder to trust.

## 12 Running It

Backtesting needs nothing but `qate`: point an environment's
`strategy_module_name` at `qate.strategy.pisces` and replay. Any
`maker_exchange` / `taker_exchange` pair with data on both sides works — BITBANK
maker against COINCHECK taker on `BTC_SPOT` runs as it stands.

One caveat. `SimulatorGateway` reports unlimited balances, so the startup
allocation always lands on case (a) and the rebalance paths in §6 are only ever
exercised against a real account.

Trading it live needs an exchange adapter, which is a separate install by design.
See [../AGENTS.md](../AGENTS.md).
