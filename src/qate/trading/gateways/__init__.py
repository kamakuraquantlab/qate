"""Gateway implementations: what actually turns an order request into an order.

`qate.core.gateway.ExchangeGateway` is the contract. These are the ways of
satisfying it, and there is more than one because venues differ in how their order
API behaves:

| Module | Holds |
|---|---|
| `queued` | `QueuedGateway`: `create()` enqueues, the gateway's own loop does the work |
| `default` | `DefaultGateway`: a blocking REST api on that loop, plus order reconciliation |
| `nonblocking` | `DefaultGatewayAsync`: the same, driving an async order api |
| `simulator` | `SimulatorGateway`: fills against recorded books, no thread and no queue |

`QueuedGateway` is the reason `ExchangeGateway` can be a plain interface: running
on a thread is a choice made here, not part of what a gateway means. `simulator`
is the proof -- it satisfies the same contract with no thread at all, and it is the
only one of the four this package can produce on its own. The other three need an
`Api`, and every `Api` implementation lives in `qate-exchanges`.

A venue adapter normally subclasses one of the first three and overrides the parts
its exchange does differently, rather than implementing the contract from scratch.
"""

from .default import CheckOrderStatusCallable, DefaultGateway, ReconOrdersCallable
from .nonblocking import DefaultGatewayAsync
from .queued import QueuedGateway
from .simulator import DEFAULT_SLIPPAGE_RATE, SimulatorGateway

__all__ = [
    "DEFAULT_SLIPPAGE_RATE",
    "CheckOrderStatusCallable",
    "DefaultGateway",
    "DefaultGatewayAsync",
    "QueuedGateway",
    "ReconOrdersCallable",
    "SimulatorGateway",
]
