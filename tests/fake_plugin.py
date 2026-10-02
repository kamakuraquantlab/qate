from qate.core.model import ExchangeName
from qate.exchange import registry


def register() -> None:
    registry.register(registry.ExchangeAdapter(exchange_name=ExchangeName.RAKUTEN))
