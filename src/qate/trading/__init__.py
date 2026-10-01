"""What a strategy is made of, and what runs it.

`core` is the vocabulary — orders, books, trades, the event loop. This is the layer
a strategy is actually written against.

| Module | Holds |
|---|---|
| `strategy` | `Strategy`: the class a variant extends, and the handlers it overrides |
| `config` | `StrategyConfig`: what a run trades, as the strategy declares it |
| `trader` | `Trader`: the loop that drives one strategy, warmup included |
| `runtime` | `Runtime`: the process around the trader -- conns, gateways, reporters |
| `chart` | Bars, the builders that form them, and the charts that carry indicators |
| `indicator`, `indicators/` | The `Indicator` contract, and SMA, EMA, MACD, RSI, ATR |
| `inventory` | What an account holds, what it may spend, and the drawdown guard |
| `pnl_tracker` | Turning fills into realized PnL and fees, per position |
| `gateways/` | The gateway implementations an adapter builds on: blocking, and async |
| `metrics` | Emitting metrics, and the local append-only log they are recorded in |
| `param` | `ParamGrid`: the defaults, and the values a sweep walks |
| `exceptions` | `StopTradingException`, `ApiException` |
| `reporter` | `Reporter`: where a run's outcomes are published. No implementation here |

## Writing a strategy

`strategy.Strategy` and `chart` are the two to read. `knowledge/03_writing_strategy.md`
is the contract, and `qate.strategy.corvus` is the smallest complete example.

A strategy takes its time from event timestamps -- `self.now_ts` -- and never from
the clock. That single rule is what makes a backtest mean anything, and breaking it
produces numbers rather than an error.
"""
