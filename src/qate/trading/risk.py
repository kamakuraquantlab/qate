from collections import deque
import math

from qate.core.model import TradingMode


class PerTradeGate:
    is_closed: bool = False

    def on_trade(self, trade) -> None:
        raise NotImplementedError


class PerMpGate:
    is_closed: bool = False

    def on_marketprice(self, market_price) -> None:
        raise NotImplementedError


class PerBarGate:
    is_closed: bool = False

    def on_bar(self, bar) -> None:
        raise NotImplementedError


class PerPnlGate:
    is_closed: bool = False

    def on_pnl(self, pnl_update, ts: float = 0.0) -> None:
        raise NotImplementedError

    def tick(self, ts: float) -> None:
        pass


class Risk:
    def __init__(self):
        self._trade_gates: list[PerTradeGate] = []
        self._mp_gates: list[PerMpGate] = []
        self._bar_gates: list[PerBarGate] = []
        self._pnl_gates: list[PerPnlGate] = []
        self._all_gates: list = []
        self.state: TradingMode = TradingMode.DEFAULT

    def add(self, gate) -> None:
        if isinstance(gate, PerTradeGate):
            self._trade_gates.append(gate)
        if isinstance(gate, PerMpGate):
            self._mp_gates.append(gate)
        if isinstance(gate, PerBarGate):
            self._bar_gates.append(gate)
        if isinstance(gate, PerPnlGate):
            self._pnl_gates.append(gate)
        self._all_gates.append(gate)

    def on_trade(self, trade) -> None:
        for g in self._trade_gates:
            g.on_trade(trade)
        self._recompute()

    def on_marketprice(self, market_price) -> None:
        for g in self._mp_gates:
            g.on_marketprice(market_price)
        self._recompute()

    def on_bar(self, bar) -> None:
        for g in self._bar_gates:
            g.on_bar(bar)
        self._recompute()

    def on_pnl(self, pnl_update, ts: float = 0.0) -> None:
        for g in self._pnl_gates:
            g.on_pnl(pnl_update, ts)
        self._recompute()

    def tick(self, ts: float) -> None:
        for g in self._pnl_gates:
            g.tick(ts)
        self._recompute()

    def _recompute(self) -> None:
        self.state = (
            TradingMode.NO_ENTRY
            if any(g.is_closed for g in self._all_gates)
            else TradingMode.DEFAULT
        )


class DrawdownGate(PerPnlGate):
    """Block new entries when realized PnL drawdown from peak exceeds a threshold."""

    def __init__(self, max_drawdown: float = math.inf):
        self.max_drawdown = max_drawdown
        self._peak_pnl = 0.0
        self._net_realized = 0.0
        self.is_closed = False

    def on_pnl(self, pnl_update, ts: float = 0.0) -> None:
        self._net_realized += pnl_update.pnl
        if self._net_realized > self._peak_pnl:
            self._peak_pnl = self._net_realized
        self.is_closed = (self._peak_pnl - self._net_realized) > self.max_drawdown


class SpreadGate(PerMpGate):
    """Block new entries when the execution spread exceeds a threshold."""

    def __init__(self, max_spread_bps: float = 6.0):
        self.max_spread_bps = max_spread_bps
        self.is_closed = False

    def on_marketprice(self, market_price) -> None:
        if market_price is None:
            self.is_closed = False
            return
        spread_bps = (market_price.ask - market_price.bid) / market_price.bid * 10_000
        self.is_closed = spread_bps > self.max_spread_bps


class TradeRateGate(PerTradeGate):
    """Block new entries when the ref-market trade rate spikes abnormally."""

    def __init__(self, window_seconds: float = 30.0, max_trades: int = 1000):
        self.window_seconds = window_seconds
        self.max_trades = max_trades
        self._timestamps: deque[float] = deque()
        self.is_closed = False

    def on_trade(self, trade) -> None:
        ts = trade.get_ts()
        cutoff = ts - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        self._timestamps.append(ts)
        self.is_closed = len(self._timestamps) > self.max_trades


