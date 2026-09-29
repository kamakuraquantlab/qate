"""What a strategy is made of, and what runs it.

`core` is the vocabulary — orders, books, trades, the event loop. This is the layer
a strategy is actually written against.

| Module | Holds |
|---|---|
| `strategy` | `Strategy`: the class a variant extends, and the handlers it overrides |
| `trader` | `Trader`: the loop that drives one strategy, warmup included |
| `chart` | Bars, the builders that form them, and the charts that carry indicators |
| `indicator`, `indicators/` | The `Indicator` contract, and SMA, EMA, MACD, RSI, ATR |
| `inventory` | What an account holds, what it may spend, and the drawdown guard |
| `pnl_tracker` | Turning fills into realized PnL and fees, per position |
| `gateway` | `DefaultGateway`: the generic gateway an adapter builds on |
| `gateway_async` | The same, for a venue with a non-blocking order path |
| `metric_log` | The local append-only record of what a strategy did |
| `param` | `ParamGrid`: the defaults, and the values a sweep walks |
| `exceptions` | `StopTradingException`, `ApiException` |
| `chat` | The interface a chat notifier implements. No implementation here |

## Writing a strategy

`strategy.Strategy` and `chart` are the two to read. `knowledge/03_writing_strategy.md`
is the contract, and `qate.strategy.corvus` is the smallest complete example.

A strategy takes its time from event timestamps -- `self.now_ts` -- and never from
the clock. That single rule is what makes a backtest mean anything, and breaking it
produces numbers rather than an error.
"""
