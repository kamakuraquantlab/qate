# Corvus — Design

Read a list of orders, place them, exit. Corvus is the smallest strategy that does
anything real, and one of the two `qate` ships as a worked example.

Read it first if you are here to learn the framework. It has no signal, no
inventory, no schedule and no PnL, so everything left is the order lifecycle —
which is the part every strategy needs and the part that is fiddly to get right.

`qate.strategy.corvus` is the code; [03_writing_strategy.md](03_writing_strategy.md)
is the contract it implements.

## 1 What It Is

A single-shot order executor. It reads order instructions from a JSONL file, posts
a maker order at the touch for each one, reprices any that drift, and exits once
every instruction is filled.

It has two uses, and they are the same code:

- **As an example.** What remains after the trading idea is removed.
- **As a tool.** It is what executes a rebalance that
  [04_pisces.md](04_pisces.md) has asked for. Pisces writes the file and stops; a
  human reads it; corvus does exactly what it says and nothing else.

## 2 How It Works

- Subscribes to order books for every exchange+symbol appearing in the file.
- On the first book for a market, places a limit order at best bid (BUY) or best
  ask (SELL). One order in flight per market at a time.
- If the touch has moved and the order has been resting longer than
  `reprice_interval`, cancels it and re-places on the next book.
- On fill, marks that instruction done. When all are done, raises `EventLoopExit`
  and the process ends.
- On cancel, re-places on the next book — unless stopping, in which case it stays
  cancelled.
- On `handle_stop`, cancels everything outstanding and exits once the cancels are
  confirmed.

Both halves of the reprice rule matter. Price alone would cancel and re-place on
every tick of a moving market and never fill; time alone would leave an order
sitting behind the market for the whole interval.

## 3 Config

| Field | Meaning |
|---|---|
| `instructions` | The orders to place, as `OrderInstruction(exchange, symbol, side, amount)` |
| `reprice_interval` | Seconds an order may rest at a stale price before being repriced (default 5) |

`Config.from_jsonl(path)` builds it from a file. Anything larger than the venue's
entry in `MAX_ORDER_SIZE` is split into equal chunks, because a single order above
a venue's limit is rejected rather than trimmed.

`get_exchanges()` returns the venues in the file, so unlike a collector this *does*
build gateways — it places real orders and needs real credentials.

## 4 The JSONL File

One JSON object per line:

```json
{"exchange": "BITBANK", "symbol": "XRP_SPOT", "side": "BUY", "amount": 50.0}
{"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 46.31}
```

Lines carrying a `"type"` key are skipped. That is what lets a run append its own
results to the same file it read: the record of what was asked and what happened
stays in one place, and re-reading it picks up only the instructions.

## 5 Running It

Live, so it needs an exchange adapter installed — see `qate.exchange`. Build the
config from the file and boot it like any other strategy:

```python
from qate.strategy.corvus.config import Config

config = Config.from_jsonl("rebalance.jsonl")
```

There is nothing to backtest. Corvus has no view on price and produces no PnL; a
replay would only confirm that the simulator fills orders, which
the gateway's own tests already do.

## 6 What Corvus Does NOT Do

Deliberately, and this is the whole design:

- No profit logic. It does not decide whether an order is a good idea.
- No inventory tracking. It does not know or care what the account holds.
- No schedule guard. If a venue is down, the orders simply do not fill.
- No partial-fill accounting beyond "filled or not".

It executes exactly what the file says and stops. Everything it does not do is
something a human decided before writing the file, which is the point: moving
funds is worth reading the numbers first.
