"""Reporters: the fan-out, and that a broken one cannot take a run down.

The isolation is the part worth holding. A reporter is an observer -- nothing about
a trading decision depends on anyone being told -- so a closed socket or a failing
webhook must log and be ignored. That is the opposite of the fail-fast rule
everywhere else in this library, and deliberately so.
"""

from qate.boot.bootstrap import Bootstrap
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState, OrderType
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy

MARKET = Market(ExchangeName.GMO, Symbol.BTC_JPY)
TS = 1767225600.0


class Recorder(Reporter):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def connect(self):
        self.calls.append(("connect",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def on_start(self):
        self.calls.append(("on_start",))

    def on_stop(self):
        self.calls.append(("on_stop",))

    def on_order(self, order_response):
        self.calls.append(("on_order", order_response))

    def on_pnl_update(self, pnl_update):
        self.calls.append(("on_pnl_update", pnl_update))

    def on_summary(self, market_id, summary):
        self.calls.append(("on_summary", market_id, summary))

    def on_exception(self, error):
        self.calls.append(("on_exception", error))

    def on_message(self, message):
        self.calls.append(("on_message", message))

    @property
    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class Exploding(Reporter):
    """Fails in every hook, the way an unreachable service would."""

    def on_order(self, order_response):
        raise RuntimeError("webhook is down")

    def on_pnl_update(self, pnl_update):
        raise RuntimeError("webhook is down")

    def disconnect(self):
        raise RuntimeError("socket already closed")


def fill() -> OrderResponse:
    request = OrderRequest(
        ts=TS,
        market=MARKET,
        side=Side.BUY,
        price=15_000_000.0,
        size=0.01,
        settle_type=SettleType.OPEN,
        order_type=OrderType.DEFAULT,
    )
    return OrderResponse(
        request,
        state=OrderState.FILLED,
        created_ts=TS,
        order_id="1",
        exec_size=0.01,
        exec_price=15_000_100.0,
        completed_ts=TS + 1,
    )


def pnl(settle_type: SettleType) -> PnlUpdate:
    return PnlUpdate(
        signal_ts=TS,
        open_ts=TS,
        pnl=100.0,
        fee=1.0,
        settle_type=settle_type,
        side=Side.BUY,
        notional=150_000.0,
        open_cost=150_000.0,
        close_interval=1.0,
        market=MARKET,
    )


def bootstrap(tmp_path, monkeypatch, *reporters) -> Bootstrap:
    monkeypatch.chdir(tmp_path)  # the metric log writes to cwd
    boot = Bootstrap(Strategy())
    for reporter in reporters:
        boot.add_reporter(reporter)
    return boot


def test_a_fill_reaches_every_reporter(tmp_path, monkeypatch):
    a, b = Recorder(), Recorder()
    boot = bootstrap(tmp_path, monkeypatch, a, b)

    response = fill()
    boot.handle_order(response)

    assert a.kinds == ["on_order"]
    assert b.kinds == ["on_order"]
    assert a.calls[0][1] is response


def test_a_close_reports_the_update_and_the_running_summary(tmp_path, monkeypatch):
    recorder = Recorder()
    boot = bootstrap(tmp_path, monkeypatch, recorder)

    boot.handle_pnl_update(pnl(SettleType.OPEN))
    assert recorder.kinds == ["on_pnl_update"]

    boot.handle_pnl_update(pnl(SettleType.CLOSE))
    assert recorder.kinds == ["on_pnl_update", "on_pnl_update", "on_summary"]

    _, market_id, summary = recorder.calls[-1]
    assert market_id == MARKET.id
    assert summary


def test_an_open_reports_no_summary(tmp_path, monkeypatch):
    """A summary is the running total after a position closed, not on every event."""
    recorder = Recorder()
    boot = bootstrap(tmp_path, monkeypatch, recorder)
    boot.handle_pnl_update(pnl(SettleType.OPEN))
    assert "on_summary" not in recorder.kinds


def test_exceptions_and_messages_are_reported(tmp_path, monkeypatch):
    recorder = Recorder()
    boot = bootstrap(tmp_path, monkeypatch, recorder)

    error = RuntimeError("something broke")
    boot.put(EventType.EXCEPTION, error)
    boot.put(EventType.MSG_OUT, "hello")
    for _ in range(2):
        boot._next()

    assert recorder.kinds == ["on_exception", "on_message"]
    assert recorder.calls[0][1] is error
    assert recorder.calls[1][1] == "hello"


def test_a_failing_reporter_does_not_stop_the_run(tmp_path, monkeypatch):
    """The isolation guarantee. A run must survive an unreachable destination."""
    good = Recorder()
    boot = bootstrap(tmp_path, monkeypatch, Exploding(), good)

    boot.handle_order(fill())
    boot.handle_pnl_update(pnl(SettleType.CLOSE))

    # The working reporter still saw everything, in order.
    assert good.kinds == ["on_order", "on_pnl_update", "on_summary"]


def test_a_reporter_that_overrides_nothing_is_harmless(tmp_path, monkeypatch):
    """Every hook is a no-op, so a reporter implements only what it cares about."""
    boot = bootstrap(tmp_path, monkeypatch, Reporter())
    boot.handle_order(fill())
    boot.handle_pnl_update(pnl(SettleType.CLOSE))


def test_a_reporter_can_send_messages_back_to_the_strategy(tmp_path, monkeypatch):
    """A bidirectional reporter -- a chat bot taking commands -- still works."""
    received: list = []

    class Listening(Strategy):
        def handle_msg(self, msg):
            received.append(msg)

    monkeypatch.chdir(tmp_path)
    boot = Bootstrap(Listening())
    reporter = Recorder()
    boot.add_reporter(reporter)

    reporter.publish_status(EventType.MSG_IN, "status?")
    boot._next()

    # Bootstrap routes MSG_IN to the trader, which passes it to the strategy.
    assert boot.trader.event_queue.get(timeout=0.1) == (EventType.MSG_IN, "status?")
