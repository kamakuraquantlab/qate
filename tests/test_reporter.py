"""Reporters: the fan-out, and that a broken one cannot take a run down.

Event dispatch isolates ordinary handler failures so one unreachable reporter
cannot prevent another from recording the same outcome. Reporter lifecycle hooks
remain fail-fast.
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
    run.put(EventType.TRADING_ORDER, response)
    run._next()

    assert a.kinds == ["report_order"]
    assert b.kinds == ["report_order"]
    assert a.calls[0][1] is response


def test_a_close_reports_the_update(tmp_path, monkeypatch):
    recorder = Recorder()
    run = runtime(tmp_path, monkeypatch, recorder)

    run.put(EventType.TRADING_PNL_UPDATE, pnl(SettleType.OPEN))
    run._next()
    assert recorder.kinds == ["report_pnl"]

    run.put(EventType.TRADING_PNL_UPDATE, pnl(SettleType.CLOSE))
    run._next()
    assert recorder.kinds == ["report_pnl", "report_pnl"]


def test_exceptions_are_reported(tmp_path, monkeypatch):
    recorder = Recorder()
    run = runtime(tmp_path, monkeypatch, recorder)

    error = RuntimeError("something broke")
    run.put(EventType.EXCEPTION, error)
    run._next()

    assert recorder.kinds == ["report_exception"]
    assert recorder.calls[0][1] is error


def test_a_failing_reporter_does_not_stop_the_run(tmp_path, monkeypatch):
    """The isolation guarantee. A run must survive an unreachable destination."""
    good = Recorder()
    run = runtime(tmp_path, monkeypatch, Exploding(), good)

    run.put(EventType.TRADING_ORDER, fill())
    run._next()
    run.put(EventType.TRADING_PNL_UPDATE, pnl(SettleType.CLOSE))
    run._next()

    # The working reporter still saw everything, in order.
    assert good.kinds == ["report_order", "report_pnl"]


def test_a_reporter_that_overrides_nothing_is_harmless(tmp_path, monkeypatch):
    """Every hook is a no-op, so a reporter implements only what it cares about."""
    run = runtime(tmp_path, monkeypatch, Reporter())
    run.put(EventType.TRADING_ORDER, fill())
    run._next()
    run.put(EventType.TRADING_PNL_UPDATE, pnl(SettleType.CLOSE))
    run._next()
