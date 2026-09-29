"""
Standard technical indicators for trading strategies.

All indicators inherit from qate.trading.indicator.Indicator and implement
the compute(bars: deque[Bar]) -> float | dict | None method.
"""

from .atr import ATR
from .ema import EMA
from .macd import MACD
from .rsi import RSI
from .sma import SMA

__all__ = ["SMA", "EMA", "ATR", "RSI", "MACD"]
