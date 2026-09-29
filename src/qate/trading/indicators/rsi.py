from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator


class RSI(Indicator):
    """
    Relative Strength Index

    Measures momentum by comparing the magnitude of recent gains to recent losses.
    Returns a value between 0 and 100, where values above 70 indicate overbought
    conditions and values below 30 indicate oversold conditions.
    """

    def __init__(self, period: int = 14, field: str = "close"):
        """
        Initialize RSI indicator.

        Args:
            period: Number of bars for RSI calculation (default: 14)
            field: Bar field to use (default: "close")
        """
        self.period = period
        self.field = field
        self.avg_gain = None
        self.avg_loss = None

    def compute(self, bars: deque[Bar]) -> float | None:
        if len(bars) < self.period + 1:
            return None

        current_value = getattr(bars[-1], self.field)
        prev_value = getattr(bars[-2], self.field)

        change = current_value - prev_value
        gain = max(change, 0)
        loss = abs(min(change, 0))

        # Initialize with simple average on first calculation
        if self.avg_gain is None:
            bars_list = list(bars)[-(self.period + 1):]
            gains = []
            losses = []

            for i in range(1, len(bars_list)):
                curr = getattr(bars_list[i], self.field)
                prev = getattr(bars_list[i - 1], self.field)
                change = curr - prev
                gains.append(max(change, 0))
                losses.append(abs(min(change, 0)))

            self.avg_gain = sum(gains) / self.period
            self.avg_loss = sum(losses) / self.period
        else:
            # Use Wilder's smoothing method
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period

        if self.avg_loss == 0:
            return 100.0

        rs = self.avg_gain / self.avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi
