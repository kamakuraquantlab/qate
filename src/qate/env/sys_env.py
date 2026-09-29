"""Machine-level settings: where things are, and which credentials exist.

Everything an installation needs to know about its own machine, in one place, so
that no other module carries a path. There are no hardcoded locations here: each
answer comes from a config file, an environment variable, or a documented
default under the user's home directory.

## The one root

`get_env_root_dir()` is where trading environments live -- one directory per
`EnvName`, holding its config, parameter grid, logs and results. It defaults to
`~/env`, is overridden by `QATE_ENV_ROOT`, and is overridden again by a
`.qate.json` sitting next to the running script, which is how a checkout keeps
its own environments beside itself instead of in `$HOME`.

There is deliberately no market-data root here. `qate` does not read market data
and does not know where it lives; the tool that downloaded it does.

## Credentials

`~/.qate/<service>.keys` is an INI file, one section per named key set:

```ini
[DEFAULT]
API_KEY = ...
SECRET  = ...
```

A missing file raises `CredentialsNotFound` naming the path it looked for. That
is the useful failure: nothing in this library invents a credential or falls
back to an unauthenticated call, so a backtest -- which asks for none -- is
never affected by their absence, and a live run fails at startup rather than
part way through.
"""

import configparser
import json
import os
import sys
from pathlib import Path

from .env import Env
from .env_name import EnvName

DEFAULT_ENV_ROOT = Path.home() / "env"
DEFAULT_SECRET_DIR = Path.home() / ".qate"

ENV_ROOT_VAR = "QATE_ENV_ROOT"
SECRET_DIR_VAR = "QATE_SECRET_DIR"
CONFIG_FILE_NAME = ".qate.json"


class CredentialsNotFound(Exception):
    def __init__(self, path: Path, section: str):
        super().__init__(
            f"No credentials for section [{section}] in {path}. "
            f"Create the file, or set {SECRET_DIR_VAR} to the directory holding it."
        )


def _script_config() -> dict:
    """`.qate.json` beside the running script, if there is one."""
    if not sys.argv:
        return {}
    config_file = Path(sys.argv[0]).parent / CONFIG_FILE_NAME
    if not config_file.is_file():
        return {}
    try:
        return json.loads(config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def get_env_root_dir() -> Path:
    """Where trading environments live. Script config, then env var, then `~/env`."""
    from_script = _script_config().get("env_root_dir")
    if from_script:
        return Path(from_script).expanduser()
    from_env = os.environ.get(ENV_ROOT_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_ENV_ROOT


def get_secret_dir() -> Path:
    from_script = _script_config().get("secret_dir")
    if from_script:
        return Path(from_script).expanduser()
    from_env = os.environ.get(SECRET_DIR_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_SECRET_DIR


def _read_keys(service_name: str, section: str, *fields: str) -> tuple[str, ...]:
    path = get_secret_dir() / f"{service_name.lower()}.keys"
    config = configparser.ConfigParser()
    config.read(path)
    if section not in config:
        raise CredentialsNotFound(path, section)
    entry = config[section]
    missing = [f for f in fields if f not in entry]
    if missing:
        raise CredentialsNotFound(path, section)
    return tuple(entry[f] for f in fields)


def get_api_keys(service_name: str, key_name: str = "DEFAULT") -> tuple[str, str]:
    """An exchange's `(api_key, secret)` from `<service>.keys`."""
    return _read_keys(service_name, key_name, "API_KEY", "SECRET")


def get_influxdb(key_name: str = "DEFAULT") -> tuple[str, str, str]:
    """`(token, write_url, read_url)` for the metrics export, from `influxdb.keys`."""
    return _read_keys("influxdb", key_name, "TOKEN", "WRITE_URL", "READ_URL")


def load_env(env_name: EnvName) -> Env:
    """The environment directory. Reading its files is `qate.boot.load_trading_env`."""
    return Env(get_env_root_dir(), env_name)
