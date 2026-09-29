"""The exchange adapter contract, and the registry that finds adapters.

`qate` ships no venue. Each one arrives as a separately installed package that
registers an `ExchangeAdapter` here, and the factory resolves venues through
this registry rather than importing them.

That is the whole reason the indirection exists. A backtest reaches no venue --
it replays stored data through `qate.simulator` -- so the published package
carries no adapter, and cannot be made to reach a live exchange by
configuration alone: the code to do it is not installed. Asking for a venue in
that state raises `UnknownExchange`, which says so.

## Supplying an adapter

An adapter package declares an entry point in the `qate.exchanges` group:

```toml
[project.entry-points."qate.exchanges"]
my_venues = "my_venues:register"
```

The named object is called with no arguments on first lookup and is expected to
call `register()` once per venue it provides. Loading is lazy and happens once.

`QATE_EXCHANGE_PLUGINS` overrides discovery with a comma-separated list of
`module` or `module:attr` targets, for a checkout that is not installed.

## Partial adapters are normal

Every hook is optional. A venue that publishes public data but that nothing
trades against supplies `create_public_connection` alone; asking it for a
private connection raises, naming the venue and the hook. Missing capability is
reported where it is requested, not guessed at.
"""

import os
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from logging import getLogger
from typing import Callable

from qate.core.api import Api
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order_api import OrderApi

LOG = getLogger(__name__)

ENTRY_POINT_GROUP = "qate.exchanges"
PLUGIN_ENV_VAR = "QATE_EXCHANGE_PLUGINS"


@dataclass(frozen=True)
class ExchangeAdapter:
    """Everything one venue can supply. Every hook but the name is optional."""

    exchange_name: ExchangeName
    create_api: Callable[[str, str], Api] | None = None
    create_order_api: Callable[[str, str], OrderApi] | None = None
    create_gateway: Callable[[Api], ExchangeGateway] | None = None
    create_public_connection: Callable[[], PublicConnection] | None = None
    create_private_connection: Callable[[Api], PrivateConnection] | None = None

    def hook(self, name: str) -> Callable:
        fn = getattr(self, name)
        if fn is None:
            raise UnsupportedExchangeCapability(self.exchange_name, name)
        return fn


class UnknownExchange(Exception):
    def __init__(self, exchange_name: ExchangeName):
        known = ", ".join(sorted(x.name for x in registered())) or "none"
        super().__init__(
            f"No adapter registered for {exchange_name.name}. Registered: {known}. "
            f"Install the package providing it, or point {PLUGIN_ENV_VAR} at it."
        )


class UnsupportedExchangeCapability(Exception):
    def __init__(self, exchange_name: ExchangeName, hook: str):
        super().__init__(f"{exchange_name.name} adapter provides no {hook}")


_ADAPTERS: dict[ExchangeName, ExchangeAdapter] = {}
_discovered = False


def register(adapter: ExchangeAdapter) -> None:
    """Add an adapter. A second registration for a venue replaces the first."""
    if adapter.exchange_name in _ADAPTERS:
        LOG.warning(f"Replacing already registered adapter for {adapter.exchange_name.name}")
    _ADAPTERS[adapter.exchange_name] = adapter
    LOG.info(f"Registered exchange adapter {adapter.exchange_name.name}")


def get(exchange_name: ExchangeName) -> ExchangeAdapter:
    """The adapter for a venue, discovering installed plugins on first call."""
    discover()
    adapter = _ADAPTERS.get(exchange_name)
    if adapter is None:
        raise UnknownExchange(exchange_name)
    return adapter


def registered() -> list[ExchangeName]:
    discover()
    return list(_ADAPTERS)


def discover(force: bool = False) -> None:
    """Load adapter plugins. Idempotent; `force` re-runs it."""
    global _discovered
    if _discovered and not force:
        return
    _discovered = True

    override = os.environ.get(PLUGIN_ENV_VAR, "").strip()
    if override:
        for target in (t.strip() for t in override.split(",") if t.strip()):
            _load(target, source=PLUGIN_ENV_VAR)
        return

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ep.load()()
        except Exception:
            # One broken plugin must not take out the others, or a backtest
            # that needs no adapter at all.
            LOG.exception(f"Failed to load exchange plugin {ep.name} ({ep.value})")


def _load(target: str, source: str) -> None:
    module_name, _, attr = target.partition(":")
    try:
        module = import_module(module_name)
        getattr(module, attr or "register")()
    except Exception:
        LOG.exception(f"Failed to load exchange plugin {target} from {source}")
