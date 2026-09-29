"""The only gateway `qate` can produce on its own.

`SimulatorGateway` stands where an exchange gateway would: a strategy creates
and cancels orders through it exactly as it would against a venue, and the
simulator fills them against replayed order books. It simulates a *named*
exchange -- `SimulatorGateway(ExchangeName.GMO)` -- so the same strategy code,
symbols and tick sizes are exercised as in production.

It reaches nothing. Fills come from the order books fed into it, so a backtest
is deterministic and offline.

It fills in place. `create()` records an order and the next order book matches it,
all inside the caller's call, so a replay is single-threaded and needs no queue of
its own -- `replay.ReplayQueue` is the only one, and the trader drains it.
"""

from .gateway import DEFAULT_SLIPPAGE_RATE, SimulatorGateway
from .replay import ReplayQueue

__all__ = [
    "DEFAULT_SLIPPAGE_RATE",
    "ReplayQueue",
    "SimulatorGateway",
]
