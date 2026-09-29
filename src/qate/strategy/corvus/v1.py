from dataclasses import dataclass
from logging import getLogger

from qate.core.model import EventLoopExit, Market, OrderBook, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderType
from qate.trading.gateway import DefaultGateway
from qate.trading.strategy import Strategy

from .config import Config, OrderInstruction

LOG = getLogger(__name__)
EPSILON = 1e-6


@dataclass
class PendingOrder:
    instruction: OrderInstruction
    ctx_id: int = None
    response: OrderResponse = None
    fill_response: OrderResponse = None
    placed_at: float = 0.0
    placed_price: float = 0.0
    filled: bool = False
    cancel_requested: bool = False

    def is_placed(self) -> bool:
        return self.ctx_id is not None

    def is_competitive(self, target_price: float, now_ts: float, reprice_interval: float) -> bool:
        if self.response is None or self.cancel_requested:
            return True
        if abs(self.placed_price - target_price) < EPSILON:
            return True
        if now_ts - self.placed_at < reprice_interval:
            return True
        return False


class Variant(Strategy[Config]):
    def __init__(self, config: Config, params: dict):
        super().__init__(config, params)
        self.gateways: dict = {}
        self.pending: list[PendingOrder] = [PendingOrder(i) for i in config.instructions]
        self._books: dict[str, OrderBook] = {}
        self._stop_requested: bool = False
        self.now_ts: float = 0.0

    def add_gateway(self, gateway: DefaultGateway):
        self.gateways[gateway.exchange_name] = gateway

    def warmup_order_book(self, order_book: OrderBook):
        self._books[order_book.market.id] = order_book
        self.now_ts = order_book.get_ts()
        required = {Market(i.exchange, i.symbol).id for i in self.config.instructions}
        if required.issubset(self._books.keys()):
            self._is_ready = True

    def handle_order_book(self, order_book: OrderBook):
        self.now_ts = order_book.get_ts()
        if self._stop_requested:
            return
        if not (order_book.best_ask and order_book.best_bid):
            return

        mid = order_book.market.id
        in_flight = any(
            not p.filled and p.is_placed()
            and Market(p.instruction.exchange, p.instruction.symbol).id == mid
            for p in self.pending
        )
        for p in self.pending:
            if p.filled or Market(p.instruction.exchange, p.instruction.symbol).id != mid:
                continue

            target_price = self._best_price(order_book, p.instruction.side)
            if not p.is_placed():
                if not in_flight:
                    self._place(p, target_price)
                    in_flight = True
                continue
            if not p.is_competitive(target_price, self.now_ts, self.config.reprice_interval):
                LOG.info(
                    f"CORVUS REPRICE {p.instruction.side.name} {p.instruction.symbol.name}"
                    f" {p.placed_price} => {target_price} on {p.instruction.exchange.name}"
                )
                p.cancel_requested = True
                self._cancel(p)

    def _best_price(self, order_book: OrderBook, side: Side):
        return order_book.best_bid if side == Side.BUY else order_book.best_ask

    def _place(self, p: PendingOrder, price: float):
        instr = p.instruction
        market = Market(instr.exchange, instr.symbol)
        request = OrderRequest(
            self.now_ts, market, instr.side, price, instr.amount, SettleType.OPEN, OrderType.MAKER
        )
        self.gateways[instr.exchange].create(request)
        p.ctx_id = request.ctx_id
        p.placed_at = self.now_ts
        p.placed_price = price
        LOG.info(
            f"CORVUS PLACE {instr.side.name} {instr.amount} {instr.symbol.name}" f" @ {price} on {instr.exchange.name}"
        )

    def _cancel(self, p: PendingOrder):
        request = p.response.order_request
        request.order_id = p.response.order_id
        self.gateways[p.instruction.exchange].cancel(request)

    def handle_order_created(self, order_response: OrderResponse):
        super().handle_order_created(order_response)
        ctx_id = order_response.order_request.ctx_id
        for p in self.pending:
            if p.ctx_id == ctx_id:
                p.response = order_response
                if self._stop_requested:
                    p.cancel_requested = True
                    self._cancel(p)
                break

    def handle_order_filled(self, order_response: OrderResponse):
        super().handle_order_filled(order_response)
        ctx_id = order_response.order_request.ctx_id
        for p in self.pending:
            if p.ctx_id == ctx_id:
                p.filled = True
                p.fill_response = order_response
                LOG.info(
                    f"CORVUS FILLED {order_response.order_request.side.name}"
                    f" {order_response.exec_size} {order_response.order_request.market.symbol.name}"
                    f" on {order_response.order_request.market.exchange_name.name}"
                )
                break
        self._check_all_done()

    def handle_order_cancelled(self, order_response: OrderResponse):
        super().handle_order_cancelled(order_response)
        ctx_id = order_response.order_request.ctx_id
        for p in self.pending:
            if p.ctx_id == ctx_id:
                p.ctx_id = None
                p.response = None
                p.cancel_requested = False
                if self._stop_requested:
                    self._check_all_done()
                else:
                    LOG.info(
                        f"CORVUS repricing {p.instruction.side.name}"
                        f" {p.instruction.symbol.name} on {p.instruction.exchange.name}"
                        f" — will re-place on next book update"
                    )
                break

    def handle_order_error(self, order_response: OrderResponse):
        super().handle_order_error(order_response)
        ctx_id = order_response.order_request.ctx_id
        for p in self.pending:
            if p.ctx_id == ctx_id:
                LOG.warning(
                    f"CORVUS ORDER ERROR {p.instruction.side.name} {p.instruction.symbol.name}"
                    f" on {p.instruction.exchange.name} — will retry on next book update"
                )
                p.ctx_id = None
                p.response = None
                break

    def handle_stop(self, _=None):
        self._stop_requested = True
        for p in self.pending:
            if p.filled or p.cancel_requested or p.response is None:
                continue
            p.cancel_requested = True
            self._cancel(p)
        self._check_all_done()

    def _check_all_done(self):
        if self._stop_requested:
            done = all(p.filled or p.ctx_id is None for p in self.pending)
        else:
            done = all(p.filled for p in self.pending)
        if done:
            LOG.info("CORVUS all orders done — exiting")
            raise EventLoopExit()
