"""Two worked strategies, shipped to be read.

`qate.trading.strategy.Strategy` is an abstract class, and an abstract class is a
poor teacher. These are the two smallest complete answers to "what does a real
one look like" — installed rather than pasted into a README, so they can be
imported, backtested and stepped through.

| Strategy | Is | Shows |
|---|---|---|
| `corvus` | A single-shot order executor: read a list of orders, place them, exit | The order lifecycle on its own — place, reprice, cancel, fill, stop |
| `pisces` | Cross-exchange arbitrage: post a maker order on one venue, hedge on another | Inventory, PnL, two venues at once, and what startup and shutdown actually cost |

Read `corvus` first. It has no profit logic, no inventory and no schedule, so what
is left is purely the mechanics of getting an order filled — which is the part that
is fiddly and the part every strategy needs.

`pisces` is the same framework carrying a real trading idea, and most of its bulk
is the unglamorous half: reconciling what the exchange says you hold against what
the strategy assumed, and cancelling cleanly on the way out. That ratio is the
lesson.

## They are examples, not products

Neither is tuned, and neither is a recommendation. `pisces` captures a spread that
a maker rebate makes just about positive; whether that spread still exists on any
particular pair of venues today is a question for a backtest, which is what
Enoshima is for.

## What they need to run

A backtest needs nothing beyond `qate`: point an environment's
`strategy_module_name` at `qate.strategy.pisces` and replay. Note that
`SimulatorGateway` reports unlimited balances, so `pisces`'s startup allocation
always sees case (a) and its rebalance paths are only exercised against a real
account.

Trading either of them live needs an exchange adapter, which is a separate install
by design — see `qate.exchange`.

Design notes are in `knowledge/04_pisces.md` and `knowledge/05_corvus.md`, and the
`Variant` contract both of these implement is in
`knowledge/03_writing_strategy.md`.
"""
