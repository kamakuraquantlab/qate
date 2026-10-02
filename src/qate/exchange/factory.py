"""Construct exchange objects by name, without naming a venue.

Same five calls the library has always made; they now resolve through
`registry` instead of importing venue modules directly, so the set of available
venues is whatever is installed rather than whatever this file enumerates.

Nothing that *builds* anything is on a backtest's path: a replay feeds
`qate.trading.gateways.simulator` directly and never asks for a venue object.
`get_fee_rate` is the one call a replay does make, and it constructs nothing -- it
reads a number an adapter declared. `test_backtest_end_to_end.py` holds that line.
"""

from logging import getLogger

from qate.core.api import Api
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market
from qate.core.order_api import OrderApi

from . import registry
from .registry import FeeSchedule

LOG = getLogger(__name__)


def create_exchange_api(exchange_name: ExchangeName, api_key: str, secret: str) -> Api:
    return registry.get(exchange_name).hook("create_api")(api_key, secret)


def create_exchange_order_api(exchange_name: ExchangeName, api_key: str, secret: str) -> OrderApi:
    return registry.get(exchange_name).hook("create_order_api")(api_key, secret)


def create_exchange_gateway(exchange_name: ExchangeName, api_key: str, secret: str) -> ExchangeGateway:
    adapter = registry.get(exchange_name)
    api = adapter.hook("create_api")(api_key, secret)
    return adapter.hook("create_gateway")(api)


def create_public_connection(exchange_name: ExchangeName) -> PublicConnection:
    return registry.get(exchange_name).hook("create_public_connection")()


def create_private_connection(api: Api) -> PrivateConnection:
    return registry.get(api.exchange_name).hook("create_private_connection")(api)


def get_fee_rate(market: Market) -> FeeSchedule | None:
    """What a venue charges on one market, or `None` if nobody says.

    `None` covers all three ways of not knowing -- no adapter installed for the venue,
    an adapter that declares no rates, an adapter that declares rates but not for this
    symbol -- because the caller does the same thing in each case and a run must not
    fail over a rate. `PnlTracker.register_fee` is the caller; it costs an unknown
    market at zero and says so once.

    Unlike every other call here this builds nothing and connects to nothing, which is
    what lets a replay ask it.
    """
    adapter = registry.find(market.exchange_name)
    if adapter is None:
        return None
    return adapter.fee_rates.get(market.symbol)


def available_exchanges() -> list[ExchangeName]:
    """Venues an adapter is installed for, simulator included."""
    return registry.registered()
