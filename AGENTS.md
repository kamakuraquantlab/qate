# qate — Agent Entry Point

Public trading library (`qate`), part of Kamakura Quant Lab. Read
[README.md](README.md) first; this page holds the things that will cost you if
you miss them.

## The one invariant

**This package must never be able to reach an exchange.** It is public. Every
change is measured against that.

Concretely, and in order of how easy each is to break by accident:

1. **No WebSocket client may enter the dependency tree, and no HTTP client of
   qate's own may enter `dependencies`.** Both live in `qate-exchanges`. Their
   absence is what makes the claim checkable with `pip list` rather than by
   review. An "optional" extra that pulls one in is the same mistake wearing a
   hat.

   The exception, and the only one: `komachi` brings `httpx`, because downloading
   purchased data is an HTTP call. It reaches one host. If you find yourself
   wanting `httpx` for anything else, what you are writing belongs in
   `qate-exchanges`.
2. **No venue endpoint, symbol mapping, or request signing belongs here.**
   `qate.core.conn` declares what a connection *is*; the implementation is an
   adapter's.
3. **No credential may have a default.** `qate.env.sys_env` raises
   `CredentialsNotFound` naming the file it looked for. It never falls back.
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
| A generic indicator, chart, or risk rule | `qate.trading` | |
| A way of reading recorded data | `qate.data` | `qate.store` |
| A way of writing a run's own output | `qate.store` | `qate.data` |

`qate.data` reads, `qate.store` writes. Market data is only ever read.

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
- **Metrics are written locally first, always.** InfluxDB is a later export, and
  `influxdb-client` is an extra. Nothing on a run's hot path may require a
  database to be reachable.
- **Metric objects pack enum *values*, not names** — `[measurement, ts, tags,
  fields]` with integer keys. `ExchangeName`, `Symbol`, `Measurement`, `Tag` and
  `Field` are therefore append-only: reordering a member silently rewrites the
  meaning of every metric file already on disk.
- **Every `date=` partition is an Asia/Tokyo day**, 15:00–14:59 UTC, and the
  timestamps inside the files are UTC epochs. `DtRange.days` already produces
  these keys; do not convert.
- **Order-book depth is read from the file's columns**, never assumed. It differs
  by venue and has changed across the archive. Absent levels arrive as `None` or
  `NaN` depending on how parquet typed the column — both have to be handled.
- **`numba` is optional, and `qate.trading.aggression` must keep working
  without it.** It pins hard against numpy's ABI and lags each new numpy release,
  so requiring it would make the package uninstallable on a current numpy for a
  speedup most readers do not need. The fallback `jit` is a pass-through; the
  decorated functions are plain loops and are correct unjitted.
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
