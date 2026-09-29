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

Market data is not here. It is read, not written, and it comes from
`qate.data.bronze`.
"""
