"""qate -- a quantitative trading library, built to be backtested.

| Package | Holds |
|---|---|
| `core` | Event loop, models, orders, and the interfaces a venue implements |
| `trading` | Strategy base class, charts, indicators, inventory, PnL, risk, the metric log |
| `simulator` | The gateway a backtest fills orders against |
| `exchange` | The adapter contract and registry. No venue lives here |
| `env` | Named run directories, and machine-level settings |
| `boot` | Wiring a strategy, its gateways and its feeds together |
| `util` | Date ranges, serialization, counters, logging |

It reaches no exchange. Venue adapters are separate installs that register
themselves with `qate.exchange`; without one there is no code here that can
connect anywhere, which is what makes the library safe to run against a live
account by accident -- it cannot be done.
"""
