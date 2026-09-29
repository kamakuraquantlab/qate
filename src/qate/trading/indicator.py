from abc import ABC, abstractmethod
from collections import deque

from .chart import Bar


class Indicator(ABC):
    """
    Base class for all indicators.

    Indicators compute values from bar data and can be composed with other indicators.
    """

    @abstractmethod
    def compute(self, bars: deque[Bar]) -> float | dict | None:
        """
        Compute indicator value from bar data.

        Args:
            bars: Deque of Bar objects

        Returns:
            Indicator value (float), multiple values (dict), or None if not enough data
        """
