import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from logging import getLogger

from qate.core.api import Api
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order import OrderType
from qate.core.order_api import OrderApi
from qate.core.symbol import Symbol

LOG = getLogger(__name__)

ENTRY_POINT_GROUP = "qate.exchanges"
PLUGIN_ENV_VAR = "QATE_EXCHANGE_PLUGINS"


@dataclass(frozen=True)
class FeeSchedule:
    maker: float
    taker: float

    def rate(self, order_type: OrderType) -> float:
        return self.maker if order_type == OrderType.MAKER else self.taker


@dataclass(frozen=True)
class ExchangeAdapter:
    exchange_name: ExchangeName
    create_api: Callable[[str, str], Api] | None = None
    create_order_api: Callable[[str, str], OrderApi] | None = None
    create_gateway: Callable[[Api], ExchangeGateway] | None = None
    create_public_connection: Callable[[], PublicConnection] | None = None
    create_private_connection: Callable[[Api], PrivateConnection] | None = None
    fee_rates: dict[Symbol, FeeSchedule] = field(default_factory=dict)

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
    if adapter.exchange_name in _ADAPTERS:
        LOG.warning(f"Replacing already registered adapter for {adapter.exchange_name.name}")
    _ADAPTERS[adapter.exchange_name] = adapter
    LOG.info(f"Registered exchange adapter {adapter.exchange_name.name}")


def get(exchange_name: ExchangeName) -> ExchangeAdapter:
    discover()
    adapter = _ADAPTERS.get(exchange_name)
    if adapter is None:
        raise UnknownExchange(exchange_name)
    return adapter


def find(exchange_name: ExchangeName) -> ExchangeAdapter | None:
    discover()
    return _ADAPTERS.get(exchange_name)


def registered() -> list[ExchangeName]:
    discover()
    return list(_ADAPTERS)


def discover(force: bool = False) -> None:
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
