"""Gateway implementations: what actually turns an order request into an order.

`qate.core.gateway.ExchangeGateway` is the contract. These are the ways of
satisfying it, and there is more than one because venues differ in how their order
API behaves:

| Module | Holds |
|---|---|
| `default` | `DefaultGateway`: a blocking REST api, plus order reconciliation |
| `nonblocking` | `DefaultGatewayAsync`: the same, for a venue with an async order api |

A venue adapter normally subclasses one of these and overrides the parts its
exchange does differently, rather than implementing the contract from scratch.
"""

from .default import CheckOrderStatusCallable, DefaultGateway, ReconOrdersCallable
from .nonblocking import DefaultGatewayAsync

__all__ = [
    "CheckOrderStatusCallable",
    "DefaultGateway",
    "DefaultGatewayAsync",
    "ReconOrdersCallable",
]
