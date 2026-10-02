"""What a fill costs: where the rate comes from, and what happens when it doesn't.

A fee rate is venue knowledge, so it arrives on an `ExchangeAdapter` with the rest of
what a venue supplies — `qate` holds the shape and no number. These tests cover the
two ends of that: `factory.get_fee_rate` reading a rate off whatever is installed, and
`PnlTracker` costing a fill with the rate of the market the fill happened on.

The second is the one with a bug behind it. One tracker serves several markets, which
is not an accident — pisces shares one between a maker venue and a taker venue, and
that sharing is what makes its round-trip PnL a round trip — so a tracker that held
one rate would charge a rebate on the wrong exchange.
"""

import logging

import pytest

from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState, OrderType
from qate.core.symbol import Symbol
from qate.exchange import factory, registry
from qate.trading.inventory import Inventory
from qate.trading.pnl_tracker import PnlTracker

MAKER_MARKET = Market(ExchangeName.BITBANK, Symbol.XRP_SPOT)
TAKER_MARKET = Market(ExchangeName.GMO, Symbol.XRP_SPOT)
TS = 1767225600.0

# Taker is everything that is not MAKER; `OrderType` has no TAKER member.
TAKER = OrderType.DEFAULT

BITBANK_FEES = {Symbol.XRP_SPOT: registry.FeeSchedule(maker=-0.0002, taker=0.0012)}
GMO_FEES = {Symbol.XRP_SPOT: registry.FeeSchedule(maker=-0.0001, taker=0.0005)}


@pytest.fixture(autouse=True)
def adapters(monkeypatch):
    """Two venues with rates, and no plugin discovery to add a third."""
    monkeypatch.setattr(registry, "_discovered", True)
    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {
            ExchangeName.BITBANK: registry.ExchangeAdapter(
                exchange_name=ExchangeName.BITBANK, fee_rates=BITBANK_FEES
            ),
            ExchangeName.GMO: registry.ExchangeAdapter(exchange_name=ExchangeName.GMO, fee_rates=GMO_FEES),
        },
    )


# ------------------------------------------------------------------- the factory


def test_a_rate_is_read_off_the_adapter_for_the_venue():
    assert factory.get_fee_rate(TAKER_MARKET) == registry.FeeSchedule(maker=-0.0001, taker=0.0005)
    assert factory.get_fee_rate(MAKER_MARKET).maker == -0.0002


@pytest.mark.parametrize(
    "market,why",
    [
        (Market(ExchangeName.HUOBI, Symbol.BTC_USDT), "no adapter installed for the venue"),
        (Market(ExchangeName.GMO, Symbol.SOL_JPY), "adapter installed, but no rate for the symbol"),
    ],
)
def test_not_knowing_a_rate_is_None_rather_than_an_exception(market, why):
    """Every way of not knowing answers the same, because the caller does too.

    `registry.get` raises `UnknownExchange` for a missing venue, which is right when
    something is about to be built and wrong here: a backtest host has no adapter by
    design and still has to be able to run.
    """
    assert factory.get_fee_rate(market) is None, why


def test_reading_a_rate_builds_nothing(monkeypatch):
    """Why a replay is allowed to call it: no hook on the adapter is touched.

    Every constructor this venue offers raises. A fee lookup still answers, so nothing
    on the path from `get_fee_rate` to a rate can open a connection.
    """

    def explode(*args, **kwargs):
        raise AssertionError("a fee lookup called an adapter constructor")

    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {
            ExchangeName.GMO: registry.ExchangeAdapter(
                exchange_name=ExchangeName.GMO,
                create_api=explode,
                create_order_api=explode,
                create_gateway=explode,
                create_public_connection=explode,
                create_private_connection=explode,
                fee_rates=GMO_FEES,
            )
        },
    )

    assert factory.get_fee_rate(TAKER_MARKET).taker == 0.0005


# ------------------------------------------------------------------ the tracker


def test_one_tracker_costs_each_market_with_its_own_rate():
    """pisces's shape: one tracker, a rebating maker venue and a taker venue."""
    tracker = PnlTracker()
    tracker.register_fee(MAKER_MARKET)
    tracker.register_fee(TAKER_MARKET)

    maker_fee = tracker._fee(MAKER_MARKET, 100_000.0, OrderType.MAKER)
    taker_fee = tracker._fee(TAKER_MARKET, 100_000.0, TAKER)

    assert maker_fee == pytest.approx(-20.0), "bitbank rebates a maker fill"
    assert taker_fee == pytest.approx(50.0), "GMO charges a taker fill"


def test_a_cross_venue_round_trip_carries_both_legs():
    """The whole reason the rate is per market and not per tracker.

    Buy as maker on bitbank, sell as taker on GMO. The open leg's fee is deferred to
    the close so one `PnlUpdate` carries the round trip: -20 + 50.5 on these notionals.
    """
    tracker = PnlTracker()
    tracker.register_fee(MAKER_MARKET)
    tracker.register_fee(TAKER_MARKET)

    tracker.process_order(fill(MAKER_MARKET, Side.BUY, SettleType.OPEN, 1000, 100.0, OrderType.MAKER))
    update = tracker.process_order(fill(TAKER_MARKET, Side.SELL, SettleType.CLOSE, 1000, 101.0, TAKER))

    assert update.fee == pytest.approx(30.5)
    assert tracker.total_fees == pytest.approx(30.5)


def test_register_fee_returns_what_it_found():
    tracker = PnlTracker()

    assert tracker.register_fee(TAKER_MARKET).taker == 0.0005
    assert tracker.register_fee(Market(ExchangeName.HUOBI, Symbol.BTC_USDT)) is None


def test_an_unknown_market_is_costed_at_zero_and_warns_once(caplog):
    """A rate nobody registered must not kill a live strategy mid-position."""
    tracker = PnlTracker()

    with caplog.at_level(logging.WARNING, logger="qate.trading.pnl_tracker"):
        assert tracker._fee(TAKER_MARKET, 100_000.0, TAKER) == 0.0
        assert tracker._fee(TAKER_MARKET, 100_000.0, OrderType.MAKER) == 0.0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "once per market, not once per fill"
    assert TAKER_MARKET.id in warnings[0].message


def test_a_venue_with_no_adapter_warns_at_registration_not_at_the_first_fill(caplog):
    """Said at setup, where someone is watching, rather than deep in a run."""
    with caplog.at_level(logging.WARNING, logger="qate.trading.pnl_tracker"):
        PnlTracker().register_fee(Market(ExchangeName.HUOBI, Symbol.BTC_USDT))

    assert "HUOBI" in caplog.text


def test_an_inventory_registers_its_own_market():
    """Which is what makes a strategy built the usual way cost its fills correctly."""
    tracker = PnlTracker()
    Inventory(MAKER_MARKET, max_drawdown=1_000.0, pnl_tracker=tracker)
    Inventory(TAKER_MARKET, max_drawdown=1_000.0, pnl_tracker=tracker)

    assert sorted(tracker.fee_rates) == sorted([MAKER_MARKET.id, TAKER_MARKET.id])


def fill(market, side, settle_type, size, price, order_type) -> OrderResponse:
    request = OrderRequest(
        ts=TS,
        market=market,
        side=side,
        price=price,
        size=size,
        settle_type=settle_type,
        order_type=order_type,
    )
    return OrderResponse(
        request,
        state=OrderState.FILLED,
        created_ts=TS,
        exec_size=size,
        exec_price=price,
        completed_ts=TS + 1,
    )
