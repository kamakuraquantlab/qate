"""Gateway implementations: what actually turns an order request into an order.

`qate.core.gateway.ExchangeGateway` is the contract. These are the ways of
satisfying it, and there is more than one because venues differ in how their order
API behaves:

| Module | Holds |
|---|---|
| `queued` | `QueuedGateway`: `create()` enqueues, the gateway's own loop does the work |
| `default` | `DefaultGateway`: a blocking REST api on that loop, plus order reconciliation |
| `nonblocking` | `DefaultGatewayAsync`: the same, driving an async order api |

`QueuedGateway` is the reason `qate.core.gateway.ExchangeGateway` can be a plain
interface: running on a thread is a choice made here, not part of what a gateway
means. `qate.simulator` satisfies the same contract with no thread at all.

A venue adapter normally subclasses one of these and overrides the parts its
exchange does differently, rather than implementing the contract from scratch.
"""

from .default import CheckOrderStatusCallable, DefaultGateway, ReconOrdersCallable
from .nonblocking import DefaultGatewayAsync
from .queued import QueuedGateway

__all__ = [
    "CheckOrderStatusCallable",
    "DefaultGateway",
    "DefaultGatewayAsync",
    "QueuedGateway",
    "ReconOrdersCallable",
]
