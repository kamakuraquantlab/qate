"""The queue a replay is driven through.

A live run is several threads passing events over real queues: a WebSocket
thread fills one, the trader drains it, the gateway has its own. A replay has no
reason to be concurrent -- the events already exist, in order -- and every
reason not to be, since two runs of the same data must produce the same result.

`ReplayQueue` is what the trader drains. It hands out recorded market events in
order, and the strategy's own events ahead of them, so the same `Trader` and
`Strategy` code runs unchanged.

There used to be a second class here, `SyncEventQueue`, which made a gateway
dispatch in place rather than on its own thread. It existed because the gateway
contract extended `EventLoop`, so a backtest had to hand the gateway a fake queue
to get a fill computed inside the strategy's own call. The contract is a plain
interface now and `SimulatorGateway` fills directly, so there is nothing left to
defeat.
"""

from collections import deque
from typing import Iterable, Iterator

from qate.core.ev_q import EventQueue
from qate.core.model import TimeSeriesData

Event = tuple[str, TimeSeriesData]


class ReplayQueue(EventQueue):
    """Recorded market events, with the strategy's own events taking priority.

    Order matters and is the whole content of this class. An order response --
    created, filled, cancelled -- must reach the strategy before the next market
    event does. Otherwise a strategy is shown a new book while still believing an
    order is open, reacts to it, and the backtest measures a strategy nobody
    wrote. Live, the same ordering comes for free: the gateway's response is
    already in the queue before the next tick arrives from the venue.

    Exhausting the market events returns `None`, which is what `EventLoop` reads
    as "stop" -- so a replay ends when the data does, with no sentinel to append.
    """

    def __init__(self, events: Iterable[Event]):
        self._events: Iterator[Event] = iter(events)
        self._trading_events: deque[Event] = deque()

    def put(self, event: Event) -> None:
        self._trading_events.append(event)

    def get(self, timeout=0) -> Event | None:
        if self._trading_events:
            return self._trading_events.popleft()
        return next(self._events, None)
