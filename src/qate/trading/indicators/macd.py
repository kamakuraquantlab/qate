from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator

from .ema import EMA


class MACD(Indicator):
    """
    Moving Average Convergence Divergence

    Trend-following momentum indicator that shows the relationship between two
    moving averages. Returns a dict with 'macd', 'signal', and 'histogram' values.
    """

    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        field: str = "close",
    ):
        """
        Initialize MACD indicator.

        Args:
            fast_period: Period for fast EMA (default: 12)
            slow_period: Period for slow EMA (default: 26)
            signal_period: Period for signal line EMA (default: 9)
            field: Bar field to use (default: "close")
        """
        self.fast_ema = EMA(fast_period, field)
        self.slow_ema = EMA(slow_period, field)
        self.signal_ema = EMA(signal_period, field)
        self.field = field

    def compute(self, bars: deque[Bar]) -> dict | None:
        fast = self.fast_ema.compute(bars)
        slow = self.slow_ema.compute(bars)

        if fast is None or slow is None:
            return None

        macd_line = fast - slow

        # Create a temporary deque with a single bar containing the MACD value
        # This is a workaround to use EMA for the signal line
        # In practice, we need to track MACD history
        if not hasattr(self, "_macd_history"):
            self._macd_history = deque(maxlen=100)

        # Create a temporary bar with MACD as the close price
        temp_bar = Bar(
            open=macd_line,
            high=macd_line,
            low=macd_line,
            close=macd_line,
            volume=0,
            timestamp=bars[-1].timestamp,
        )
        self._macd_history.append(temp_bar)

        signal = self.signal_ema.compute(self._macd_history)

        if signal is None:
            return {"macd": macd_line, "signal": None, "histogram": None}

        histogram = macd_line - signal

        return {"macd": macd_line, "signal": signal, "histogram": histogram}
