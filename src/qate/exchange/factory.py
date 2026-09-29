"""Construct exchange objects by name, without naming a venue.

Same five calls the library has always made; they now resolve through
`registry` instead of importing venue modules directly, so the set of available
venues is whatever is installed rather than whatever this file enumerates.

Nothing here is on a backtest's path: a replay feeds `qate.simulator.gateway`
directly, and never asks for a venue.
"""

from qate.core.api import Api
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order_api import OrderApi

from . import registry


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


def available_exchanges() -> list[ExchangeName]:
    """Venues an adapter is installed for, simulator included."""
    return registry.registered()
