"""Lazy discovery of what an installed package offers this one.

`qate` ships no venue and no fee rate, so twice over it has to ask what is
installed: `qate.exchange.registry` for adapters, `qate.trading.fee` for fee
schedules. Both want the same three behaviours, which is why they share this rather
than each having a copy to drift from:

- an entry-point group, loaded once, on first use and not at import
- a comma-separated environment override naming `module` or `module:attr`, for a
  checkout that is not installed
- one broken plugin logs and the rest still load, because a backtest that needs no
  plugin at all must not be taken down by someone else's bad install

The override *replaces* entry-point discovery rather than adding to it. That is what
makes it useful for testing a checkout in isolation.
"""

import os
from importlib import import_module
from importlib.metadata import entry_points
from logging import Logger


def load_plugins(group: str, env_var: str, log: Logger, default_attr: str = "register") -> None:
    """Call every plugin hook in `group`, or those named by `env_var` if it is set.

    Each hook is called with no arguments and is expected to register whatever it
    provides. Exceptions are logged, never raised: the caller is resolving an
    optional capability, and a plugin that cannot load is the absence of that
    capability rather than a failure of the process.
    """
    override = os.environ.get(env_var, "").strip()
    if override:
        for target in (t.strip() for t in override.split(",") if t.strip()):
            _load(target, source=env_var, default_attr=default_attr, log=log)
        return

    for ep in entry_points(group=group):
        try:
            ep.load()()
        except Exception:
            log.exception(f"Failed to load {group} plugin {ep.name} ({ep.value})")


def _load(target: str, source: str, default_attr: str, log: Logger) -> None:
    module_name, _, attr = target.partition(":")
    try:
        module = import_module(module_name)
        getattr(module, attr or default_attr)()
    except Exception:
        log.exception(f"Failed to load plugin {target} from {source}")
