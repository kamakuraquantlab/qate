from logging import getLogger

from qate.core.conn import Connection
from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import SettleType
from qate.core.order import OrderResponse
from qate.trading.metrics import MetricLog, RotationInterval
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader
from qate.util.counter import Stat

LOG = getLogger(__name__)


class Bootstrap(EventLoop):
    """
    A class that does everything except trading
    """

    def __init__(self, strategy: Strategy):
        super().__init__(None, heartbeat_interval=5)
        self.trader = Trader(strategy)
        self.trader.add_status_listener(self.event_queue)
        self.gateways: list[ExchangeGateway] = []
        self.reporters: list[Reporter] = []
        self.metrics_writer = MetricLog(RotationInterval.FIVE_MINUTE, 256, "Metrics")

        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)
        self.register(EventType.EXCEPTION, self.handle_reportable_exception)
        self.register(EventType.MSG_OUT, self.handle_outgoing_message)
        self.register(EventType.MSG_IN, self.trader.notify)
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

    def add_reporter(self, reporter: Reporter):
        """Add a destination for this run's outcomes. Like `add_conn` for feeds.

        Several are fine and each sees everything; a reporter that only cares about
        one kind of outcome overrides one hook and ignores the rest.
        """
        self.reporters.append(reporter)
        # A reporter may also be a source -- a chat bot taking commands -- so its
        # inbound messages reach the strategy the same way any other event does.
        reporter.add_status_listener(self.event_queue)

    def _report(self, call, *args):
        """Call one hook on every reporter, and let none of them stop the run.

        A reporter is an observer. A broken webhook or a closed socket must not take
        a live strategy down with it, so this logs and continues -- which is the
        opposite of the fail-fast rule everywhere else, and deliberately so: there
        is nothing about a trading decision that depends on anyone being told.
        """
        for reporter in self.reporters:
            try:
                getattr(reporter, call)(*args)
            except Exception:
                LOG.exception(f"Reporter {reporter.__class__.__name__}.{call} failed")

    def before_loop(self):
        for reporter in self.reporters:
            LOG.info(f"reporter {reporter.__class__.__name__} connect")
            reporter.connect()
        if self.trader:
            LOG.info(f"trader {self.trader.__class__.__name__} start")
            self.trader.start()
        for gateway in self.gateways:
            LOG.info(f"gateway {gateway.__class__.__name__} {gateway.exchange_name.name} start")
            gateway.start()
        for conn in self.conns:
            conn.connect()

        self._report("on_start")

    def after_loop(self):
        LOG.warning(f"{self.__class__.__name__} metrics writer close {len(self.metrics_writer.buffer)}")
        self.metrics_writer.close()

    def _get_stat(self, key: str):
        if key not in self.stats:
            stat = Stat()
            self.stats[key] = stat
        return self.stats[key]

    def handle_order(self, order_response: OrderResponse):
        self.metrics_writer.add(order_response.to_metric())
        self._get_stat(order_response.order_request.market.id).add("slippage", order_response.slippage)
        self._report("on_order", order_response)

    def handle_metrics(self, metrics: list):
        for metric in metrics:
            self.metrics_writer.add(metric)

    def handle_pnl_update(self, pnl_update: PnlUpdate):
        self.metrics_writer.add(pnl_update.to_metric())

        self._report("on_pnl_update", pnl_update)

        market_id = pnl_update.market.id
        stat = self._get_stat(market_id)
        stat.add("fee", pnl_update.fee)
        if pnl_update.settle_type == SettleType.CLOSE:
            stat.add("pnl", pnl_update.pnl)
            snapshot = stat.snapshot
            snapshot_log = market_id + " " + snapshot.replace("\n", " ")
            LOG.info(f"STAT {snapshot_log}")
            self._report("on_summary", market_id, snapshot)

    def handle_reportable_exception(self, error: BaseException):
        self._report("on_exception", error)

    def handle_outgoing_message(self, message: str):
        self._report("on_message", message)

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

            self._report("on_stop")
            for reporter in self.reporters:
                LOG.info(f"disconnect reporter {reporter.__class__.__name__}")
                try:
                    reporter.disconnect()
                except Exception:
                    LOG.exception(f"Reporter {reporter.__class__.__name__}.disconnect failed")
        finally:
            super().stop()
