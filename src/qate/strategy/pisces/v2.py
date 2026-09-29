import math
from dataclasses import dataclass, field
from logging import getLogger

from qate.core.ev_type import EventType
from qate.core.model import (
    EventLoopExit,
    Market,
    OrderBook,
    SettleType,
    Side,
    TradingMode,
)
from qate.core.order import OrderRequest, OrderResponse, OrderType
from qate.core.symbol import Symbol
from qate.trading.gateways import DefaultGateway
from qate.trading.inventory import SpotInventory
from qate.trading.pnl_tracker import PnlTracker
from qate.trading.strategy import Strategy

from .adjust_inventory import MultiRebalanceCase, plan_multi_allocation, write_rebalance_file
from .config import Config, PairConfig
from .schedule import Schedule
from .source import MakerSource, TakerSpotSource

LOG = getLogger(__name__)

SHUTDOWN_TIMEOUT = 30.0


@dataclass
class MakerOrderEntry:
    request: OrderRequest
    response: OrderResponse = None
    cancel_requested: bool = False


@dataclass
class Params:
    # minimum spread required to place a maker order (e.g. 0.001 = 0.1% → ¥0.30 at XRP ¥300)
    default_profit_margin_percentage: float
    # looser margin used when inventory is imbalanced and a forced trade is needed to rebalance
    transfer_profit_margin_percentage: float
    # max price drift before cancelling and repricing a maker order (e.g. 0.0001 = 0.01% → ¥0.03 at XRP ¥300)
    price_tolerance_percentage: float

    @classmethod
    def from_dict(cls, data) -> "Params":
        return cls(
            data.get("default_profit_margin_percentage", 0.001),
            data.get("transfer_profit_margin_percentage", 0.0004),
            data.get("price_tolerance_percentage", 0.0001),
        )


@dataclass
class PairState:
    symbol: Symbol
    maker: MakerSource
    taker: TakerSpotSource
    maker_orders: dict = field(default_factory=dict)  # int -> MakerOrderEntry
    default_profit_margin: float = 0.0
    transfer_profit_margin: float = 0.0
    price_tolerance: float = 0.0
    last_task_ts: float = 0.0

    def update_price_vars(self, price: float, params: Params):
        self.default_profit_margin = price * params.default_profit_margin_percentage
        self.transfer_profit_margin = price * params.transfer_profit_margin_percentage
        self.price_tolerance = price * params.price_tolerance_percentage


class Variant(Strategy[Config]):
    def __init__(self, config: Config, params: dict):
        super().__init__(config, params)
        self.params_obj = Params.from_dict(params)

        self.maker_gateway: DefaultGateway = None
        self.taker_gateway: DefaultGateway = None

        self.pairs: dict[Symbol, PairState] = {}
        self._order_to_pair: dict[int, PairState] = {}
        self._warmup_books: dict = {}

        self.trading_mode = TradingMode.NO_TRADING
        self.schedule = Schedule()
        self.now_ts = 0.0
        self.stop_requested = False
        self.stop_requested_ts = 0.0

    def add_gateway(self, gateway: DefaultGateway):
        if gateway.exchange_name == self.config.maker_exchange:
            self.maker_gateway = gateway
        else:
            self.taker_gateway = gateway

    # ── startup ───────────────────────────────────────────────────────────────

    def _pair_config(self, symbol: Symbol) -> PairConfig | None:
        for pc in self.config.pairs:
            if pc.symbol == symbol:
                return pc
        return None

    def _all_books_ready(self) -> bool:
        for pc in self.config.pairs:
            if Market(self.config.maker_exchange, pc.symbol).id not in self._warmup_books:
                return False
            if Market(self.config.taker_exchange, pc.symbol).id not in self._warmup_books:
                return False
        return True

    def _init_from_balances(self):
        first_symbol = self.config.pairs[0].symbol
        maker_jpy = self.maker_gateway.fetch_balance_sync(first_symbol)["quote"]
        taker_jpy = self.taker_gateway.fetch_balance_sync(first_symbol)["quote"]

        maker_base = {pc.symbol: self.maker_gateway.fetch_balance_sync(pc.symbol)["base"] for pc in self.config.pairs}
        taker_base = {pc.symbol: self.taker_gateway.fetch_balance_sync(pc.symbol)["base"] for pc in self.config.pairs}

        maker_prices = {pc.symbol: self._warmup_books[Market(self.config.maker_exchange, pc.symbol).id][1].mid for pc in self.config.pairs}
        taker_prices = {pc.symbol: self._warmup_books[Market(self.config.taker_exchange, pc.symbol).id][1].mid for pc in self.config.pairs}

        plan = plan_multi_allocation(
            pairs=self.config.pairs,
            maker_exchange=self.config.maker_exchange,
            taker_exchange=self.config.taker_exchange,
            jpy={self.config.maker_exchange: maker_jpy, self.config.taker_exchange: taker_jpy},
            base={self.config.maker_exchange: maker_base, self.config.taker_exchange: taker_base},
            prices={self.config.maker_exchange: maker_prices, self.config.taker_exchange: taker_prices},
            min_jpy_to_keep=self.config.min_jpy_to_keep,
        )

        if plan.case == MultiRebalanceCase.INSUFFICIENT:
            LOG.error("Insufficient funds — exiting")
            self._is_ready = True
            self.publish_status(EventType.EV_LOOP_EXIT, self)
            raise EventLoopExit()

        if plan.case == MultiRebalanceCase.REBALANCE:
            write_rebalance_file(plan.steps)
            self._is_ready = True
            self.publish_status(EventType.EV_LOOP_EXIT, self)
            raise EventLoopExit()

        maker_allocs = plan.allocations[self.config.maker_exchange]
        taker_allocs = plan.allocations[self.config.taker_exchange]

        for pc in self.config.pairs:
            sym = pc.symbol
            maker_market = Market(self.config.maker_exchange, sym)
            taker_market = Market(self.config.taker_exchange, sym)
            maker_ob, maker_mp = self._warmup_books[maker_market.id]
            taker_ob, taker_mp = self._warmup_books[taker_market.id]

            max_drawdown = pc.allocated_value_jpy * 0.3
            pnl_tracker = PnlTracker()

            maker_inv = SpotInventory(maker_market, maker_allocs[sym].quote, maker_allocs[sym].base, max_drawdown, pnl_tracker)
            maker_src = MakerSource(maker_market, pc.order_size, maker_inv)
            maker_src.gateway = self.maker_gateway
            maker_src.order_book = maker_ob
            maker_src.market_price = maker_mp

            taker_inv = SpotInventory(taker_market, taker_allocs[sym].quote, taker_allocs[sym].base, max_drawdown, pnl_tracker)
            taker_src = TakerSpotSource(taker_market, taker_inv)
            taker_src.gateway = self.taker_gateway
            taker_src.order_book = taker_ob
            taker_src.market_price = taker_mp

            pair = PairState(symbol=sym, maker=maker_src, taker=taker_src)
            pair.update_price_vars(taker_mp.mid, self.params_obj)
            pair.last_task_ts = self.now_ts
            self.pairs[sym] = pair

        LOG.info(f"Initialized {len(self.pairs)} pairs: {[s.name for s in self.pairs]}")

    def warmup_order_book(self, order_book: OrderBook):
        self.now_ts = order_book.get_ts()
        pc = self._pair_config(order_book.symbol)
        if pc is None:
            return

        market_price = order_book.market_price(pc.order_size * 3.0)
        if not market_price or not market_price.bid or not market_price.ask:
            return

        self._warmup_books[order_book.market.id] = (order_book, market_price)

        if self._all_books_ready():
            self._init_from_balances()
            self._is_ready = True

    # ── per-pair trading logic ────────────────────────────────────────────────

    def _run_pair_tasks(self, pair: PairState):
        if self.now_ts - pair.last_task_ts < 900:
            return
        pair.last_task_ts = self.now_ts

        price = pair.taker.market_price.ask
        pair.update_price_vars(price, self.params_obj)
        LOG.info(
            f"VARIABLES {pair.symbol.name} price={price:.3f}"
            f" default_profit_margin={pair.default_profit_margin:.3f}"
            f" price_tolerance={pair.price_tolerance:.3f}"
        )
        self.trading_mode = self.schedule.check(self.now_ts)

    def _pair_ok(self, pair: PairState) -> bool:
        maker_age = self.now_ts - pair.maker.market_price.get_ts()
        taker_age = self.now_ts - pair.taker.market_price.get_ts()
        return maker_age < 1.0 and taker_age < 1.0

    def _profit_margin(self, pair: PairState, side: Side) -> float:
        if side == Side.BUY and not pair.maker.inventory.can_sell(pair.maker.order_size):
            return pair.transfer_profit_margin
        if side == Side.SELL and not pair.taker.inventory.can_sell(pair.maker.order_size):
            return pair.transfer_profit_margin
        return pair.default_profit_margin

    def _suggest_side(self, pair: PairState) -> Side:
        if not pair.maker.inventory.can_sell(pair.maker.order_size):
            LOG.info(f"### SUGGEST_SIDE {pair.symbol.name} BUY (maker base low)")
            return Side.BUY
        if not pair.taker.inventory.can_sell(pair.maker.order_size):
            LOG.info(f"### SUGGEST_SIDE {pair.symbol.name} SELL (taker base low)")
            return Side.SELL

        taker_maker = pair.maker.market_price.bid - pair.taker.market_price.ask
        maker_taker = pair.taker.market_price.bid - pair.maker.market_price.ask
        side = Side.SELL if taker_maker > maker_taker else Side.BUY
        LOG.info(
            f"### SUGGEST_SIDE {pair.symbol.name} {side.name}"
            f" taker_maker={taker_maker:.3f} maker_taker={maker_taker:.3f}"
        )
        return side

    def _suggest_price(self, pair: PairState, side: Side) -> float:
        profit_margin = self._profit_margin(pair, side)
        if side == Side.BUY:
            return min(
                pair.taker.market_price.bid - profit_margin,
                pair.maker.order_book.best_ask - pair.maker.symbol_def.price_unit,
            )
        else:
            return max(
                pair.taker.market_price.ask + profit_margin,
                pair.maker.order_book.best_bid + pair.maker.symbol_def.price_unit,
            )

    def _place_orders(self, pair: PairState):
        if len(pair.maker_orders) >= self.config.max_orders:
            return

        side = self._suggest_side(pair)
        price = self._suggest_price(pair, side)
        if side == Side.BUY:
            order_request = pair.maker.attempt_buy(self.now_ts, price, SettleType.OPEN)
        else:
            order_request = pair.maker.attempt_sell(self.now_ts, price, SettleType.OPEN)

        if order_request is not None:
            pair.maker_orders[order_request.ctx_id] = MakerOrderEntry(order_request)
            self._order_to_pair[order_request.ctx_id] = pair

    def _cancel_orders(self, pair: PairState):
        for entry in pair.maker_orders.values():
            if not entry.response or entry.cancel_requested:
                continue

            current_price = self._suggest_price(pair, entry.request.side)
            if math.fabs(current_price - entry.request.price) > pair.price_tolerance:
                LOG.info(
                    f"### CANCEL {pair.symbol.name} {entry.request.ctx_id} {entry.request.side.name}"
                    f" price drift: {entry.request.price:.3f} => {current_price:.3f}"
                )
                entry.cancel_requested = True
                pair.maker.cancel(entry.response)

    # ── event handlers ────────────────────────────────────────────────────────

    def check_stop_requested(self):
        if not self.stop_requested:
            return

        all_empty = all(len(p.maker_orders) == 0 for p in self.pairs.values())
        if all_empty:
            raise EventLoopExit()

        if self.now_ts - self.stop_requested_ts > SHUTDOWN_TIMEOUT:
            total = sum(len(p.maker_orders) for p in self.pairs.values())
            LOG.warning(f"Shutdown timeout: {total} order(s) unconfirmed after {SHUTDOWN_TIMEOUT}s")
            raise EventLoopExit()

        for pair in self.pairs.values():
            for entry in pair.maker_orders.values():
                if not entry.response or entry.cancel_requested:
                    continue
                entry.cancel_requested = True
                pair.maker.cancel(entry.response)

    def before_loop(self):
        for pair in self.pairs.values():
            self._run_pair_tasks(pair)

    def handle_stop(self, _=None):
        self.stop_requested = True
        self.stop_requested_ts = self.now_ts
        self.trading_mode = TradingMode.NO_TRADING

    def handle_order_book(self, order_book: OrderBook):
        self.check_stop_requested()
        self.now_ts = order_book.get_ts()

        pair = self.pairs.get(order_book.symbol)
        if pair is None:
            return

        self._run_pair_tasks(pair)

        market_price = order_book.market_price(pair.maker.order_size * 3.0)
        if market_price:
            self.add_metric(market_price.to_metric())
            if order_book.exchange_name == self.config.maker_exchange:
                pair.maker.order_book = order_book
                pair.maker.market_price = market_price
            else:
                pair.taker.order_book = order_book
                pair.taker.market_price = market_price

        if not self._pair_ok(pair):
            return
        if self.trading_mode == TradingMode.NO_TRADING:
            return

        self._cancel_orders(pair)
        if self.trading_mode == TradingMode.NO_ENTRY:
            return
        self._place_orders(pair)

    def handle_order_created(self, order_response: OrderResponse):
        super().handle_order_created(order_response)
        ctx_id = order_response.order_request.ctx_id
        pair = self._order_to_pair.get(ctx_id)
        if pair and order_response.order_request.order_type == OrderType.MAKER:
            pair.maker_orders[ctx_id].response = order_response

    def handle_order_cancelled(self, order_response: OrderResponse):
        super().handle_order_cancelled(order_response)
        ctx_id = order_response.order_request.ctx_id
        pair = self._order_to_pair.pop(ctx_id, None)
        if pair:
            pair.maker.inventory.release(order_response.order_request)
            del pair.maker_orders[ctx_id]

    def handle_order_filled(self, order_response: OrderResponse):
        super().handle_order_filled(order_response)
        ctx_id = order_response.order_request.ctx_id
        pair = self._order_to_pair.pop(ctx_id, None)
        if pair is None:
            return
        if order_response.order_request.order_type == OrderType.MAKER:
            self._on_maker_filled(pair, order_response)
        else:
            self._on_taker_filled(pair, order_response)

    def _on_taker_filled(self, pair: PairState, order_response: OrderResponse):
        pair.taker.inventory.release(order_response.order_request)
        pnl_update = pair.taker.inventory.process_order(order_response)
        self.publish_pnl_update(pnl_update)

    def _on_maker_filled(self, pair: PairState, order_response: OrderResponse):
        pair.maker.inventory.release(order_response.order_request)
        pnl_update = pair.maker.inventory.process_order(order_response)
        self.publish_pnl_update(pnl_update)

        ctx_id = order_response.order_request.ctx_id
        if ctx_id in pair.maker_orders:
            del pair.maker_orders[ctx_id]

        if order_response.exec_size < pair.taker.symbol_def.size_unit:
            LOG.info(f"Ignore small exec_size {order_response.exec_size:.6f} < {pair.taker.symbol_def.size_unit}")
            return

        if order_response.order_request.side == Side.SELL:
            taker_req = pair.taker.buy(self.now_ts, order_response.exec_size, SettleType.CLOSE)
        else:
            taker_req = pair.taker.sell(self.now_ts, order_response.exec_size, SettleType.CLOSE)

        self._order_to_pair[taker_req.ctx_id] = pair
