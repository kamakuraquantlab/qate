"""Unit tests for TradeAggressionBuilder window scaling.

Regression guard for the seconds-vs-milliseconds bug: Trade timestamps are in
SECONDS (Trade.exchange_ts), so a window_sec=1 window must only hold trades
within 1 second. A previous version used `ts - window_sec*1000`, making every
window 1000x too long (a "1s" window spanned ~1000s).

Run: python -m pytest test/test_aggression.py
"""

from qate.core.model import Side, Trade
from qate.trading.aggression import TradeAggressionBuilder


def _trade(ts: float, size: float = 1.0, side: Side = Side.BUY) -> Trade:
    return Trade(
        trade_id="t", side=side, price=100.0, size=size,
        symbol=None, exchange_name=None, exchange_ts=ts, received_ts=ts,
    )


def test_one_second_window_holds_only_one_second():
    # One trade per second for 10 seconds. The window is [now-window_sec, now]
    # inclusive, so at t=9 the 1s window holds t=8,9 (2 trades) — the point is
    # it is ~window_sec seconds wide, NOT 1000x (which would hold all 10).
    b = TradeAggressionBuilder(windows_sec=[1, 5])
    for t in range(10):
        b.on_trade(_trade(float(t), size=1.0))
    agg = {a.window_sec: a for a in b.get_trade_aggression(9.0)}
    assert agg[1].total_count == 2, agg[1].total_count      # t=8,9
    assert agg[1].total_qty == 2.0
    # 5s window: ts >= 4 -> t=4,5,6,7,8,9 = 6 trades
    assert agg[5].total_count == 6, agg[5].total_count


def test_window_evicts_old_trades():
    # Sub-second trades: 20 trades at 0.1s spacing over 2 seconds.
    b = TradeAggressionBuilder(windows_sec=[1])
    ts = 0.0
    for _ in range(20):
        b.on_trade(_trade(ts))
        ts += 0.1
    # now_ts ~1.9; 1s window keeps ts in (0.9, 1.9] -> ~10 trades, NOT all 20
    agg = b.get_trade_aggression(1.9)[0]
    assert 9 <= agg.total_count <= 11, agg.total_count


def test_imbalance_reflects_recent_window_only():
    b = TradeAggressionBuilder(windows_sec=[1])
    # old sells (outside 1s), recent buys (inside 1s)
    for t in range(5):
        b.on_trade(_trade(float(t), side=Side.SELL))
    for t in range(5, 8):
        b.on_trade(_trade(7.0, side=Side.BUY))  # all at t=7
    agg = b.get_trade_aggression(7.0)[0]
    assert agg.imbalance_qty > 0  # only the recent buys are in-window


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
