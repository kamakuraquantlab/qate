"""Where a run's own output goes.

Three modules, and the order between them is the design:

| Module | Holds |
|---|---|
| `metrics` | Trading metrics as local msgpack. Written on the hot path, always. |
| `timeseries` | Hive-partitioned parquet, and the PnL and bar stores a backtest fills |
| `influx` | The optional export of `metrics` into an InfluxDB bucket |

Everything is written locally first. A database is somewhere to copy results to
afterwards, never a dependency of producing them, so a run finishes whether or
not one is reachable.

Market data is not here, and not anywhere in `qate`. It is read rather than
written, and locating and reading it belongs to whoever owns it -- for Kamakura
Quant Lab data that is `komachi`, and the replayer that joins the two is
Enoshima. A trading library that also knew the storage layout would be a second
implementation of a question that already has an owner.
"""
