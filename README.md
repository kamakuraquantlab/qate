# qate

A quantitative trading library, built to be backtested. It provides the event
loop, order lifecycle, strategy base class, charts and indicators, inventory and
PnL tracking, and a simulator that fills orders against recorded order books.

It reaches no exchange. That is a property of the install, not a promise in a
README: there is no HTTP client and no WebSocket client anywhere in its dependency
tree, no venue endpoint in its source, and no request-signing or account code. A
venue arrives only as a separate package that registers an adapter, and without
one there is no code here that could connect anywhere.

It also does not know where market data lives, or what format it is in. That
belongs to whoever owns the data. `qate` defines what an order book and a trade
*are*; something else reads them and hands them over.

```bash
pip install kamakuraquantlab-qate
```

For Kamakura Quant Lab data: [Komachi](https://github.com/kamakuraquantlab/Komachi)
downloads it, [Hase](https://github.com/kamakuraquantlab/Hase) derives features
from it, and [Enoshima](https://github.com/kamakuraquantlab/Enoshima) backtests
over it. This is the library Enoshima sits on.

## Layout

| Package | Holds |
|---|---|
| `qate.core` | Event loop, models, order lifecycle, and the interfaces a venue implements |
| `qate.trading` | Strategy base class, chart and bars, indicators, inventory, PnL, risk |
| `qate.strategy` | Two worked strategies, shipped to be read |
| `qate.simulator` | The gateway a backtest fills orders against, and the queues that drive it |
| `qate.exchange` | The adapter contract and registry. No venue lives here |
| `qate.store` | A run's own output: local metrics, parquet results, InfluxDB export |
| `qate.env` | Named run directories, and machine-level settings |
| `qate.boot` | Wiring a strategy, its gateways and its feeds together |
| `qate.util` | Date ranges, counters, encoding, replay-aware logging |

## Two worked strategies

An abstract base class is a poor teacher, so `qate` ships two complete strategies:

| Strategy | Is | Shows |
|---|---|---|
| `qate.strategy.corvus` | Read a list of orders, place them, exit | The order lifecycle alone — place, reprice, cancel, fill, stop |
| `qate.strategy.pisces` | Cross-exchange arbitrage, maker one side and taker the other | A real idea, plus what startup, inventory and shutdown actually cost |

Read **corvus** first: it has no signal, no inventory and no schedule, so what is
left is the machinery every strategy needs and the part that is fiddly.

**pisces** is the same framework carrying a real trading idea, and its largest file
does no trading at all — it works out whether the strategy can start, because
exchanges have no sub-accounts and a strategy cannot assume the balance it left
behind. That ratio is the lesson, and it is what example strategies usually omit.

Neither is tuned and neither is a recommendation. Backtesting them needs nothing
but `qate`; trading them live needs an exchange adapter, which is a separate
install by design.

See [knowledge/05_corvus.md](knowledge/05_corvus.md) and
[knowledge/04_pisces.md](knowledge/04_pisces.md), and
[knowledge/03_writing_strategy.md](knowledge/03_writing_strategy.md) for the
`Variant` contract to write your own.

## Market data comes from outside

There is no reader here, deliberately. Reading recorded data means knowing a
storage layout, and a layout has an owner — for Kamakura Quant Lab data that is
`komachi`, which writes it. A trading library that also knew the layout would be a
second implementation of one question, and the day the two disagree is the day a
backtest silently skips a date the downloader thinks it has.

So a replayer hands `qate` events. `Enoshima` is the one that joins the two: it
asks `komachi` where the files are, reads them, and produces
`qate.core.model.OrderBook` and `Trade`. Anything that can produce those objects
works the same way.

`qate.util.dt_range.DtRange` is the shared vocabulary for *when*: it yields ISO
date partition keys, which is what both the bronze layer and
`qate.store.timeseries` are laid out by.

## Running a backtest

A replay is single-threaded on purpose: the events already exist, in order, and
two runs of the same data must give the same result. Two queues collapse the live
threading into one loop, so the same `Trader`, `Strategy` and gateway code runs
unchanged:

```python
from qate.simulator import ReplayQueue, SimulatorGateway, SyncEventQueue
from qate.trading.trader import Trader

# events: an iterable of (EventType, OrderBook | Trade), in timestamp order.
# Where they come from is not qate's business — see above.

gateway = SimulatorGateway(ExchangeName.GMO, slippage_rate=0.0)
gateway.set_event_queue(SyncEventQueue(gateway.handlers))   # match orders in place

trader = Trader(strategy, ReplayQueue(events))              # market data + own events
trader.add_gateway(gateway)
trader.register(EventType.MARKET_ORDER_BOOK, gateway.handle_order_book)
trader.run()                                                # returns when the data ends
```

Two behaviours are worth knowing before reading a result:

- **An order fills on the book after the one it was created on.** The simulator
  matches against the next snapshot, never the one the strategy was looking at
  when it decided. That is the latency model, and it is why a strategy cannot
  trade on information it could not have had.
- **`Trader` runs a warmup pass first**, draining events into
  `Strategy.warmup_*` until the strategy reports `is_ready`. Indicators get their
  history before anything trades, and the warmup events are not traded on.

`tests/test_backtest_end_to_end.py` is this whole path in one file, from parquet
on disk to results on disk, and is the shortest complete example.

## Where a run's results go

Locally first, always:

```python
from qate.store import metrics, timeseries

writer = metrics.MsgpackWriter(metrics.RotationInterval.FIVE_MINUTE, 256, "Metrics")
results = timeseries.ResultStore("./results", "example.v1", param_set_id)
```

Metrics are appended to local msgpack on the hot path; per-trade PnL and bars go
to parquet. A database is somewhere to copy results to afterwards, never a
dependency of producing them, so a run finishes whether or not one is reachable:

```bash
pip install 'kamakuraquantlab-qate[influx]'
```

```python
from qate.store.influx import InfluxdbStore, export_metrics

export_metrics("./run_dir", InfluxdbStore(config), bucket_name="my_backtest")
```

## Supplying an exchange

Live trading needs an adapter package. It declares an entry point and registers
one `ExchangeAdapter` per venue:

```toml
[project.entry-points."qate.exchanges"]
my_venues = "my_venues:register"
```

```python
from qate.core.model import ExchangeName
from qate.exchange import ExchangeAdapter, register

def register_all():
    register(ExchangeAdapter(
        exchange_name=ExchangeName.GMO,
        create_api=GMOApi,
        create_gateway=GMOGateway,
        create_public_connection=GMOPublicConn,
        create_private_connection=GMOPrivateConn,
    ))
```

Every hook is optional, and a venue is resolved lazily on first use. Asking for
one that is not installed raises `UnknownExchange` naming what is; asking an
adapter for something it does not provide raises
`UnsupportedExchangeCapability` naming the hook. Set `QATE_EXCHANGE_PLUGINS` to
load an adapter from a checkout that is not installed.

## Settings

| What | Where |
|---|---|
| Run directories | `~/env`, or `QATE_ENV_ROOT`, or `env_root_dir` in a `.qate.json` beside the script |
| Credentials | `~/.qate/<service>.keys`, INI, one section per key set |

There is no market-data setting. `qate` does not read market data, so it has no
opinion about where it is.

A backtest asks for no credential. `qate.env.sys_env` raises
`CredentialsNotFound` naming the file it looked for rather than falling back to
an unauthenticated call, so a live run fails at startup instead of part way
through.

## Extras

| Extra | Adds |
|---|---|
| `influx` | `influxdb-client`, for exporting metrics to a bucket |
| `fast` | `numba`, which JITs the trade-aggression hot loops |

Neither changes a result. Without `fast`, `qate.trading.aggression` runs the same
code unjitted — slower on a long replay, identical in output.

## Documentation

| Document | Read it when |
|---|---|
| [knowledge/01_philosophy.md](knowledge/01_philosophy.md) | Deciding whether to add a check, a test, or a comment |
| [knowledge/02_env.md](knowledge/02_env.md) | Environments: the directory, the lock, config discovery |
| [knowledge/03_writing_strategy.md](knowledge/03_writing_strategy.md) | Writing a strategy: the `Variant` contract, config vs params |
| [knowledge/04_pisces.md](knowledge/04_pisces.md) | Cross-exchange arbitrage: startup, rebalance, shutdown |
| [knowledge/05_corvus.md](knowledge/05_corvus.md) | Single-shot order execution — the order lifecycle alone |

## Development

```bash
pip install -e '.[dev]'
pytest
```

## Licence

Apache 2.0. See [LICENSE.md](LICENSE.md).
