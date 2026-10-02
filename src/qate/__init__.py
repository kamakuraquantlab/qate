"""qate -- a quantitative trading library, built to be backtested.

| Package | Holds |
|---|---|
| `core` | Event loop, models, orders, and the interfaces a venue implements |
| `trading` | Strategy base class, charts, indicators, inventory, PnL, gateways, the metric log, the live runtime |
| `strategy` | Two worked strategies, shipped to be read |
| `exchange` | The adapter contract and registry, fee rates included. No venue lives here |
| `util` | Date ranges, serialization, counters, logging |

A run directory, a credential and the loader that reads them are not here: they are
`qate-env`. Nothing in this package names a path, a host or a key.

It reaches no exchange. Venue adapters are separate installs that register
themselves with `qate.exchange`; without one there is no code here that can
connect anywhere, which is what makes the library safe to run against a live
account by accident -- it cannot be done.
"""
