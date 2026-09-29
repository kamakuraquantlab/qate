"""Every class initialises each of its bases exactly once, and ends up whole.

`qate.core.feed`'s mixins do not chain, because they are mixed in beside
`threading.Thread`, which is not cooperative. The cost of that choice is that a
class combining them has to initialise each by name, and forgetting one gives an
`AttributeError` the first time something publishes -- not at construction. This
file is what makes that a test failure instead.

It also catches the opposite mistake, which is the one that was actually there:
`QueuedGateway` initialised `EventLoop`, `StatusFeed` and `Thread` twice per
gateway, because its second explicit base call entered a mixin whose cooperative
`super()` continued along the instance's MRO and arrived back at `EventLoop`. A
queue was built and discarded and the handler table was cleared and refilled on
every construction.
"""

import collections
import threading

import pytest

from qate.core import ev_loop, feed
from qate.core.api import Api
from qate.core.model import ExchangeName
from qate.simulator import SimulatorGateway
from qate.trading.gateways import DefaultGateway, QueuedGateway
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

BASES = (
    feed.StatusFeed,
    feed.OrderFeed,
    feed.MarketDataFeed,
    feed.ExchangeFeed,
    ev_loop.EventLoop,
    threading.Thread,
)

# What each mixin is responsible for. A class that mixes one in and never
# initialises it is missing these.
STATE = {
    feed.StatusFeed: ["status_listeners"],
    feed.OrderFeed: ["order_listeners"],
    feed.MarketDataFeed: ["order_book_listeners", "trade_listeners", "subscribe_event_types"],
    feed.ExchangeFeed: ["exchange_execution_listeners", "exchange_order_listeners"],
}


class FakeApi(Api):
    exchange_name = ExchangeName.GMO


@pytest.fixture
def init_counts(monkeypatch):
    """Count `__init__` calls per instance, for every base that has state."""
    counts: dict[int, list[str]] = collections.defaultdict(list)
    alive: list = []  # hold references, or a freed object's id gets reused

    for cls in BASES:
        original = cls.__init__

        def counting(self, *args, _cls=cls, _original=original, **kwargs):
            alive.append(self)
            counts[id(self)].append(_cls.__name__)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", counting)

    def check(obj) -> list[str]:
        alive.append(obj)
        return counts[id(obj)]

    return check


CASES = [
    ("Strategy", lambda: Strategy(), [feed.StatusFeed]),
    ("Reporter", lambda: Reporter(), [feed.StatusFeed]),
    ("Trader", lambda: Trader(Strategy()), [feed.StatusFeed]),
    ("QueuedGateway", lambda: QueuedGateway(FakeApi()), [feed.StatusFeed, feed.OrderFeed]),
    ("DefaultGateway", lambda: DefaultGateway(FakeApi()), [feed.StatusFeed, feed.OrderFeed]),
    ("SimulatorGateway", lambda: SimulatorGateway(ExchangeName.GMO), [feed.OrderFeed]),
]


@pytest.mark.parametrize("label,make,mixins", CASES, ids=[c[0] for c in CASES])
def test_each_base_is_initialised_exactly_once(label, make, mixins, init_counts, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    obj = make()

    calls = init_counts(obj)
    duplicated = sorted({name for name in calls if calls.count(name) > 1})
    assert not duplicated, f"{label} initialises {duplicated} more than once: {calls}"


@pytest.mark.parametrize("label,make,mixins", CASES, ids=[c[0] for c in CASES])
def test_every_mixin_it_uses_is_initialised(label, make, mixins, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    obj = make()

    for mixin in mixins:
        for attribute in STATE[mixin]:
            assert hasattr(obj, attribute), f"{label} never initialised {mixin.__name__}: no {attribute}"


def test_a_thread_backed_class_is_startable(tmp_path, monkeypatch):
    """Thread.__init__ ran, and only once, so the thread object is sound."""
    monkeypatch.chdir(tmp_path)
    gateway = DefaultGateway(FakeApi())
    assert gateway.name  # Thread.__init__ assigns one
    assert not gateway.is_alive()
    gateway.start()
    try:
        assert gateway.is_alive()
    finally:
        gateway.stop()
        gateway.join(timeout=5)
    assert not gateway.is_alive()


def test_a_gateway_builds_one_event_queue(tmp_path, monkeypatch):
    """The double init built a queue, threw it away and built another."""
    monkeypatch.chdir(tmp_path)
    built = []
    original = ev_loop.create_event_queue

    def counting():
        queue = original()
        built.append(queue)
        return queue

    monkeypatch.setattr(ev_loop, "create_event_queue", counting)
    gateway = DefaultGateway(FakeApi())

    assert len(built) == 1, f"built {len(built)} queues for one gateway"
    assert gateway.event_queue is built[0]


def test_a_gateway_keeps_the_handlers_registered_during_construction(tmp_path, monkeypatch):
    """The second init cleared `handlers`; anything registered before it was lost."""
    monkeypatch.chdir(tmp_path)
    from qate.core.gateway import EVENT_CANCEL_ORDER, EVENT_CREATE_ORDER

    gateway = DefaultGateway(FakeApi())
    assert EVENT_CREATE_ORDER in gateway.handlers
    assert EVENT_CANCEL_ORDER in gateway.handlers
    assert ev_loop.EVENT_ADD_TASK in gateway.handlers
    # The reconciliation task DefaultGateway schedules in its own __init__ survived.
    assert gateway.event_queue.qsize() == 1
