import asyncio
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any, Awaitable, Callable

from qate.core.ev_type import EventType
from qate.core.order import (
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


@dataclass
class _AsyncOrderResult:
    order_request: OrderRequest
    payload: Any = None
    error: BaseException | None = None


class DefaultGatewayAsync(DefaultGateway):
    """Default gateway variant that sends create/cancel through an async API."""

    def __init__(self, api, order_api: OrderApi, heartbeat_interval_ts: float = 3):
        super().__init__(api, heartbeat_interval_ts=heartbeat_interval_ts)
        self._order_api = order_api
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._async_thread.start()

        self.register(EVENT_ASYNC_CREATE_RESULT, self._handle_async_create_result)
        self.register(EVENT_ASYNC_CANCEL_RESULT, self._handle_async_cancel_result)

        self._is_async_shutdown = False

    def _run_async_loop(self) -> None:
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def _submit_async(
        self,
        order_request: OrderRequest,
        event_type: str,
        operation: Callable[[OrderRequest], Awaitable[Any]],
    ) -> None:
        async def runner() -> None:
            try:
                result = await operation(order_request)
                self.put(event_type, _AsyncOrderResult(order_request, result, None))
            except Exception as exc:  # noqa: BLE001 - capture any transport issues
                self.put(event_type, _AsyncOrderResult(order_request, None, exc))

        asyncio.run_coroutine_threadsafe(runner(), self._async_loop)

    def handle_create_order(self, order_request: OrderRequest):  # type: ignore[override]
        self._submit_async(order_request, EVENT_ASYNC_CREATE_RESULT, self._order_api.create_order)

    def _handle_async_create_result(self, result: _AsyncOrderResult) -> None:
        order_request = result.order_request
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
        LOG.info(
            f"ORDER CREATED ctx_id={order_request.ctx_id} order_id={order_id} "
            f"{order_request.side.name} size={order_request.size:.3f} price={order_request.price:.3f}"
        )
        self.publish_order_created(order_tracker.created_response())

    def handle_cancel_order(self, order_request: OrderRequest):
        if order_request.order_id not in self.exchange_orders:
            LOG.info(
                "Cancelling order %s but it is already filled or cancelled",
                order_request.ctx_id,
            )
            return
        self._submit_async(order_request, EVENT_ASYNC_CANCEL_RESULT, self._order_api.cancel_order)

    def _handle_async_cancel_result(self, result: _AsyncOrderResult) -> None:
        order_request = result.order_request
        if result.error:
            LOG.error("Failed to cancel order ctx_id=%s", order_request.ctx_id, exc_info=result.error)
            self.publish_status(EventType.EXCEPTION, result.error)
            return

        if order_request.order_id not in self.exchange_orders:
            return

        if not result.payload:
            LOG.warning("Exchange refused to cancel order ctx_id=%s", order_request.ctx_id)
            return

        order_tracker = self.exchange_orders[order_request.order_id]
        LOG.info("order cancelled ctx_id=%s retry=%s", order_request.ctx_id, order_tracker.retry_cnt)
        order_tracker.on_exchange_order(
            ExchangeOrder(
                order_request.order_id,
                0,
                0,
                OrderState.CANCELLED,
                time.time(),
            )
        )
        self.publish_order_cancelled(order_tracker.cancelled_response())
        del self.exchange_orders[order_request.order_id]

    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        self._shutdown_async()

    def join(self, timeout: float | None = None) -> None:  # type: ignore[override]
        super().join(timeout=timeout)
        if self._async_thread.is_alive():
            self._async_thread.join(timeout)

    def _shutdown_async(self) -> None:
        if self._is_async_shutdown:
            return
        self._is_async_shutdown = True

        try:
            future = asyncio.run_coroutine_threadsafe(self._order_api.aclose(), self._async_loop)
            future.result(timeout=5)
        except Exception:  # noqa: BLE001 - surface later during stop
            LOG.debug("order_api.aclose failed", exc_info=True)

        self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._async_thread.is_alive():
            self._async_thread.join(timeout=5)
        self._async_loop.close()
