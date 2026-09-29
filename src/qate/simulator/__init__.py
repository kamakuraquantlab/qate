"""The only gateway `qate` can produce on its own.

`SimulatorGateway` stands where an exchange gateway would: a strategy creates
and cancels orders through it exactly as it would against a venue, and the
simulator fills them against replayed order books. It simulates a *named*
exchange -- `SimulatorGateway(ExchangeName.GMO)` -- so the same strategy code,
symbols and tick sizes are exercised as in production.

It reaches nothing. Fills come from the order books fed into it, so a backtest
is deterministic and offline.

`replay` holds the two queues that drive it in a single thread: `ReplayQueue`,
which the trader drains, and `SyncEventQueue`, which makes the gateway match
orders in place.
"""

from .gateway import DEFAULT_SLIPPAGE_RATE, SimulatorGateway
from .replay import ReplayQueue, SyncEventQueue

__all__ = [
    "DEFAULT_SLIPPAGE_RATE",
    "ReplayQueue",
    "SimulatorGateway",
    "SyncEventQueue",
]
