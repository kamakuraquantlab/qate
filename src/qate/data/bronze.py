"""Read market data from the Kamakura Quant Lab bronze layer.

Bronze is the recorded market data a backtest replays: one parquet file per
market, dataset and Asia/Tokyo day, in a Hive-partitioned tree.

    <root>/bronze/dataset=Trade/exchange=GMO/symbol=BTC_JPY/date=2026-01-15/data.parquet

`komachi` downloads it, and `komachi.data_root()` is where it put it, so a
backtest normally names no path at all. An explicit root still works, and
because this is the same layout the data was produced in, a root pointing at a
locally built tree reads identically.

## Only reading

Nothing here collects. A reader opens files that already exist, which is the
whole of `qate`'s relationship with market data: acquiring it is `komachi`'s
job, deriving from it is `hase`'s, and replaying it is this module's.

## Two datasets and their schemas

`Trade` is `ts, side, price, size` with `side` 0 for a buy and 1 for a sell.
`OrderBook` is `ts` plus `bid<i>_price, bid<i>_qty` and `ask<i>_price,
ask<i>_qty` for each level. Depth is read from the file's columns rather than
assumed, because it differs by venue and has changed over the archive; a level
whose price is null is absent from that snapshot, not zero.

## Dates are JST

Every `date=` partition is an Asia/Tokyo day, spanning 15:00-14:59 UTC, and the
`ts` inside the files is a UTC epoch. `DtRange.days` produces the same ISO date
strings these partitions use, so a range maps onto files without conversion.
"""

from abc import ABC, abstractmethod
from logging import getLogger
from pathlib import Path

import pandas as pd

from qate.core.ev_type import EventType
from qate.core.model import Market, OrderBook, OrderLevel, Side, Trade, TimeSeriesData

LOG = getLogger(__name__)

BRONZE = "bronze"
TRADE = "Trade"
ORDER_BOOK = "OrderBook"
DATASETS = (TRADE, ORDER_BOOK)

# Where komachi puts data when nothing has been configured. Kept so that a
# missing komachi install degrades to a wrong-but-obvious path rather than an
# ImportError from inside a backtest.
DEFAULT_ROOT = "~/kamakuraquantlab-data"


class MarketDataNotFound(Exception):
    pass


def data_root(override: str | Path | None = None) -> Path:
    """The data root: an explicit override, else whatever komachi was set to.

    Asking komachi rather than re-deriving the answer means the backtest reads
    exactly where the downloader wrote, and keeps working when that setting
    changes.
    """
    if override:
        return Path(override).expanduser()
    try:
        import komachi

        return Path(komachi.data_root())
    except Exception:
        LOG.warning(
            f"komachi is not installed or not set up; falling back to {DEFAULT_ROOT}. "
            f"Pass an explicit data root to read from somewhere else."
        )
        return Path(DEFAULT_ROOT).expanduser()


def dataset_dir(root: str | Path, dataset: str, market: Market) -> Path:
    return (
        Path(root)
        / BRONZE
        / f"dataset={dataset}"
        / f"exchange={market.exchange_name.name}"
        / f"symbol={market.symbol.name}"
    )


def data_path(root: str | Path, dataset: str, market: Market, date_str: str) -> Path:
    return dataset_dir(root, dataset, market) / f"date={date_str}" / "data.parquet"


def available_dates(root: str | Path, dataset: str, market: Market) -> list[str]:
    """Every date of one dataset that is complete on disk, sorted.

    A `date=` directory without its `data.parquet` is a partial download and is
    not reported as held -- the same rule `komachi.bronze` applies, so the two
    never disagree about which days a run can cover.
    """
    base = dataset_dir(root, dataset, market)
    if not base.is_dir():
        return []
    return sorted(d.name.split("=", 1)[1] for d in base.glob("date=*") if (d / "data.parquet").is_file())


class BronzeReader(ABC):
    """One market and dataset, read a day at a time.

    `load` is the interface a replay driver calls: it returns the day's events
    as `(EventType, object)` pairs in file order, which for bronze is timestamp
    order. A day that is not on disk reads as empty rather than raising, so a
    gap in the archive costs that day and not the run -- `available_dates` is
    how a caller checks first when it wants to know.
    """

    dataset: str

    def __init__(self, root: str | Path, market: Market):
        self.root = Path(root)
        self.market = market

    def path(self, date_str: str) -> Path:
        return data_path(self.root, self.dataset, self.market, date_str)

    def read_a_day(self, date_str: str) -> pd.DataFrame:
        path = self.path(date_str)
        if not path.exists():
            LOG.warning(f"No {self.dataset} data for {self.market.id} on {date_str} ({path})")
            return pd.DataFrame()
        df = pd.read_parquet(path)
        LOG.info(f"Loaded {len(df)} {self.dataset} rows for {self.market.id} {date_str}")
        return df

    def load(self, date_str: str) -> list[tuple[str, TimeSeriesData]]:
        df = self.read_a_day(date_str)
        if df.empty:
            return []
        return self.convert(df)

    def load_days(self, days: list[str]) -> list[tuple[str, TimeSeriesData]]:
        events: list[tuple[str, TimeSeriesData]] = []
        for date_str in days:
            events.extend(self.load(date_str))
        return events

    def available_dates(self) -> list[str]:
        return available_dates(self.root, self.dataset, self.market)

    @abstractmethod
    def convert(self, df: pd.DataFrame) -> list[tuple[str, TimeSeriesData]]:
        pass


class TradeReader(BronzeReader):
    dataset = TRADE

    def convert(self, df: pd.DataFrame) -> list[tuple[str, Trade]]:
        symbol = self.market.symbol
        exchange_name = self.market.exchange_name
        return [
            (
                EventType.MARKET_TRADE,
                Trade(
                    "",
                    Side.BUY if row.side == 0 else Side.SELL,
                    row.price,
                    row.size,
                    symbol,
                    exchange_name,
                    row.ts,
                ),
            )
            for row in df.itertuples(index=False)
        ]


class OrderBookReader(BronzeReader):
    dataset = ORDER_BOOK

    def convert(self, df: pd.DataFrame) -> list[tuple[str, OrderBook]]:
        depth = book_depth(df.columns)
        if depth == 0:
            raise MarketDataNotFound(
                f"{self.dataset} file for {self.market.id} has no bid/ask level columns: {list(df.columns)[:8]}"
            )

        symbol = self.market.symbol
        exchange_name = self.market.exchange_name

        # Read by position rather than by name: itertuples over 80+ named
        # columns is measurably slower, and a full-depth day is millions of rows.
        ordered = ["ts"]
        for side in ("bid", "ask"):
            for i in range(depth):
                ordered += [f"{side}{i}_price", f"{side}{i}_qty"]
        df = df[ordered]

        ask_start = 1 + 2 * depth
        events = []
        for row in df.itertuples(index=False, name=None):
            events.append(
                (
                    EventType.MARKET_ORDER_BOOK,
                    OrderBook(
                        _levels(row, 1, depth),
                        _levels(row, ask_start, depth),
                        symbol,
                        exchange_name,
                        row[0],
                    ),
                )
            )
        return events


def _levels(row: tuple, start: int, depth: int) -> list[OrderLevel]:
    """Price/qty pairs from `start`, stopping at the first absent level.

    Absent levels are only ever a tail: a book with four levels fills 0-3 and
    leaves the rest null. Levels below the touch are not optional, so stopping
    is right -- a book with a hole in it is not a book.

    Both spellings of absent have to be handled. A level column that is null for
    every row in a file comes back as `None` because parquet typed it as null,
    while one that is null for only some rows comes back as `NaN` in a float
    column. `price != price` is the NaN test, cheaper than `math.isnan` in a loop
    this hot, and it does not catch `None` -- which is why both are here.
    """
    levels = []
    for i in range(depth):
        price = row[start + 2 * i]
        if price is None or price != price:
            break
        levels.append(OrderLevel(price, row[start + 2 * i + 1]))
    return levels


def book_depth(columns) -> int:
    """How many levels a book file holds, from its column names."""
    names = set(columns)
    depth = 0
    while f"bid{depth}_price" in names and f"ask{depth}_price" in names:
        depth += 1
    return depth


class BronzeStore:
    """The bronze tree under one root, and readers onto it."""

    def __init__(self, root: str | Path | None = None):
        self.root = data_root(root)

    def create_trade(self, market: Market) -> TradeReader:
        return TradeReader(self.root, market)

    def create_order_book(self, market: Market) -> OrderBookReader:
        return OrderBookReader(self.root, market)

    def create_reader(self, market: Market, event_type: str) -> BronzeReader:
        """A reader for the dataset behind a market-data event type."""
        if event_type == EventType.MARKET_TRADE:
            return self.create_trade(market)
        if event_type == EventType.MARKET_ORDER_BOOK:
            return self.create_order_book(market)
        raise ValueError(f"No bronze dataset for event type {event_type}")

    def available_dates(self, market: Market, dataset: str) -> list[str]:
        return available_dates(self.root, dataset, market)

    def markets(self) -> list[Market]:
        """Every market with at least one complete bronze file."""
        market_ids = set()
        for dataset in DATASETS:
            base = Path(self.root) / BRONZE / f"dataset={dataset}"
            if not base.is_dir():
                continue
            for ex in base.glob("exchange=*"):
                for sym in ex.glob("symbol=*"):
                    if not any(d.joinpath("data.parquet").is_file() for d in sym.glob("date=*")):
                        continue
                    market_ids.add(f"{ex.name.split('=', 1)[1]}:{sym.name.split('=', 1)[1]}")

        markets = []
        for market_id in sorted(market_ids):
            try:
                markets.append(Market.from_str(market_id))
            except KeyError:
                # A market qate has no ExchangeName or Symbol for. It is on disk
                # and readable by other tools; it is just not something this
                # install can replay.
                LOG.info(f"Skipping unknown market {market_id}")
        return markets
