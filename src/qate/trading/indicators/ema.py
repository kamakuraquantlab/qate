from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator


class EMA(Indicator):
    def __init__(self, period: int):
        self.period = period
        self.multiplier = 2 / (period + 1)
        self.ema_value = None

    def compute(self, bars: deque[Bar]) -> float | None:
        if len(bars) < self.period:
            return None

        current_value = bars[-1].close

        # Initialize with SMA on first calculation
        if self.ema_value is None:
            start = -self.period
            values = [bar.close for bar in list(bars)[start:]]
            self.ema_value = sum(values) / self.period
        else:
            # EMA = (current - prev_ema) * multiplier + prev_ema
            self.ema_value = (current_value - self.ema_value) * self.multiplier + self.ema_value

        return self.ema_value
