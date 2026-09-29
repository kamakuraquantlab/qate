# Env System

The `Env` class (`qate.env.env`) is a **directory-based environment unit**. Each named environment
maps to a folder under the env root — `~/env` by default, overridden by
`QATE_ENV_ROOT` or by `env_root_dir` in a `.qate.json` beside the running script.
See `qate.env.sys_env`.

`EnvName` (`qate.env.env_name`) is a validated `str` subclass — any uppercase `[A-Z_]+` string is a
valid env name. It used to be an `Enum` with a fixed member set; it was changed to a string so new
environments can be created without editing `env_name.py`. Because it *is* a real `str`, it round-trips
through JSON, path joins, dict keys and f-strings with no `.name` unwrapping. Construct/validate with
`EnvName("MY_BACKTEST")` (raises `ValueError` on invalid input), and use it directly as an argparse
`type=EnvName` to validate CLI input.

Names are a convention, not a registry: pick something memorable per deployment
and keep one environment per running process.

## 1 Directory layout

```
{ENV_DIR}/{EnvName}/
  desc.json          # name + list of module keys to load
  {key}.json         # typed config objects (one file per module key)
  .lock              # PID-based process lock (fcntl); prevents double-start
  env.log            # rotating log file (midnight rotation)
  start_{name}.sh    # auto-generated on first daemon run
  stop_{name}.sh     # auto-generated on first daemon run
```

## 2 Key methods

| Method | What it does |
|--------|-------------|
| `env.visit()` | `cd` to `work_dir` + configure logging to `env.log` |
| `env.enter(daemon=False)` | `visit()` + acquire `.lock`; if `daemon=True` and scripts don't exist yet, generates `start_*.sh` / `stop_*.sh` and exits |
| `env.leave()` | Release lock + delete `.lock` |
| `env.load()` | Load `desc.json` + all configured modules |
| `env.load_object(key, cls)` | Deserialize `{key}.json` → dataclass via `dacite` |
| `env.save_object(key, obj)` | Serialize dataclass → `{key}.json` |

## 3 Logging

`visit()` attaches a `TimedRotatingFileHandler` (midnight rotation) to the root logger.
Format: `{asctime} {levelname:8s} {name} {message}`

When multiple processes share an env, pass `enable_multiprocess_logging=True` to get
per-PID log files (`env_{PID}.log`) instead of a shared `env.log`.

## 4 Daemon scripts

On the first call to `enter(daemon=True)` the env generates `start_{name}.sh` and
`stop_{name}.sh` in the work dir using `nohup`, then **exits** so the operator can review them.
On subsequent calls it acquires the lock and runs normally.

## 5 Config discovery

`qate.boot.config.load_trading_env(env)` reads an environment and returns a
`TradingEnv`: the profile, the config, the params, the grid, the feature flags. It
names the classes it wants rather than being told:

```python
from qate.boot.config import load_trading_env

definition = load_trading_env(env)      # nothing entered, nothing locked
definition.profile.strategy_name        # "my_strategy.v1"
definition.config.get_markets()
definition.params                       # what one run uses
```

The one class that cannot be known in advance is the strategy's own `Config`, and
`trading.json` already names the strategy module, so it is resolved by convention:
`<strategy_module>.config.Config`. That is the layout
[03_writing_strategy.md](03_writing_strategy.md) documents.

`env.load_object(file_name, cls)` is the single-file version, and what
`load_trading_env` is built from. Type hooks are pre-registered for `ExchangeName`,
`Symbol`, `Side`, `SettleType`, `Market` and `EnvName`, so those fields deserialize
from their string forms; for `EnvName` the hook re-validates.

### 5.1 `desc.json` and its module map

`desc.json` used to carry a map from each file to the class that loads it:

```json
{
  "name": "SOME_ENV",
  "modules": {"config": "my_strategy.config.Config", "params": "builtins.dict"}
}
```

`Env.load()` still reads it, and nothing in the library requires it any more. The
map bought no flexibility -- every reader already knew it wanted a
`TradingProfile` and a dict -- and it cost a file that goes stale the moment code
moves, pointing at a class that no longer exists while the code it describes works
fine. Prefer `load_trading_env`, and let `desc.json` say only which environment
this is.

## 6 Where a run writes

An environment is a *definition*. A process that runs it also produces output --
logs, results, a lock -- and the two do not have to share a directory. `Env` takes
a `work_dir` explicitly for that reason:

```python
definition = Env(root, name)                                  # read from here
output = Env(root, name, work_dir=Path("~/backtests") / name)  # write here
output.enter()                                                # chdir, log, lock
```

That is how one environment can serve a live process and a backtest at the same
time: the backtest reads the config files, takes its own lock somewhere else, and
never writes into the directory the live process is using.

## 6 Typical usage pattern

```python
env = Env(sys_env.get_env_root_dir(), EnvName("SOME_ENV"))
env.enter(daemon=False)   # or daemon=True for background service
config = env.load_object("config", MyConfig)
# ... run the strategy ...
env.leave()
```
