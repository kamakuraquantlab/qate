from collections import deque
from collections.abc import Iterable, Iterator

from qate.core.ev_q import EventQueue
from qate.core.model import TimeSeriesData

Event = tuple[str, TimeSeriesData]


class ReplayQueue(EventQueue):
    def __init__(self, events: Iterable[Event]):
        self._events: Iterator[Event] = iter(events)
        self._trading_events: deque[Event] = deque()

    def put(self, event: Event) -> None:
        self._trading_events.append(event)

    def get(self, timeout=0) -> Event | None:
        if self._trading_events:
            return self._trading_events.popleft()
        return next(self._events, None)
