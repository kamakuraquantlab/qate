from qate.core.model import ExchangeName, MarketPrice, SettleType, Side, Symbol, Trade, TradingMode
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.risk import PerBookGate, PerPnlGate, PerSignalGate, PerTradeGate, Risk, RollingDrawdownGate


def trade(ts: float) -> Trade:
    return Trade(str(ts), Side.BUY, 2.0, 1.0, Symbol.XRP_USDT, ExchangeName.BINANCE, ts)


def book(ts: float) -> MarketPrice:
    return MarketPrice(1.99, 2.01, Symbol.XRP_USDT, ExchangeName.BINANCE, ts)


def close(ret_bps: float, ts: float = 0.0) -> PnlUpdate:
    """A closing fill that returned `ret_bps` on a cost of 1."""
    return PnlUpdate(ts, ts, ret_bps / 10_000, 0.0, SettleType.CLOSE, Side.SELL, 1.0, 1.0, 0.0)


class _Counting(PerTradeGate, PerBookGate, PerSignalGate, PerPnlGate):
    """Counts what reaches it, and closes on whichever event `closes_on` names."""

    def __init__(self, closes_on: str = ""):
        self.closes_on = closes_on
        self.seen: list[str] = []

    def _see(self, event: str) -> None:
        self.seen.append(event)
        self.is_closed = event == self.closes_on

    def on_trade(self, trade):
        self._see("trade")

    def on_book(self, ts, market_price):
        self._see("book")

    def on_signal(self, result_bps):
        self._see("signal")

    def on_pnl(self, pnl_update, ts=0.0):
        self._see("pnl")


class _TradeOnly(PerTradeGate):
    def __init__(self):
        self.trades = 0

    def on_trade(self, trade):
        self.trades += 1


def test_each_event_reaches_only_the_gates_that_read_it():
    risk, every, trades_only = Risk(), _Counting(), _TradeOnly()
    risk.add(every)
    risk.add(trades_only)

    risk.on_trade(trade(1.0))
    risk.on_book(2.0, book(2.0))
    risk.on_signal(-3.0)
    risk.on_pnl(close(0.0), 4.0)
    risk.tick(5.0)

    assert every.seen == ["trade", "book", "signal", "pnl"]
    assert trades_only.trades == 1


def test_any_closed_gate_blocks_entry_until_it_reopens():
    risk, gate = Risk(), _Counting(closes_on="signal")
    risk.add(gate)
    risk.add(_TradeOnly())
    assert risk.state == TradingMode.DEFAULT

    risk.on_signal(-1.0)
    assert risk.state == TradingMode.NO_ENTRY

    risk.on_book(1.0, book(1.0))
    assert risk.state == TradingMode.DEFAULT


def test_the_drawdown_gate_never_closes_without_a_threshold():
    gate = RollingDrawdownGate()
    for i in range(100):
        gate.on_pnl(close(-1_000.0), float(i))
    assert not gate.is_closed


def test_the_drawdown_gate_sums_closes_in_its_window_only():
    gate = RollingDrawdownGate(window_seconds=10, threshold_bps=50, cooldown_seconds=100)
    gate.on_pnl(close(-30.0), 0.0)
    gate.on_pnl(close(-30.0), 11.0)  # the first has left the window
    assert not gate.is_closed

    opening = close(-1_000.0, 12.0)
    opening.settle_type = SettleType.OPEN
    gate.on_pnl(opening, 12.0)  # an open realizes nothing
    assert not gate.is_closed

    gate.on_pnl(close(-30.0), 15.0)
    assert gate.is_closed


def test_the_drawdown_gate_stays_closed_for_its_cooldown_then_starts_over():
    gate = RollingDrawdownGate(window_seconds=1_000, threshold_bps=50, cooldown_seconds=100)
    gate.on_pnl(close(-60.0), 0.0)
    assert gate.is_closed

    gate.on_pnl(close(+500.0), 50.0)  # ignored while closed
    gate.tick(99.0)
    assert gate.is_closed

    gate.tick(100.0)
    assert not gate.is_closed

    gate.on_pnl(close(-40.0), 101.0)  # the -60 went with the cooldown
    assert not gate.is_closed


def test_risk_reopens_on_tick_when_the_cooldown_ends():
    risk, gate = Risk(), RollingDrawdownGate(threshold_bps=10, cooldown_seconds=60)
    risk.add(gate)
    risk.on_pnl(close(-20.0), 0.0)
    assert risk.state == TradingMode.NO_ENTRY
    risk.tick(60.0)
    assert risk.state == TradingMode.DEFAULT
