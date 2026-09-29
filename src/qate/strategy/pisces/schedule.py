from datetime import datetime

from qate.core.model import TradingMode


class Schedule:
    def check(self, ts: float) -> TradingMode:
        dt = datetime.fromtimestamp(ts)
        weekday = dt.weekday()
        hour = dt.hour

        # GMO Saturday maintenance window
        if weekday == 5 and hour == 8:
            return TradingMode.NO_ENTRY
        if weekday == 5 and 9 <= hour <= 11:
            return TradingMode.NO_TRADING

        return TradingMode.DEFAULT
