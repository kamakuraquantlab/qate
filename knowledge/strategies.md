# The Two Worked Strategies

`qate` ships two strategies. Neither is a recommendation and neither is tuned;
they are installed so that "what does a real one look like" has an answer you can
import, backtest and step through.

## 1 corvus — single-shot order execution

**Module:** `qate.strategy.corvus` · **Variant:** `v1`

### 1.1 What it is

Read a list of orders from a JSONL file, place a maker order at the touch for each,
and exit when they are all filled. No signal, no inventory, no schedule, no PnL.

Read this one first. Everything left after removing the trading idea is the part
every strategy needs and the part that is fiddly: getting an order placed, noticing
it has drifted, cancelling it, re-placing it, knowing when you are done, and
stopping cleanly when told to.

### 1.2 How it works

- Subscribes to order books for every exchange+symbol in the file.
- On the first book for a market, places a limit order at best bid (BUY) or best
  ask (SELL) — one in flight per market at a time.
- If the touch has moved past `reprice_interval` seconds' tolerance, cancels and
  re-places on the next book.
- On fill, marks the instruction done. When all are done, raises `EventLoopExit`.
- On `handle_stop`, cancels everything outstanding and exits once the cancels are
  confirmed.

### 1.3 Config

| Field | Meaning |
|---|---|
| `instructions` | The orders to place, as `OrderInstruction(exchange, symbol, side, amount)` |
| `reprice_interval` | Seconds an order may sit at a stale price before being repriced (default 5) |

`Config.from_jsonl(path)` builds it from a file, splitting anything above the
venue's entry in `MAX_ORDER_SIZE` into equal chunks.

```json
{"exchange": "BITBANK", "symbol": "XRP_SPOT", "side": "BUY", "amount": 50.0}
{"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 46.31}
```

### 1.4 It is also a tool

Corvus is what executes a rebalance `pisces` asks for. Pisces writes the file and
exits; a human reads it; corvus does exactly what it says. Splitting it that way is
the point — see [pisces.md §11](pisces.md).

## 2 pisces — cross-exchange arbitrage

**Module:** `qate.strategy.pisces` · **Variants:** `v1` (one pair), `v2` (several)

### 2.1 What it is

Post a limit order on the **maker** venue priced ahead of the **taker** venue's
spread; when it fills, hedge immediately with a market order on the taker. The
profit is the cross-venue spread net of fees, and a maker rebate is usually what
tips it positive.

### 2.2 Why it is worth reading

The trading idea is about thirty lines. The largest file in the module is
`adjust_inventory.py`, which does no trading at all — it works out whether the
strategy can start.

That ratio is the lesson, and it is the thing example strategies usually omit.
Exchanges have no sub-accounts, so a strategy shares an account balance with
everything else using it and cannot assume the state it left behind. Before
trading, it has to fetch what it actually holds, compare that against what it
needs, and refuse to start when the answer is no.

### 2.3 A live strategy as an integration test

Pisces is deliberately run small and live. A cross-exchange arb only turns a
profit when market data, spread arithmetic, order placement and fill tracking are
*all* correct, so a small positive PnL is a signal that the whole stack works —
one that static test cases covering a fast-changing codebase would not give. See
[philosophy.md §2](philosophy.md#2-dont-write-tests) for the reasoning.

A second, unrelated reason to run one: some venues discount fees by traded volume,
so a low-margin strategy can pay for itself by moving an account into a cheaper
tier.

### 2.4 How it works

- `MakerSource` posts a limit order priced ahead of the taker spread by
  `profit_margin`.
- On maker fill, hedge on the taker with a market order.
- `suggest_side()` picks BUY or SELL from inventory balance across the two venues,
  so the strategy self-corrects rather than drifting one way.
- Staleness guard: if either book is older than 1s, place nothing. A venue's
  maintenance window becomes a non-event instead of needing a schedule rule.
- Max drawdown: 30% of `allocated_value_jpy` per pair.
- On startup, checks inventory and writes `rebalance.jsonl` if a manual rebalance
  is needed first.

### 2.5 Config

| Field | Meaning |
|---|---|
| `maker_exchange` | Venue that posts limit orders |
| `taker_exchange` | Venue used for hedge market orders |
| `pairs` | `PairConfig(symbol, order_size, allocated_value_jpy)`, one per pair |
| `min_jpy_to_keep` | JPY ring-fenced before allocation, per venue |
| `max_orders` | Concurrent maker orders (default 1) |

Params, from `param_grid.json`:

| Field | Default | Meaning |
|---|---|---|
| `default_profit_margin_percentage` | 0.0007 | Minimum spread to capture (0.07%) |
| `transfer_profit_margin_percentage` | 0.0001 | Reduced margin when the trade also rebalances inventory |
| `price_tolerance_percentage` | 0.0001 | Price drift tolerated before repricing |

### 2.6 Running it

Backtesting needs nothing but `qate`: point an environment at
`qate.strategy.pisces` and replay. A `maker_exchange` / `taker_exchange` pair with
data on both sides is the only requirement — BITBANK maker against COINCHECK taker
on `BTC_SPOT` runs as it stands.

One caveat: `SimulatorGateway` reports unlimited balances, so the startup
allocation always sees "case a" and the rebalance paths in §5 of
[pisces.md](pisces.md) are only exercised against a real account.

Trading it live needs an exchange adapter, which is a separate install by design.
See [../AGENTS.md](../AGENTS.md).

Full design notes: [pisces.md](pisces.md).
