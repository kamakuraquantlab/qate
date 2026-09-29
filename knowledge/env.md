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

`desc.json` is the index of the environment. It contains a `Description` object:

```json
{
  "name": "SOME_ENV",
  "modules": {
    "config": "qate.strategy.pisces.config.Config",
    "params": "builtins.dict"
  }
}
```

`modules` maps a key to a fully-qualified class name. When `env.load()` is called, it reads
`desc.json`, then for each key loads `{key}.json` from the work dir and deserializes it into
the declared class using `dacite.from_dict()`. All loaded objects are kept in an internal
dict.

Apps retrieve objects by key:

```python
env.load()
config = env.get("config")   # returns a Config instance
params = env.get("params")   # returns a dict
```

Type hooks are pre-registered for `ExchangeName`, `Symbol`, `Side`, `SettleType`, `Market`,
and `EnvName` so those fields deserialize automatically from their string representations.
For `EnvName` the hook is `EnvName(x)`, which re-validates the string on load.

## 6 Typical usage pattern

```python
env = Env(sys_env.get_env_root_dir(), EnvName("SOME_ENV"))
env.enter(daemon=False)   # or daemon=True for background service
config = env.load_object("config", MyConfig)
# ... run the strategy ...
env.leave()
```
