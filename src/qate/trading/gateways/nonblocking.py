import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from qate.core.ev_type import EventType
from qate.core.model import FatalException
from qate.core.order import (
    ExchangeExecution,
    ExchangeOrder,
    OrderError,
    OrderRequest,
    OrderResponse,
    OrderState,
    OrderTracker,
)
from qate.core.order_api import OrderApi

from .default import DefaultGateway

LOG = getLogger(__name__)

EVENT_ASYNC_CREATE_RESULT = "EVENT_ASYNC_CREATE_ORDER_RESULT"
EVENT_ASYNC_CANCEL_RESULT = "EVENT_ASYNC_CANCEL_ORDER_RESULT"
_EVENT_STOP = "EVENT_ASYNC_GATEWAY_STOP"


@dataclass
class _AsyncOrderResult:
    order_request: OrderRequest
    payload: Any = None
    error: BaseException | None = None


class DefaultGatewayAsync(DefaultGateway):
    """Async create/cancel, with all order state owned by the gateway thread.

    Stop drains submitted operations and their results before closing the API.
    As with the synchronous API, transports must bound their request duration.
    Reconciliation and balance queries still use the synchronous API.
    """

    def __init__(self, api, order_api: OrderApi, heartbeat_interval_ts: float = 3):
        super().__init__(api, heartbeat_interval_ts=heartbeat_interval_ts)
        self._order_api = order_api
        self._pending_operations = 0
        self._pending_creates: set[int] = set()
        self._early_events: dict[str, list[tuple[str, Any]]] = {}
        self._execution_ids: dict[str, set[str]] = {}
        self._stop_requested = threading.Event()
        self._stopping = False
        self._is_async_shutdown = False
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.register(EVENT_ASYNC_CREATE_RESULT, self._create_result)
        self.register(EVENT_ASYNC_CANCEL_RESULT, self._cancel_result)
        self.register(_EVENT_STOP, self._handle_stop)
        self._async_thread.start()

    def _run_async_loop(self) -> None:
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def create(self, order_request: OrderRequest) -> None:
        if self._stop_requested.is_set():
            raise RuntimeError("Gateway is stopping")
        super().create(order_request)

    def cancel(self, order_request: OrderRequest) -> None:
        if self._stop_requested.is_set():
            raise RuntimeError("Gateway is stopping")
        super().cancel(order_request)

    def _submit_async(
        self,
        order_request: OrderRequest,
        event_type: str,
        operation: Callable[[OrderRequest], Awaitable[Any]],
    ) -> None:
        async def runner() -> None:
            try:
                result = await operation(order_request)
            except (Exception, asyncio.CancelledError) as exc:
                self.put(event_type, _AsyncOrderResult(order_request, error=exc))
            else:
                self.put(event_type, _AsyncOrderResult(order_request, payload=result))

        self._pending_operations += 1
        asyncio.run_coroutine_threadsafe(runner(), self._async_loop)

    def handle_create_order(self, order_request: OrderRequest):
        if self._stopping:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))
            return
        self._pending_creates.add(order_request.ctx_id)
        self._submit_async(order_request, EVENT_ASYNC_CREATE_RESULT, self._order_api.create_order)

    def _create_result(self, result: _AsyncOrderResult) -> None:
        try:
            self._handle_async_create_result(result)
        finally:
            self._pending_creates.remove(result.order_request.ctx_id)
            if not self._pending_creates:
                self._early_events.clear()
            self._operation_finished()

    def _handle_async_create_result(self, result: _AsyncOrderResult) -> None:
        order_request = result.order_request
        if isinstance(result.error, FatalException):
            raise result.error
        if result.error:
            LOG.error("Failed to create order ctx_id=%s", order_request.ctx_id, exc_info=result.error)
            self.publish_status(EventType.EXCEPTION, result.error)
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))
            return
        order_id = result.payload
        if order_id is None:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))
            return
        order_tracker = OrderTracker(order_request, order_id, time.time())
        self.exchange_orders[order_id] = order_tracker
        self.publish_order_created(order_tracker.created_response())
        for event_type, event in self._early_events.pop(order_id, []):
            if event_type == EventType.EXCHANGE_EXECUTION:
                self.handle_exchange_execution(event)
            else:
                self.handle_exchange_order(event)

    def handle_exchange_execution(self, execution: ExchangeExecution):
        order_id = execution.order_id
        if order_id not in self.exchange_orders:
            if self._pending_creates:
                self._early_events.setdefault(order_id, []).append((EventType.EXCHANGE_EXECUTION, execution))
            return
        if execution.trade_id is not None:
            seen = self._execution_ids.setdefault(order_id, set())
            if execution.trade_id in seen:
                return
            seen.add(execution.trade_id)
        super().handle_exchange_execution(execution)
        if order_id not in self.exchange_orders:
            self._execution_ids.pop(order_id, None)

    def handle_exchange_order(self, exchange_order: ExchangeOrder):
        order_id = exchange_order.order_id
        if order_id not in self.exchange_orders:
            if self._pending_creates:
                self._early_events.setdefault(order_id, []).append((EventType.EXCHANGE_ORDER, exchange_order))
            return
        super().handle_exchange_order(exchange_order)
        if order_id not in self.exchange_orders:
            self._execution_ids.pop(order_id, None)

    def handle_cancel_order(self, order_request: OrderRequest):
        tracker = self.exchange_orders.get(order_request.order_id)
        if tracker is None:
            return  # The fill/cancel race already has a terminal response queued.
        if self._stopping:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CANCEL))
            return
        self._submit_async(order_request, EVENT_ASYNC_CANCEL_RESULT, self._order_api.cancel_order)

    def _cancel_result(self, result: _AsyncOrderResult) -> None:
        try:
            self._handle_async_cancel_result(result)
        finally:
            self._operation_finished()

    def _handle_async_cancel_result(self, result: _AsyncOrderResult) -> None:
        order_request = result.order_request
        if isinstance(result.error, FatalException):
            raise result.error
        if order_request.order_id not in self.exchange_orders:
            return  # An authoritative terminal event won the race.
        if result.error:
            LOG.error("Failed to cancel order ctx_id=%s", order_request.ctx_id, exc_info=result.error)
            self.publish_status(EventType.EXCEPTION, result.error)
        if result.error or not result.payload:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CANCEL))
            return

        # Acceptance is not a terminal cancellation. Keep tracking executions
        # until the private feed or a status query confirms the final state.
        tracker = self.exchange_orders[order_request.order_id]
        tracker.state = OrderState.CANCEL_START
        # Make it eligible for the next reconciliation pass without blocking
        # this result handler on a synchronous status request.
        tracker.last_recon_ts = 0

    def _operation_finished(self):
        self._pending_operations -= 1
        if self._stopping and not self._pending_operations:
            super().stop()

    def _handle_stop(self, _):
        self._stopping = True
        if not self._pending_operations:
            super().stop()

    def stop(self) -> None:
        if not self._stop_requested.is_set():
            self._stop_requested.set()
            self.put(_EVENT_STOP)
        if self.ident is None:
            self._shutdown_async()

    def after_loop(self):
        # Fatal exceptions also exit EventLoop without going through stop().
        # Drain other operations even if one of their siblings failed fatally.
        self._stop_requested.set()
        self._stopping = True
        try:
            while self._pending_operations:
                self.tick()
        finally:
            self._shutdown_async()

    def _shutdown_async(self) -> None:
        if self._is_async_shutdown:
            return
        self._is_async_shutdown = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._order_api.aclose(), self._async_loop)
            future.result(timeout=5)
        except Exception as error:
            # A failed transport close must still let EventLoop publish its exit.
            LOG.exception("Failed to close async order API")
            self.publish_status(EventType.EXCEPTION, error)
        finally:
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
            self._async_thread.join()
            self._async_loop.close()
