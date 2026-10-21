"""`received_ts` is stamped when each book or trade is built, not when the class was.

Adapters do not pass it, so the default is the only value a live event carries. As
`= time.time()` it was evaluated once, at import, and every event a process ever saw
reported the same arrival time.
"""

import time

from qate.core.model import ExchangeName, OrderBook, Side, Trade
from qate.core.symbol import Symbol


def _built_between(make):
    before = time.time()
    event = make()
    after = time.time()
    assert before <= event.received_ts <= after
    return event


def test_each_order_book_is_stamped_when_built():
    first = _built_between(lambda: OrderBook([], [], Symbol.XRP_SPOT, ExchangeName.GMO, 1.0))
    time.sleep(0.01)
    second = _built_between(lambda: OrderBook([], [], Symbol.XRP_SPOT, ExchangeName.GMO, 1.0))
    assert second.received_ts > first.received_ts


def test_each_trade_is_stamped_when_built():
    def make():
        return Trade("1", Side.BUY, 1.0, 1.0, Symbol.XRP_SPOT, ExchangeName.GMO, 1.0)

    first = _built_between(make)
    time.sleep(0.01)
    second = _built_between(make)
    assert second.received_ts > first.received_ts


def test_an_explicit_received_ts_is_kept():
    book = OrderBook([], [], Symbol.XRP_SPOT, ExchangeName.GMO, 1.0, received_ts=1.5)
    assert book.received_ts == 1.5
