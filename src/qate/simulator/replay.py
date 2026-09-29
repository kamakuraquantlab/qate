"""The two queues a replay is driven through.

A live run is several threads passing events over real queues: a WebSocket
thread fills one, the trader drains it, the gateway has its own. A replay has no
reason to be concurrent -- the events already exist, in order -- and every
reason not to be, since two runs of the same data must produce the same result.

These are the two queue implementations that collapse that machinery into one
thread, so the same `Trader`, `Strategy` and gateway code runs unchanged.

`SyncEventQueue` makes a gateway dispatch in place: putting an event calls its
handler directly instead of waking a thread.

`ReplayQueue` is what the trader drains. It hands out recorded market events in
order, and the strategy's own events ahead of them.
"""

from collections import deque
from typing import Iterable, Iterator

from qate.core.ev_q import EventQueue
from qate.core.model import TimeSeriesData

Event = tuple[str, TimeSeriesData]


class SyncEventQueue(EventQueue):
    """Dispatch into a loop's handlers immediately, with no thread and no buffer.

    Handed to a `SimulatorGateway` in place of its queue so that creating an
    order is matched, and its response published, within the strategy's own call.
    A queue that had to be drained by a running loop would leave the order
    pending until the next tick, which is not what happens on an exchange and
    not something a strategy should have to model.

    `get` is not implemented: nothing drains this, by design.
    """

    def __init__(self, handlers: dict[str, list]):
        self.handlers = handlers

    def get(self, timeout=None) -> Event:
        raise NotImplementedError("SyncEventQueue dispatches on put; nothing reads it")

    def put(self, event: Event) -> None:
        (event_type, event_data) = event
        for handler in self.handlers.get(event_type, ()):
            handler(event_data)


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
