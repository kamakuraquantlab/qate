"""The event-loop gateway: `create()` enqueues, a thread does the work.

This is what every live venue adapter is built on, and it is the reason the
gateway contract used to carry a thread. Talking to an exchange means blocking on
a socket, and a strategy must not block, so the gateway runs its own loop:
`create()` puts an event on the queue and returns immediately, the loop picks it up
and calls the api, and the result is published back through `OrderFeed`.

It lives here rather than in `qate.core` because it is one way of satisfying
`ExchangeGateway`, not the meaning of it. A simulator satisfies the same contract
with no thread at all.

Subclasses implement `handle_create_order` and `handle_cancel_order`, which run on
the loop's thread. `DefaultGateway` is the one that calls a blocking REST api;
`DefaultGatewayAsync` drives an async one.
"""

from qate.core.api import Api
from qate.core.ev_loop import EventLoop
from qate.core.feed import OrderFeed
from qate.core.gateway import EVENT_CANCEL_ORDER, EVENT_CREATE_ORDER, ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order import OrderRequest


class QueuedGateway(ExchangeGateway, EventLoop):
    """An `ExchangeGateway` that defers its work to its own event loop."""

    def __init__(self, api: Api, heartbeat_interval_ts: float = None):
        # Each base once, by name. This used to run EventLoop, StatusFeed and
        # Thread twice: the second call entered OrderFeed, whose cooperative
        # `super()` continued along this instance's MRO and reached EventLoop again.
        EventLoop.__init__(self, None, heartbeat_interval_ts)
        OrderFeed.__init__(self)
        self.api = api

        self.register(EVENT_CREATE_ORDER, self.handle_create_order)
        self.register(EVENT_CANCEL_ORDER, self.handle_cancel_order)

    @property
    def exchange_name(self) -> ExchangeName:
        return self.api.exchange_name

    def create(self, order_request: OrderRequest) -> None:
        self.put(EVENT_CREATE_ORDER, order_request)

    def cancel(self, order_request: OrderRequest) -> None:
        self.put(EVENT_CANCEL_ORDER, order_request)

    def handle_create_order(self, order_request: OrderRequest) -> None:
        """Runs on the loop's thread. Blocking here is expected."""

    def handle_cancel_order(self, order_request: OrderRequest) -> None:
        """Runs on the loop's thread. Blocking here is expected."""
