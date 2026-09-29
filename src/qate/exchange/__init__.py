"""Exchange resolution: the adapter contract, the registry, and the factory.

No venue implementation lives in this package. See `registry` for how one is
supplied and why it is kept outside.
"""

from .factory import (
    available_exchanges,
    create_exchange_api,
    create_exchange_gateway,
    create_exchange_order_api,
    create_private_connection,
    create_public_connection,
)
from .registry import (
    ExchangeAdapter,
    UnknownExchange,
    UnsupportedExchangeCapability,
    register,
)

__all__ = [
    "ExchangeAdapter",
    "UnknownExchange",
    "UnsupportedExchangeCapability",
    "available_exchanges",
    "create_exchange_api",
    "create_exchange_gateway",
    "create_exchange_order_api",
    "create_private_connection",
    "create_public_connection",
    "register",
]
