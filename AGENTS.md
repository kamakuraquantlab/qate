# qate — Agent Entry Point

Public trading library (`qate`), part of Kamakura Quant Lab. Read
[README.md](README.md) first; this page holds the things that will cost you if
you miss them.

## The one invariant

**This package must never be able to reach an exchange.** It is public. Every
change is measured against that.

Concretely, and in order of how easy each is to break by accident:

1. **No HTTP client and no WebSocket client may enter the dependency tree, at any
   depth.** Both live in `qate-exchanges`. Their absence is what makes the claim
   checkable with `pip list` rather than by review. An "optional" extra that pulls
   one in is the same mistake wearing a hat, and so is a data dependency that
   brings one transitively — which is how `httpx` got in here once, through
   `komachi`.
2. **No venue endpoint, symbol mapping, or request signing belongs here.**
   `qate.core.conn` declares what a connection *is*; the implementation is an
   adapter's.
3. **No credential, path or host may be named here at all.** Not "no default" —
   none. Credentials, run directories and machine settings are `qate-env`, so this
   package has nothing that could read a key or fall back to an unauthenticated
   call. `qate.env.sys_env` was here and raised `CredentialsNotFound`; the rule was
   weaker then, because a rule about how to read a credential is weaker than not
   being able to.
4. **Nothing that only makes sense from the inside.** No absolute warehouse path,
   bucket name or production environment name; no reference to a repository or
   document the reader cannot open. Applies to source, tests, docs and fixtures
   alike.

Two tests hold this, and both are worth understanding before changing them:

- `test_exchange_registry.py::test_nothing_installed_means_nothing_reachable` —
  every factory entry point raises when no adapter is registered.
- `test_backtest_end_to_end.py::test_a_backtest_never_asks_for_an_exchange` —
  a full replay runs with a tripwire in place of the registry. It asserts that
  the backtest path never *consults* the registry, rather than that no adapter is
  installed; the latter depends on the machine and on a developer's machine an
  adapter usually is.

## Where a change goes

| Adding | Goes in | Not in |
|---|---|---|
| A venue, or anything venue-specific | `qate-exchanges` | here |
| A live-trading dev program | `qate-exchanges/tools/` | `tests/` |
| A production strategy | its own private repo | here |
| Anything naming a directory, host or credential | `qate-env` | here |
| A file an environment can contain, or its loader | `qate-env` | here |
| A third worked example | probably nowhere — two is the point | |
| A generic indicator, chart, or risk rule | `qate.trading` | |
| Another way of satisfying `ExchangeGateway` | `qate.trading.gateways` | `qate.core` |
| A way of reading recorded market data | the replayer (Enoshima) | here |
| A way of storing a run's output | the tool that produces it (Enoshima) | here |

**Every module here has a consumer here.** Four sweeps have now removed code that
was in this package only because it was in the private library it came from:
`qate.store`'s parquet and InfluxDB layers, `chart2.py`, the feature-engineering
and risk modules in `qate.trading`, and `qate.env` with `qate.boot` — a run
directory, a credential lookup and a config loader, none of which anything here
imported. Before adding a module, name what in this package or in Enoshima will
import it. Before keeping one, check that something still does — and check
`qate-env`, `qate-exchanges` and the private live runner too, all of which depend on
this package; one of those sweeps nearly broke `qate-exchanges`.

**`qate.trading.runtime.Runtime` is the exception, and knowingly.** Nothing here
imports it: a backtest drives a `Trader` directly and the live runner that needs a
loop is private. It stays because it is what gives `Reporter` and `MetricLog` a
meaning — the composition they were designed for, expressed in the package that
defines them — and because it names nothing outside this package.
`tests/test_reporter.py` is its consumer.

**`qate` does not read market data and does not know where it lives.** A replayer
hands it events. This was not the original shape: there was a `qate.data.bronze`
that globbed the tree and duplicated `komachi.bronze`'s answers about which days
are complete, with a docstring promising by hand that the two agreed. Reading
recorded data means knowing a layout, a layout has an owner, and two
implementations of one question drift.

## The two shipped strategies

`qate.strategy.corvus` and `qate.strategy.pisces` are documentation that happens to
run. They are the answer to "what does a real strategy look like", which an
abstract base class cannot give.

- **They are examples, not products.** Do not tune them, do not add features to
  make them competitive, and do not let them accumulate a third and fourth variant.
  A production strategy belongs in its own private repository.
- **Do not simplify away the unglamorous parts.** `pisces`'s largest file does no
  trading — it reconciles real balances before the strategy is allowed to start.
  That file *is* the lesson; deleting it to make the example shorter would remove
  the only thing the example has that a tutorial does not.
- **Keep them honest about what they need.** Backtesting works with `qate` alone;
  trading live needs an adapter. `SimulatorGateway` reports unlimited balances, so
  pisces's rebalance paths only run against a real account, and the docs say so.
- `tests/test_example_strategies.py` checks they still load, construct, and match
  the config and params tables in `knowledge/04_pisces.md`. Example code that no
  longer runs is worse than no example.
- **Their `Config` extends `qate.trading.config.StrategyConfig`** — two methods
  saying which venues and which markets a run needs. It was `qate.boot.config.BootConfig`,
  in a module that also read JSON files and knew where credentials live; a strategy
  implements the contract, so the contract belongs beside `Strategy` and the rest
  went to `qate-env`.

## Facts worth knowing before editing

- **Strategies must use `self.now_ts`** from event timestamps, never
  `time.time()`. Wall-clock time breaks replay, and silently: the backtest still
  runs and the numbers are wrong.
- **A simulated order fills on the book *after* the one it was created on.**
  That one-snapshot delay is the latency model. Do not "fix" it into same-book
  fills; it is what stops a strategy trading on information it could not have had.
- **`Trader.before_loop` runs a `Warmup` pass** that drains events into
  `Strategy.warmup_*` until `is_ready`. It consumes at least one event even when
  the strategy is ready from the start. A test that counts events must account
  for it.
- **`qate` has no storage layer.** It writes one thing: the local metric log in
  `qate.trading.metrics`. Parquet layouts, database clients and export live with
  whoever produces the results — for a backtest, Enoshima. `qate.store` existed and
  was removed for this reason; two of its three modules had no consumer here at all.
- **Nothing on a run's hot path may require a database to be reachable.** The log
  is appended locally and shipped later, if at all.
- **Metric objects pack enum *values*, not names** — `[measurement, ts, tags,
  fields]` with integer keys. `ExchangeName`, `Symbol`, `Measurement`, `Tag` and
  `Field` are therefore append-only: reordering a member silently rewrites the
  meaning of every metric file already on disk.
- **Every `date=` partition is an Asia/Tokyo day**, 15:00–14:59 UTC, and the
  timestamps inside the files are UTC epochs. `DtRange.days` already produces
  these keys; do not convert.
- **The simulator is a gateway, not a mode.** `SimulatorGateway` sits in
  `qate.trading.gateways` beside the three live ones, and that placement is load
  bearing: `ExchangeGateway` can only stay a plain interface for as long as one
  implementation of it has no thread and no queue. It had its own top-level package
  once, which is how the contract came to extend `EventLoop` in the first place —
  a backtest then had to hand the gateway a fake synchronous queue to get a fill
  computed inside the strategy's own call.
- **`ReplayQueue` stays here, and is the point of `qate.core.ev_q`.** Swapping the
  live `queue.Queue` for `qate.trading.replay.ReplayQueue` is the entire difference
  between trading and replaying, and it is what makes a backtest expressible with
  nothing but this package installed. A replayer with a real data layout brings its
  own — Enoshima's `Channel` reads chunks shared between concurrent runs — so do
  not move this one out to meet it, and do not grow it towards a layout.
- **A bar builder is a closer plus a creator.** `qate.trading.chart` composes
  *when a bar ends* with *what the bar is*, which is what makes
  `HeikinAshiRangeBarBuilder` expressible; an inheritance hierarchy could not
  express it and that is why there was once a `chart2.py`. Add a combination by
  pairing existing pieces, not by subclassing a builder.
- **`RangeBarBuilder._box` has a property setter on purpose.** A live strategy
  assigns it directly. The box size lives on the closer now, so without the
  forwarding setter that assignment would land on an unused attribute and the box
  size would silently never change. `set_box_size` is the supported way.
- **A reporter is the one place that swallows exceptions.** `Runtime._report`
  logs and continues, because nothing about a trading decision depends on anyone
  being told and an unreachable webhook must not take a live strategy down. That is
  deliberately the opposite of the rule below; do not copy the pattern elsewhere.
- **Fail fast.** Do not add runtime guards for logic bugs. Check only for
  dynamic errors: a missing file, an empty book, a network fault.

## Before publishing

```bash
pytest
python3 -m build && cd dist && unzip -q *.whl
grep -rniE 'AKIA|s3://|BEGIN [A-Z ]*PRIVATE KEY|/home/|api[_-]?key *=' .
```

The scan is on the wheel, not the repository: what ships is the wheel. Add the
private names of whatever ecosystem this is being extracted from — repositories,
strategies, warehouse roots — to the pattern; that list belongs with the private
code, not in a public file.

The failure this exists to catch is usually not a leaked credential. It is a
public package written as though its reader works here: a reference to a
repository they cannot open, explaining a decision they cannot check. That reads
as carelessness to the one audience that matters, and it is the easiest thing in
the world to leave behind.
