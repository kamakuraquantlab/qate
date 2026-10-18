import logging

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.strategy import Strategy


class Events:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def response() -> OrderResponse:
    request = OrderRequest(
        1.0,
        Market(ExchangeName.GMO, Symbol.BTC_JPY),
        Side.BUY,
        100.0,
        1.0,
        SettleType.OPEN,
    )
    return OrderResponse(request, state=OrderState.FILLED)


def pnl_update() -> PnlUpdate:
    return PnlUpdate(
        1.0,
        0.5,
        12.5,
        0.25,
        SettleType.CLOSE,
        Side.SELL,
        100.0,
        87.5,
        1.0,
        Market(ExchangeName.GMO, Symbol.BTC_JPY),
    )


def test_a_fill_return_value_is_published_as_pnl():
    pnl = pnl_update()

    class Returning(Strategy):
        def handle_order_filled(self, order_response):
            return pnl

    strategy = Returning()
    events = Events()
    strategy.add_status_listener(events)

    strategy._order_filled(response())

    assert events.items == [(EventType.TRADING_PNL_UPDATE, pnl)]


def test_a_fill_without_pnl_publishes_nothing():
    strategy = Strategy()
    events = Events()
    strategy.add_status_listener(events)

    strategy._order_filled(response())

    assert events.items == []


def test_a_fill_logs_pnl_before_publishing(caplog):
    pnl = pnl_update()

    class Returning(Strategy):
        def handle_order_filled(self, order_response):
            return pnl

    strategy = Returning()
    events = Events()
    strategy.add_status_listener(events)

    with caplog.at_level(logging.INFO, logger="qate.trading.strategy"):
        strategy._order_filled(response())

    assert "PNL market=GMO:BTC_JPY side=SELL" in caplog.text
    assert "pnl=12.50000000 fee=0.25000000 return=0.14285714" in caplog.text
    assert events.items == [(EventType.TRADING_PNL_UPDATE, pnl)]
