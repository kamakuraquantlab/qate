from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState
from qate.core.symbol import Symbol
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


def test_a_fill_return_value_is_published_as_pnl():
    pnl = object()

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
