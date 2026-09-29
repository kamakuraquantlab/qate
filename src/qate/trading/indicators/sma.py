from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator


class SMA(Indicator):
    """
    Simple Moving Average

    Calculates the arithmetic mean of closing prices over a specified period.
    """

    def __init__(self, period: int, field: str = "close"):
        """
        Initialize SMA indicator.

        Args:
            period: Number of bars to average
            field: Bar field to use (default: "close")
        """
        self.period = period
        self.field = field

    def compute(self, bars: deque[Bar]) -> float | None:
        if len(bars) < self.period:
            return None

        values = [getattr(bar, self.field) for bar in list(bars)[-self.period :]]
        return sum(values) / self.period
