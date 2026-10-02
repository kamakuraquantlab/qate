from qate.core.api import Api
from qate.core.ev_loop import EventLoop
from qate.core.feed import OrderFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order import OrderRequest

EVENT_CREATE_ORDER = "EVENT_CREATE_ORDER"
EVENT_CANCEL_ORDER = "EVENT_CANCEL_ORDER"


class QueuedGateway(ExchangeGateway, EventLoop):
    """An `ExchangeGateway` that defers its work to its own event loop."""

    def __init__(self, api: Api, heartbeat_interval_ts: float | None = None):
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
