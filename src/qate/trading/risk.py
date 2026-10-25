"""Entry gates: rules that decide when a strategy may open a position.

A gate subclasses the base for each event it reads -- `PerTradeGate` for trades,
`PerBookGate` for the traded market's price, `PerSignalGate` for the outcome of
the strategy's own signals, `PerPnlGate` for fills -- and sets `is_closed`. One
gate may read several. `Risk` hands each event only to the gates that read it,
and while any gate is closed its `state` is `TradingMode.NO_ENTRY`: the strategy
still closes positions, and opens none.

`Risk` decides nothing about the strategy's other modes; a strategy checks its
`state` beside its own schedule.
"""

import math
from collections import deque

from qate.core.model import MarketPrice, SettleType, Trade, TradingMode
from qate.trading.pnl_tracker import PnlUpdate


class PerTradeGate:
    is_closed: bool = False

    def on_trade(self, trade: Trade) -> None:
        raise NotImplementedError


class PerBookGate:
    is_closed: bool = False

    def on_book(self, ts: float, market_price: MarketPrice) -> None:
        raise NotImplementedError


class PerSignalGate:
    is_closed: bool = False

    def on_signal(self, result_bps: float) -> None:
        raise NotImplementedError


class PerPnlGate:
    is_closed: bool = False

    def on_pnl(self, pnl_update: PnlUpdate, ts: float = 0.0) -> None:
        raise NotImplementedError

    def tick(self, ts: float) -> None:
        pass


class Risk:
    """Combine a strategy's gates into its entry permission."""

    def __init__(self):
        self._trade_gates: list[PerTradeGate] = []
        self._book_gates: list[PerBookGate] = []
        self._signal_gates: list[PerSignalGate] = []
        self._pnl_gates: list[PerPnlGate] = []
        self._all_gates: list = []
        self.state = TradingMode.DEFAULT

    def add(self, gate) -> None:
        if isinstance(gate, PerTradeGate):
            self._trade_gates.append(gate)
        if isinstance(gate, PerBookGate):
            self._book_gates.append(gate)
        if isinstance(gate, PerSignalGate):
            self._signal_gates.append(gate)
        if isinstance(gate, PerPnlGate):
            self._pnl_gates.append(gate)
        self._all_gates.append(gate)

    # Each route returns early when no gate reads its event: trades and books arrive
    # far more often than anything else, and most strategies have no gate for them.
    def on_trade(self, trade: Trade) -> None:
        if not self._trade_gates:
            return
        for gate in self._trade_gates:
            gate.on_trade(trade)
        self._recompute()

    def on_book(self, ts: float, market_price: MarketPrice) -> None:
        if not self._book_gates:
            return
        for gate in self._book_gates:
            gate.on_book(ts, market_price)
        self._recompute()

    def on_signal(self, result_bps: float) -> None:
        if not self._signal_gates:
            return
        for gate in self._signal_gates:
            gate.on_signal(result_bps)
        self._recompute()

    def on_pnl(self, pnl_update: PnlUpdate, ts: float = 0.0) -> None:
        for gate in self._pnl_gates:
            gate.on_pnl(pnl_update, ts)
        self._recompute()

    def tick(self, ts: float) -> None:
        for gate in self._pnl_gates:
            gate.tick(ts)
        self._recompute()

    def _recompute(self) -> None:
        self.state = TradingMode.NO_ENTRY if any(gate.is_closed for gate in self._all_gates) else TradingMode.DEFAULT


class RollingDrawdownGate(PerPnlGate):
    """Block new entries when the closed trades of a rolling window lost more than `threshold_bps`.

    Each closing fill adds its return, in bps (`pnl_return * 10_000`), to a window
    of the last `window_seconds`. When the window's sum falls below
    `-threshold_bps` the gate closes, and stays closed for `cooldown_seconds`
    whatever the strategy's later PnL; it then reopens with the window empty.
    `tick` reopens it on time when no fill arrives to do so.

    The default threshold is infinite: the gate never closes until a strategy sets
    one, which it may do after construction (`threshold_bps` is read on every fill).
    """

    def __init__(
        self,
        window_seconds: float = 6 * 3600,
        threshold_bps: float = math.inf,
        cooldown_seconds: float = 6 * 3600,
    ):
        self.window_seconds = window_seconds
        self.threshold_bps = threshold_bps
        self.cooldown_seconds = cooldown_seconds
        self._buf: deque[tuple[float, float]] = deque()  # (ts, return in bps)
        self.is_closed = False
        self._reopen_ts: float = -math.inf

    def on_pnl(self, pnl_update: PnlUpdate, ts: float = 0.0) -> None:
        if pnl_update.settle_type != SettleType.CLOSE:
            return

        if self.is_closed:
            if ts >= self._reopen_ts:
                self.is_closed = False
                self._buf.clear()
            else:
                return

        cutoff = ts - self.window_seconds
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

        self._buf.append((ts, pnl_update.pnl_return * 10_000))

        if sum(v for _, v in self._buf) < -self.threshold_bps:
            self.is_closed = True
            self._reopen_ts = ts + self.cooldown_seconds

    def tick(self, ts: float) -> None:
        if self.is_closed and ts >= self._reopen_ts:
            self.is_closed = False
            self._buf.clear()
