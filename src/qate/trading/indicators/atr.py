from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator


class ATR(Indicator):
    """
    Average True Range

    Measures market volatility by calculating the average of true ranges over a period.
    True Range is the greatest of:
    - Current high - current low
    - Absolute value of current high - previous close
    - Absolute value of current low - previous close
    """

    def __init__(self, period: int):
        """
        Initialize ATR indicator.

        Args:
            period: Number of bars to average true range
        """
        self.period = period

    def compute(self, bars: deque[Bar]) -> float | None:
        if len(bars) < self.period + 1:
            return None

        true_ranges = []
        bars_list = list(bars)[-(self.period + 1) :]

        for i in range(1, len(bars_list)):
            high = bars_list[i].high
            low = bars_list[i].low
            prev_close = bars_list[i - 1].close

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        return sum(true_ranges) / self.period
