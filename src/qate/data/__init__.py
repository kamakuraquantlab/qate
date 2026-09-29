"""Market data, read from what is already on disk.

`bronze` is the recorded trades and order books a backtest replays, in the
layout `komachi` downloads them into. There is no collector here and no venue
endpoint: this package opens files.
"""

from .bronze import (
    ORDER_BOOK,
    TRADE,
    BronzeStore,
    OrderBookReader,
    TradeReader,
    available_dates,
    data_root,
)

__all__ = [
    "ORDER_BOOK",
    "TRADE",
    "BronzeStore",
    "OrderBookReader",
    "TradeReader",
    "available_dates",
    "data_root",
]
