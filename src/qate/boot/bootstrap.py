from logging import getLogger

from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import SettleType
from qate.core.order import OrderResponse
from qate.core.conn import Connection
from qate.store.metrics import MsgpackWriter, RotationInterval, WriterObject
from qate.trading.chat import Chat
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader
from qate.util.counter import Stat

LOG = getLogger(__name__)


class Bootstrap(EventLoop):
    """
    A class that does everything except trading
    """

    def __init__(self, strategy: Strategy):
        super(Bootstrap, self).__init__(None, heartbeat_interval=5)
        self.trader = Trader(strategy)
        self.trader.add_status_listener(self.event_queue)
        self.gateways: list[ExchangeGateway] = []
        self.chat: Chat = None
        self.metrics_writer = MsgpackWriter(RotationInterval.FIVE_MINUTE, 256, "Metrics")

        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)
        self.register(EventType.ORDER_FILLED, self.handle_order)
        self.register(EventType.METRICS, self.handle_metrics)
        self.register(EventType.TRADING_PNL_UPDATE, self.handle_pnl_update)
        self.add_status_listener(self.event_queue)

        self.conns: list[Connection] = []
        self.stats: dict[str, Stat] = {}
        self.should_exit = False

    def add_conn(self, conn: Connection):
        self.conns.append(conn)
        conn.add_status_listener(self.event_queue)
        if isinstance(conn, MarketDataFeed):
            self.trader.subscribe_market_data_feed(conn)

    def add_gateway(self, gateway: ExchangeGateway):
        gateway.add_order_listener(self.event_queue)
        gateway.add_status_listener(self.event_queue)
        self.gateways.append(gateway)
        self.trader.add_gateway(gateway)

    def set_chat(self, chat: Chat):
        self.chat = chat
        # To chat
        self.register(EventType.EXCEPTION, self.chat.send)
        self.register(EventType.MSG_OUT, self.chat.send)
        self.register(EventType.ORDER_FILLED, self.chat.send)
        # From chat
        self.chat.add_status_listener(self.event_queue)
        self.register(EventType.MSG_IN, self.trader.notify)

    def before_loop(self):
        if self.chat:
            LOG.info(f"chat {self.chat.__class__.__name__} connect")
            self.chat.connect()
        if self.trader:
            LOG.info(f"trader {self.trader.__class__.__name__} start")
            self.trader.start()
        for gateway in self.gateways:
            LOG.info(f"gateway {gateway.__class__.__name__} {gateway.exchange_name.name} start")
            gateway.start()
        for conn in self.conns:
            conn.connect()

        if self.chat:
            self.chat.send("TRADING STARTED")

    def after_loop(self):
        LOG.warning(f"{self.__class__.__name__} metrics writer close {len(self.metrics_writer.buffer)}")
        self.metrics_writer.close()

    def _get_stat(self, key: str):
        if key not in self.stats:
            stat = Stat()
            self.stats[key] = stat
        return self.stats[key]

    def handle_order(self, order_response: OrderResponse):
        metric_object = order_response.to_metric_object()
        self.metrics_writer.add(WriterObject(metric_object[1], metric_object))
        self._get_stat(order_response.order_request.market.id).add("slippage", order_response.slippage)

    def handle_metrics(self, objects: list):
        # metric_object is a tuple of (Measuremen ts, timestamp, tags[], fields[])
        for metric_object in objects:
            self.metrics_writer.add(WriterObject(metric_object[1], metric_object))

    def handle_pnl_update(self, pnl_update: PnlUpdate):
        metric_object = pnl_update.to_metric_object()
        self.metrics_writer.add(WriterObject(metric_object[1], metric_object))

        market_id = pnl_update.market.id
        stat = self._get_stat(market_id)
        stat.add("fee", pnl_update.fee)
        if pnl_update.settle_type == SettleType.CLOSE:
            stat.add("pnl", pnl_update.pnl)
            snapshot = stat.snapshot
            snapshot_log = market_id + " " + snapshot.replace("\n", " ")
            LOG.info(f"STAT {snapshot_log}")
            if self.chat:
                self.chat.send(snapshot)

    def handle_exit(self, ev_loop: EventLoop):
        if self.should_exit:
            return
        LOG.info("Stop all because ev_loop=%s exited", ev_loop.__class__.__name__)
        self.stop()

    def stop(self):
        if self.should_exit:
            return
        self.should_exit = True
        LOG.info("stop")
        try:
            if self.trader:
                LOG.info(f"stop trader {self.trader.__class__.__name__}")
                self.trader.stop()
                self.trader.join(timeout=5.0)

            for conn in self.conns:
                LOG.info(f"disconnect conn {conn.__class__.__name__} {conn.exchange_name.name}")
                conn.disconnect()

            for gateway in self.gateways:
                LOG.info(f"stop gateway {gateway.__class__.__name__} {gateway.exchange_name.name}")
                gateway.stop()
                gateway.join(timeout=5.0)

            if self.chat:
                LOG.info(f"disconnect chat {self.chat.__class__.__name__}")
                self.chat.send("TRADING STOPPED")
                self.chat.disconnect()
        finally:
            super().stop()
