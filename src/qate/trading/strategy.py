from logging import getLogger
from typing import Generic, TypeVar

from qate.core.ev_type import EventType
from qate.core.feed import StatusFeed
from qate.core.gateway import ExchangeGateway
from qate.trading.metrics import FeedWriter
from qate.core.model import (
    EventLoopExit,
    Metric,
    OrderBook,
    SettleType,
    Trade,
)
from qate.trading.exceptions import StopTradingException
from qate.core.order import OrderError, OrderResponse

LOG = getLogger(__name__)

C = TypeVar("C")


class Strategy(StatusFeed, Generic[C]):
    def __init__(self, config: C = None, params: dict = None):
        super(Strategy, self).__init__()
        self._is_ready = False
        self.feed_writer = FeedWriter(self)
        self._open_order_failures = 0
        self.config: C = config
        self.params = params if params is not None else {}

    def get_param_multiplier(self, name: str) -> int:
        # Multiplier to convert a float param into a compact integer for id generation.
        # e.g. with factor=100000: spread_threshold=0.00005 → 5, volatility_threshold=0.0002 → 20
        # producing an id segment like "5_20". Override per strategy to match param scale.
        return 1000

    def get_param_set_id(self) -> str:
        # Mostly for offline analysis
        # Produces a compact string id from all params, e.g. "5_20" for two params.
        # Each param value is scaled by its multiplier to strip the decimal point.
        # Subclasses should override get_param_multiplier() to match their param scale,
        # and may override this method entirely to include config fields (e.g. order_size).
        return "_".join([str(int(self.get_param_multiplier(k) * v)) for k, v in list(self.params.items())])

    def handle_order_book(self, order_book: OrderBook):
        pass

    def handle_trade(self, trade: Trade):
        pass

    def handle_bar(self, bar):
        pass

    def handle_order_created(self, order_response: OrderResponse):
        LOG.info(f"ORDER {order_response.summary}")
        if order_response.order_request.settle_type == SettleType.OPEN:
            self._open_order_failures = 0

    def handle_order_cancelled(self, order_response: OrderResponse):
        LOG.info(f"ORDER {order_response.summary}")

    def handle_order_filled(self, order_response: OrderResponse):
        LOG.info(f"ORDER {order_response.summary}")

    def handle_order_error(self, order_response: OrderResponse):
        LOG.info(f"ORDER_ERROR {order_response.summary}")
        if order_response.error == OrderError.CREATE:
            order_request = order_response.order_request
            if order_request.settle_type == SettleType.CLOSE:
                raise StopTradingException("Close order creation failure")

            if order_request.settle_type == SettleType.OPEN:
                self._open_order_failures += 1
                LOG.warning(f"Open order failed to create. count={self._open_order_failures}")
                if self._open_order_failures >= 2:
                    raise StopTradingException("Open order creation failures exceeded threshold")
        elif order_response.error == OrderError.CANCEL:
            raise StopTradingException("Order cancel failure")

    def handle_msg(self, msg: str):
        pass

    def before_loop(self):
        pass

    def after_loop(self):
        self.feed_writer.close()

    def add_gateway(self, gateway: ExchangeGateway):
        pass

    def warmup_order_book(self, order_book: OrderBook):
        pass

    def warmup_trade(self, trade: Trade):
        pass

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    def add_metric(self, metric: Metric, flush_now: bool = False):
        """Emit one metric. Where it ends up is the runner's decision, not yours."""
        self.feed_writer.add(metric)
        if flush_now:
            self.feed_writer.flush()

    def publish_pnl_update(self, pnl_update):
        self.publish_status(EventType.TRADING_PNL_UPDATE, pnl_update)

    def handle_stop(self, _):
        raise EventLoopExit()

    def update_params(self, new_params: dict) -> bool:
        current_keys = set(self.params.keys())
        new_keys = set(new_params.keys())
        if current_keys != new_keys:
            LOG.warning("Param keys mismatch; skip update. current=%s new=%s", current_keys, new_keys)
            return False

        if all(self.params[k] == new_params[k] for k in current_keys):
            # No changes
            return False

        LOG.info("Strategy params update BEFORE %s", self.params)
        LOG.info("Strategy params update AFTER %s", new_params)
        self.params = new_params
        return True
