"""A stand-in adapter package, for the plugin-discovery tests.

It registers one venue and supplies no hooks, which is all the discovery path
needs to be observable.
"""

from qate.core.model import ExchangeName
from qate.exchange import registry


def register() -> None:
    registry.register(registry.ExchangeAdapter(exchange_name=ExchangeName.RAKUTEN))
