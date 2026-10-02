from collections import deque

from qate.trading.chart import Bar
from qate.trading.indicator import Indicator


class SMA(Indicator):
    def __init__(self, period: int, field: str = "close"):
        self.period = period
        self.field = field

    def compute(self, bars: deque[Bar]) -> float | None:
        if len(bars) < self.period:
            return None

        values = [getattr(bar, self.field) for bar in list(bars)[-self.period :]]
        return sum(values) / self.period
