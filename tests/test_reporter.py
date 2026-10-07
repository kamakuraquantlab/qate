"""Reporters: the fan-out, and that a broken one cannot take a run down.

The isolation is the part worth holding. A reporter is an observer -- nothing about
a trading decision depends on anyone being told -- so a closed socket or a failing
webhook must log and be ignored. That is the opposite of the fail-fast rule
everywhere else in this library, and deliberately so.
"""

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState, OrderType
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.trading.runtime import Runtime
from qate.trading.strategy import Strategy

MARKET = Market(ExchangeName.GMO, Symbol.BTC_JPY)
TS = 1767225600.0


class Recorder(Reporter):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def report_order(self, order_response):
        self.calls.append(("report_order", order_response))

    def report_metrics(self, metrics):
        self.calls.append(("report_metrics", metrics))

    def report_pnl(self, pnl_update):
        self.calls.append(("report_pnl", pnl_update))

    def report_exception(self, error):
        self.calls.append(("report_exception", error))

    def report_message(self, message):
        self.calls.append(("report_message", message))

    @property
    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class Exploding(Reporter):
    """Fails in every hook, the way an unreachable service would."""

    def report_order(self, order_response):
        raise RuntimeError("webhook is down")

    def report_pnl(self, pnl_update):
        raise RuntimeError("webhook is down")

    def stop(self):
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


def runtime(tmp_path, monkeypatch, *reporters) -> Runtime:
    monkeypatch.chdir(tmp_path)
    run = Runtime(Strategy())
    for reporter in reporters:
        run.add_reporter(reporter)
    return run


def test_a_fill_reaches_every_reporter(tmp_path, monkeypatch):
    a, b = Recorder(), Recorder()
    run = runtime(tmp_path, monkeypatch, a, b)

    response = fill()
    run.handle_order(response)

    assert a.kinds == ["report_order"]
    assert b.kinds == ["report_order"]
    assert a.calls[0][1] is response


def test_a_close_reports_the_update(tmp_path, monkeypatch):
    recorder = Recorder()
    run = runtime(tmp_path, monkeypatch, recorder)

    run.handle_pnl_update(pnl(SettleType.OPEN))
    assert recorder.kinds == ["report_pnl"]

    run.handle_pnl_update(pnl(SettleType.CLOSE))
    assert recorder.kinds == ["report_pnl", "report_pnl"]


def test_an_open_reports_no_summary(tmp_path, monkeypatch):
    """A summary is the running total after a position closed, not on every event."""
    recorder = Recorder()
    run = runtime(tmp_path, monkeypatch, recorder)
    run.handle_pnl_update(pnl(SettleType.OPEN))
    assert "report_message" not in recorder.kinds


def test_exceptions_and_messages_are_reported(tmp_path, monkeypatch):
    recorder = Recorder()
    run = runtime(tmp_path, monkeypatch, recorder)

    error = RuntimeError("something broke")
    run.put(EventType.EXCEPTION, error)
    run.put(EventType.MSG_OUT, "hello")
    for _ in range(2):
        run._next()

    assert recorder.kinds == ["report_exception", "report_message"]
    assert recorder.calls[0][1] is error
    assert recorder.calls[1][1] == "hello"


def test_a_failing_reporter_does_not_stop_the_run(tmp_path, monkeypatch):
    """The isolation guarantee. A run must survive an unreachable destination."""
    good = Recorder()
    run = runtime(tmp_path, monkeypatch, Exploding(), good)

    run.handle_order(fill())
    run.handle_pnl_update(pnl(SettleType.CLOSE))

    # The working reporter still saw everything, in order.
    assert good.kinds == ["report_order", "report_pnl"]


def test_a_reporter_that_overrides_nothing_is_harmless(tmp_path, monkeypatch):
    """Every hook is a no-op, so a reporter implements only what it cares about."""
    run = runtime(tmp_path, monkeypatch, Reporter())
    run.handle_order(fill())
    run.handle_pnl_update(pnl(SettleType.CLOSE))


def test_a_reporter_can_send_messages_back_to_the_strategy(tmp_path, monkeypatch):
    """A bidirectional reporter -- a chat bot taking commands -- still works."""
    received: list = []

    class Listening(Strategy):
        def handle_msg(self, msg):
            received.append(msg)

    monkeypatch.chdir(tmp_path)
    run = Runtime(Listening())
    reporter = Recorder()
    run.add_reporter(reporter)

    reporter.publish_status(EventType.MSG_IN, "status?")
    run._next()

    # Runtime routes MSG_IN to the trader, which passes it to the strategy.
    assert run.trader.event_queue.get(timeout=0.1) == (EventType.MSG_IN, "status?")
