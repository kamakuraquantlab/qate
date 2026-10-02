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
    adapter = registry.find(market.exchange_name)
    if adapter is None:
        return None
    return adapter.fee_rates.get(market.symbol)


def available_exchanges() -> list[ExchangeName]:
    return registry.registered()
