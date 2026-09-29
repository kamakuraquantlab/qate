import math
from dataclasses import dataclass
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
from qate.core.order import OrderError, OrderRequest, OrderResponse, OrderType
from qate.trading.gateways import DefaultGateway
from qate.trading.inventory import SpotInventory
from qate.trading.pnl_tracker import PnlTracker
from qate.trading.strategy import Strategy

from .adjust_inventory import AllocationPlan, RebalanceCase, plan_allocation
from .config import Config
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
            data.get("default_profit_margin_percentage", 0.0007),
            data.get("transfer_profit_margin_percentage", 0.0001),
            data.get("price_tolerance_percentage", 0.0001),
        )


class Variant(Strategy[Config]):
    def __init__(self, config: Config, params: dict):
        super().__init__(config, params)
        self.params_obj = Params.from_dict(params)

        self.maker_gateway: DefaultGateway = None
        self.taker_gateway: DefaultGateway = None
        self.maker: MakerSource = None
        self.taker: TakerSpotSource = None

        self._warmup_books: dict = {}

        self.trading_mode = TradingMode.NO_TRADING
        self.maker_orders: dict[int, MakerOrderEntry] = {}
        self.schedule = Schedule()

        self.default_profit_margin = 0.0
        self.transfer_profit_margin = 0.0
        self.price_tolerance = 0.0
        self.last_task_ts = 0.0
        self.now_ts = 0.0
        self.stop_requested = False
        self.stop_requested_ts = 0.0
        self._pending_taker: dict[int, OrderResponse] = {}  # taker ctx_id → maker fill response

    def add_gateway(self, gateway: DefaultGateway):
        if gateway.exchange_name == self.config.maker_exchange:
            self.maker_gateway = gateway
        else:
            self.taker_gateway = gateway

    def _init_from_balances(self):
        pc = self.config.pairs[0]
        maker_market = Market(self.config.maker_exchange, pc.symbol)
        taker_market = Market(self.config.taker_exchange, pc.symbol)

        maker_ob, maker_mp = self._warmup_books[maker_market.id]
        taker_ob, taker_mp = self._warmup_books[taker_market.id]

        maker_balance = self.maker_gateway.fetch_balance_sync(pc.symbol)
        taker_balance = self.taker_gateway.fetch_balance_sync(pc.symbol)

        plan = plan_allocation(
            symbol=pc.symbol,
            balances={
                self.config.maker_exchange: maker_balance,
                self.config.taker_exchange: taker_balance,
            },
            prices={
                self.config.maker_exchange: maker_mp.mid,
                self.config.taker_exchange: taker_mp.mid,
            },
            allocated_value_jpy=pc.allocated_value_jpy,
            min_jpy_to_keep=self.config.min_jpy_to_keep,
            order_size=pc.order_size,
        )

        self._handle_allocation_plan(plan, pc.symbol)

        maker_alloc = plan.allocations[self.config.maker_exchange]
        taker_alloc = plan.allocations[self.config.taker_exchange]

        max_drawdown = pc.allocated_value_jpy * 0.3
        pnl_tracker = PnlTracker()

        maker_inv = SpotInventory(maker_market, maker_alloc.quote, maker_alloc.base, max_drawdown, pnl_tracker)
        self.maker = MakerSource(maker_market, pc.order_size, maker_inv)
        self.maker.gateway = self.maker_gateway
        self.maker.order_book = maker_ob
        self.maker.market_price = maker_mp

        taker_inv = SpotInventory(taker_market, taker_alloc.quote, taker_alloc.base, max_drawdown, pnl_tracker)
        self.taker = TakerSpotSource(taker_market, taker_inv)
        self.taker.gateway = self.taker_gateway
        self.taker.order_book = taker_ob
        self.taker.market_price = taker_mp

        self._update_price_vars(taker_mp.mid)
        LOG.info(
            f"VARIABLES price={taker_mp.mid:.3f}"
            f" default_profit_margin={self.default_profit_margin:.3f}"
            f" transfer_profit_margin={self.transfer_profit_margin:.3f}"
            f" price_tolerance={self.price_tolerance:.3f}"
        )
        self.trading_mode = self.schedule.check(self.now_ts)
        self.last_task_ts = self.now_ts

    def _handle_allocation_plan(self, plan: AllocationPlan, symbol):
        if plan.case == RebalanceCase.OK:
            return

        if plan.case == RebalanceCase.INSUFFICIENT:
            LOG.error(f"Insufficient funds to trade {symbol.name} — exiting")
        else:
            self._write_rebalance_file(plan, symbol)

        self._is_ready = True  # let Warmup exit cleanly
        self.publish_status(EventType.EV_LOOP_EXIT, self)  # signal Bootstrap to stop
        raise EventLoopExit()

    def _write_rebalance_file(self, plan: AllocationPlan, symbol):
        import json

        path = "rebalance.jsonl"
        with open(path, "w") as f:
            for step in plan.steps:
                f.write(
                    json.dumps(
                        {
                            "exchange": step.exchange.name,
                            "symbol": step.symbol.name,
                            "side": step.side.name,
                            "amount": round(step.amount, 8),
                        }
                    )
                    + "\n"
                )
        action = "BUY" if plan.case == RebalanceCase.BUY_BASE else "SELL"
        LOG.error(
            f"Inventory imbalance for {symbol.name}: need to {action} base before trading."
            f" Rebalance orders written to: {path}"
        )
        for step in plan.steps:
            LOG.error(f"  {step.side.name} {step.amount:.4f} {step.symbol.name} on {step.exchange.name}")
        LOG.error(f"Execute these with corvus before restarting: {path}")

    def _update_price_vars(self, price: float):
        self.default_profit_margin = price * self.params_obj.default_profit_margin_percentage
        self.transfer_profit_margin = price * self.params_obj.transfer_profit_margin_percentage
        self.price_tolerance = price * self.params_obj.price_tolerance_percentage

    def warmup_order_book(self, order_book: OrderBook):
        self.now_ts = order_book.get_ts()
        pc = self.config.pairs[0]
        market_price = order_book.market_price(pc.order_size * 3.0)
        if not market_price or not market_price.bid or not market_price.ask:
            return

        self._warmup_books[order_book.market.id] = (order_book, market_price)

        maker_id = Market(self.config.maker_exchange, pc.symbol).id
        taker_id = Market(self.config.taker_exchange, pc.symbol).id
        if maker_id in self._warmup_books and taker_id in self._warmup_books:
            self._init_from_balances()
            self._is_ready = True

    def run_tasks(self):
        if self.now_ts - self.last_task_ts < 900:
            return
        self.last_task_ts = self.now_ts

        price = self.taker.market_price.ask
        self._update_price_vars(price)
        LOG.info(
            f"VARIABLES price={price:.3f}"
            f" default_profit_margin={self.default_profit_margin:.3f}"
            f" transfer_profit_margin={self.transfer_profit_margin:.3f}"
            f" price_tolerance={self.price_tolerance:.3f}"
        )
        self.trading_mode = self.schedule.check(self.now_ts)

    def check_stop_requested(self):
        if not self.stop_requested:
            return

        if len(self.maker_orders) == 0:
            raise EventLoopExit()

        if self.now_ts - self.stop_requested_ts > SHUTDOWN_TIMEOUT:
            LOG.warning(f"Shutdown timeout: {len(self.maker_orders)} order(s) unconfirmed after {SHUTDOWN_TIMEOUT}s")
            raise EventLoopExit()

        for entry in self.maker_orders.values():
            if not entry.response or entry.cancel_requested:
                continue
            entry.cancel_requested = True
            self.maker.cancel(entry.response)

    def before_loop(self):
        self.run_tasks()

    def handle_stop(self, _=None):
        self.stop_requested = True
        self.stop_requested_ts = self.now_ts
        self.trading_mode = TradingMode.NO_TRADING

    def suggest_side(self) -> Side:
        maker_can_sell = self.maker.inventory.can_sell(self.maker.order_size)
        taker_can_sell = self.taker.inventory.can_sell(self.maker.order_size)

        if not maker_can_sell:
            LOG.info(
                f"### SUGGEST_SIDE BUY (maker base low)"
                f" maker={self.maker.inventory.available_base:.4f}"
                f" taker={self.taker.inventory.available_base:.4f}"
            )
            return Side.BUY
        if not taker_can_sell:
            LOG.info(
                f"### SUGGEST_SIDE SELL (taker base low)"
                f" maker={self.maker.inventory.available_base:.4f}"
                f" taker={self.taker.inventory.available_base:.4f}"
            )
            return Side.SELL

        taker_maker = self.maker.market_price.bid - self.taker.market_price.ask
        maker_taker = self.taker.market_price.bid - self.maker.market_price.ask
        side = Side.SELL if taker_maker > maker_taker else Side.BUY
        LOG.info(
            f"### SUGGEST_SIDE {side.name}"
            f" taker_maker={taker_maker:.3f} maker_taker={maker_taker:.3f}"
            f" maker={self.maker.inventory.available_base:.4f}"
            f" taker={self.taker.inventory.available_base:.4f}"
        )
        return side

    def _profit_margin(self, side: Side) -> float:
        if side == Side.BUY and not self.maker.inventory.can_sell(self.maker.order_size):
            return self.transfer_profit_margin
        if side == Side.SELL and not self.taker.inventory.can_sell(self.maker.order_size):
            return self.transfer_profit_margin
        return self.default_profit_margin

    def suggest_price(self, side: Side) -> float:
        profit_margin = self._profit_margin(side)
        if side == Side.BUY:
            return min(
                self.taker.market_price.bid - profit_margin,
                self.maker.order_book.best_ask - self.maker.symbol_def.price_unit,
            )
        else:
            return max(
                self.taker.market_price.ask + profit_margin,
                self.maker.order_book.best_bid + self.maker.symbol_def.price_unit,
            )

    def place_orders(self):
        if len(self.maker_orders) >= self.config.max_orders:
            return

        side = self.suggest_side()
        price = self.suggest_price(side)
        if side == Side.BUY:
            order_request = self.maker.attempt_buy(self.now_ts, price, SettleType.OPEN)
        else:
            order_request = self.maker.attempt_sell(self.now_ts, price, SettleType.OPEN)

        if order_request is not None:
            self.maker_orders[order_request.ctx_id] = MakerOrderEntry(order_request)

    def cancel_orders(self):
        for entry in self.maker_orders.values():
            if not entry.response or entry.cancel_requested:
                continue

            order_request = entry.request
            current_price = self.suggest_price(order_request.side)
            if math.fabs(current_price - order_request.price) > self.price_tolerance:
                LOG.info(
                    f"### CANCEL {order_request.ctx_id} {order_request.side.name}"
                    f" price drift: {order_request.price:.3f} => {current_price:.3f}"
                )
                entry.cancel_requested = True
                self.maker.cancel(entry.response)

    def handle_order_book(self, order_book: OrderBook):
        self.check_stop_requested()
        self.now_ts = order_book.get_ts()
        self.run_tasks()

        market_price = order_book.market_price(self.maker.order_size * 3.0)
        if market_price:
            self.add_metric(market_price.to_metric())
            if order_book.exchange_name == self.config.maker_exchange:
                self.maker.order_book = order_book
                self.maker.market_price = market_price
            else:
                self.taker.order_book = order_book
                self.taker.market_price = market_price

        if not self._pair_ok():
            return
        if self.trading_mode == TradingMode.NO_TRADING:
            return

        self.cancel_orders()
        if self.trading_mode == TradingMode.NO_ENTRY:
            return
        self.place_orders()

    def _pair_ok(self) -> bool:
        maker_age = self.now_ts - self.maker.market_price.get_ts()
        taker_age = self.now_ts - self.taker.market_price.get_ts()
        return maker_age < 1.0 and taker_age < 1.0

    def handle_order_created(self, order_response: OrderResponse):
        super().handle_order_created(order_response)
        if order_response.order_request.order_type == OrderType.MAKER:
            entry = self.maker_orders[order_response.order_request.ctx_id]
            entry.response = order_response

    def handle_order_cancelled(self, order_response: OrderResponse):
        super().handle_order_cancelled(order_response)
        self.maker.inventory.release(order_response.order_request)
        del self.maker_orders[order_response.order_request.ctx_id]

    def handle_order_filled(self, order_response: OrderResponse):
        super().handle_order_filled(order_response)
        if order_response.order_request.order_type == OrderType.MAKER:
            self._on_maker_filled(order_response)
        else:
            self._on_taker_filled(order_response)

    def handle_order_error(self, order_response):
        super().handle_order_error(order_response)
        if order_response.error == OrderError.CREATE:
            req = order_response.order_request
            if req.order_type == OrderType.MAKER:
                self.maker.inventory.release(req)
                del self.maker_orders[req.ctx_id]
            else:
                # Taker close failed — unwind the maker open so the tracker stays consistent.
                self.taker.inventory.release(req)
                maker_response = self._pending_taker.pop(req.ctx_id, None)
                if maker_response:
                    self.maker.inventory.tracker.unwind_open(maker_response)

    def _on_taker_filled(self, order_response: OrderResponse):
        self.taker.inventory.release(order_response.order_request)
        self._pending_taker.pop(order_response.order_request.ctx_id, None)
        pnl_update = self.taker.inventory.process_order(order_response)
        self.publish_pnl_update(pnl_update)

    def _on_maker_filled(self, order_response: OrderResponse):
        self.maker.inventory.release(order_response.order_request)
        pnl_update = self.maker.inventory.process_order(order_response)
        self.publish_pnl_update(pnl_update)

        ctx_id = order_response.order_request.ctx_id
        if ctx_id in self.maker_orders:
            del self.maker_orders[ctx_id]

        if order_response.exec_size < self.taker.symbol_def.size_unit:
            LOG.info(f"Ignore small exec_size {order_response.exec_size:.6f} < {self.taker.symbol_def.size_unit}")
            return

        if order_response.order_request.side == Side.SELL:
            taker_req = self.taker.buy(self.now_ts, order_response.exec_size, SettleType.CLOSE)
        else:
            taker_req = self.taker.sell(self.now_ts, order_response.exec_size, SettleType.CLOSE)
        self._pending_taker[taker_req.ctx_id] = order_response
