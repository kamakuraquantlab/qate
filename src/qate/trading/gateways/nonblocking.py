import asyncio
import concurrent.futures
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
EVENT_ASYNC_RECONCILIATION_RESULT = "EVENT_ASYNC_RECONCILIATION_RESULT"
_EVENT_STOP = "EVENT_ASYNC_GATEWAY_STOP"


@dataclass
class _AsyncOrderResult:
    order_request: OrderRequest | None
    operation_id: int = -1
    event_type: str = ""
    payload: Any = None
    error: BaseException | None = None
    submitted_ts: float = 0.0


@dataclass(frozen=True)
class AsyncGatewayStats:
    pending_operations: int
    peak_pending_operations: int
    submitted_operations: int
    completed_operations: int
    failed_operations: int
    rejected_operations: int
    early_events: int
    reconciliation_runs: int
    reconciled_orders: int
    reconciliation_events: int
    average_latency_ms: float
    max_latency_ms: float


class DefaultGatewayAsync(DefaultGateway):
    """Async create/cancel, with all order state owned by the gateway thread.

    Stop drains submitted operations and their results before closing the API.
    As with the synchronous API, transports must bound their request duration.
    Reconciliation uses the async API and returns its results to the gateway thread
    for state mutation. Balance remains synchronous: it is fetched during strategy
    startup in practice, not on the gateway's order-event hot path, so making it
    asynchronous would add coordination without improving gateway responsiveness.
    """

    def __init__(
        self,
        api,
        order_api: OrderApi,
        heartbeat_interval_ts: float = 3,
        *,
        max_concurrent_operations: int = 8,
        max_pending_operations: int = 64,
        shutdown_timeout_ts: float = 10.0,
    ):
        if max_concurrent_operations < 1:
            raise ValueError("max_concurrent_operations must be positive")
        if max_pending_operations < max_concurrent_operations:
            raise ValueError("max_pending_operations must be at least max_concurrent_operations")
        if shutdown_timeout_ts <= 0:
            raise ValueError("shutdown_timeout_ts must be positive")
        super().__init__(
            api,
            heartbeat_interval_ts=heartbeat_interval_ts,
            schedule_reconciliation=False,
        )
        self._order_api = order_api
        self._max_pending_operations = max_pending_operations
        self._shutdown_timeout_ts = shutdown_timeout_ts
        self._pending_operations = 0
        self._pending_creates: set[int] = set()
        self._early_events: dict[str, list[tuple[str, Any]]] = {}
        self._execution_ids: dict[str, set[str]] = {}
        self._stop_requested = threading.Event()
        self._stopping = False
        self._shutdown_deadline: float | None = None
        self._is_async_shutdown = False
        self._async_loop = asyncio.new_event_loop()
        self._async_semaphore = asyncio.Semaphore(max_concurrent_operations)
        self._async_futures: dict[int, concurrent.futures.Future] = {}
        self._pending_async_requests: dict[int, tuple[str, OrderRequest | None, float]] = {}
        self._next_operation_id = 0
        self._async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._peak_pending_operations = 0
        self._submitted_operations = 0
        self._completed_operations = 0
        self._failed_operations = 0
        self._rejected_operations = 0
        self._early_event_count = 0
        self._reconciliation_pending = False
        self._reconciliation_checked_orders = 0
        self._reconciliation_runs = 0
        self._reconciled_orders = 0
        self._reconciliation_events = 0
        self._total_latency_ts = 0.0
        self._max_latency_ts = 0.0
        self.register(EVENT_ASYNC_CREATE_RESULT, self._create_result)
        self.register(EVENT_ASYNC_CANCEL_RESULT, self._cancel_result)
        self.register(EVENT_ASYNC_RECONCILIATION_RESULT, self._reconciliation_result)
        self.register(_EVENT_STOP, self._handle_stop)
        self.repeat(5, AsyncReconOrdersCallable())
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
    ) -> bool:
        return self._submit_operation(
            order_request,
            event_type,
            lambda: operation(order_request),
        )

    def _submit_operation(
        self,
        order_request: OrderRequest | None,
        event_type: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> bool:
        if self._pending_operations >= self._max_pending_operations:
            self._rejected_operations += 1
            LOG.error(
                "Async gateway capacity exhausted pending=%s limit=%s operation=%s ctx_id=%s",
                self._pending_operations,
                self._max_pending_operations,
                event_type,
                order_request.ctx_id if order_request is not None else None,
            )
            return False
        submitted_ts = time.monotonic()
        operation_id = self._next_operation_id
        self._next_operation_id += 1

        async def runner() -> None:
            try:
                async with self._async_semaphore:
                    result = await operation()
            except (Exception, asyncio.CancelledError) as exc:
                self.put(
                    event_type,
                    _AsyncOrderResult(
                        order_request,
                        operation_id=operation_id,
                        event_type=event_type,
                        error=exc,
                        submitted_ts=submitted_ts,
                    ),
                )
            else:
                self.put(
                    event_type,
                    _AsyncOrderResult(
                        order_request,
                        operation_id=operation_id,
                        event_type=event_type,
                        payload=result,
                        submitted_ts=submitted_ts,
                    ),
                )

        self._pending_operations += 1
        self._pending_async_requests[operation_id] = (event_type, order_request, submitted_ts)
        self._submitted_operations += 1
        self._peak_pending_operations = max(self._peak_pending_operations, self._pending_operations)
        try:
            future = asyncio.run_coroutine_threadsafe(runner(), self._async_loop)
        except Exception:
            self._pending_operations -= 1
            del self._pending_async_requests[operation_id]
            self._submitted_operations -= 1
            raise
        self._async_futures[operation_id] = future
        return True

    def _start_reconciliation(self, stale_after_ts: float, batch_size: int) -> None:
        if self._stopping or self._reconciliation_pending or not self.exchange_orders:
            return
        now = time.time()
        stale_orders = [
            tracker
            for tracker in self.exchange_orders.values()
            if now - tracker.last_recon_ts >= stale_after_ts
            and tracker.state not in (OrderState.CANCELLED, OrderState.FILLED)
        ]
        stale_orders.sort(key=lambda tracker: tracker.last_recon_ts)
        trackers = stale_orders[:batch_size]
        if not trackers:
            return

        async def reconcile() -> list[tuple[str, ExchangeExecution | ExchangeOrder]]:
            events: list[tuple[str, ExchangeExecution | ExchangeOrder]] = []
            for tracker in trackers:
                symbol = tracker.order_request.market.symbol
                exchange_order = await self._order_api.fetch_order(tracker.order_id, symbol)
                if not exchange_order or exchange_order.state == OrderState.CREATED:
                    continue
                if exchange_order.state == OrderState.CANCELLED:
                    events.append((EventType.EXCHANGE_ORDER, exchange_order))
                    continue
                executions = await self._order_api.fetch_executions(tracker.order_id, symbol)
                events.extend((EventType.EXCHANGE_EXECUTION, execution) for execution in executions)
            return events

        if self._submit_operation(None, EVENT_ASYNC_RECONCILIATION_RESULT, reconcile):
            for tracker in trackers:
                tracker.last_recon_ts = now
            self._reconciliation_pending = True
            self._reconciliation_checked_orders = len(trackers)

    def _reconciliation_result(self, result: _AsyncOrderResult) -> None:
        if result.operation_id not in self._pending_async_requests:
            return
        try:
            if isinstance(result.error, FatalException):
                raise result.error
            if result.error:
                LOG.warning("Async order reconciliation failed", exc_info=result.error)
                self.publish_status(EventType.EXCEPTION, result.error)
                self._record_reconciliation(self._reconciliation_checked_orders, 0)
                return
            for event_type, event in result.payload:
                if event_type == EventType.EXCHANGE_EXECUTION:
                    self.handle_exchange_execution(event)
                else:
                    self.handle_exchange_order(event)
            self._record_reconciliation(self._reconciliation_checked_orders, len(result.payload))
        finally:
            self._reconciliation_pending = False
            self._reconciliation_checked_orders = 0
            self._operation_finished(result)

    def handle_create_order(self, order_request: OrderRequest):
        if self._stopping:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))
            return
        self._pending_creates.add(order_request.ctx_id)
        if not self._submit_async(order_request, EVENT_ASYNC_CREATE_RESULT, self._order_api.create_order):
            self._pending_creates.remove(order_request.ctx_id)
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))

    def _create_result(self, result: _AsyncOrderResult) -> None:
        if result.operation_id not in self._pending_async_requests:
            return
        try:
            self._handle_async_create_result(result)
        finally:
            self._pending_creates.remove(result.order_request.ctx_id)
            if not self._pending_creates:
                self._early_events.clear()
            self._operation_finished(result)

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
                self._early_event_count += 1
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
                self._early_event_count += 1
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
        if not self._submit_async(order_request, EVENT_ASYNC_CANCEL_RESULT, self._order_api.cancel_order):
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CANCEL))

    def _cancel_result(self, result: _AsyncOrderResult) -> None:
        if result.operation_id not in self._pending_async_requests:
            return
        try:
            self._handle_async_cancel_result(result)
        finally:
            self._operation_finished(result)

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

    def _operation_finished(self, result: _AsyncOrderResult | None = None):
        if result is not None:
            self._pending_async_requests.pop(result.operation_id, None)
            self._async_futures.pop(result.operation_id, None)
            latency_ts = time.monotonic() - result.submitted_ts
            self._completed_operations += 1
            self._total_latency_ts += latency_ts
            self._max_latency_ts = max(self._max_latency_ts, latency_ts)
            failed = result.error is not None
            if result.event_type == EVENT_ASYNC_CREATE_RESULT and result.payload is None:
                failed = True
            elif result.event_type == EVENT_ASYNC_CANCEL_RESULT and not result.payload:
                failed = True
            if failed:
                self._failed_operations += 1
        self._pending_operations -= 1
        if self._stopping and not self._pending_operations:
            super().stop()

    def _handle_stop(self, _):
        self._stopping = True
        self._shutdown_deadline = time.monotonic() + self._shutdown_timeout_ts
        if not self._pending_operations:
            super().stop()

    def heartbeat(self):
        super().heartbeat()
        if (
            self._stopping
            and self._pending_operations
            and self._shutdown_deadline is not None
            and time.monotonic() >= self._shutdown_deadline
        ):
            self._timeout_pending_operations()
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
            deadline = time.monotonic() + self._shutdown_timeout_ts
            original_heartbeat = self.heartbeat_interval
            self.heartbeat_interval = 0.1
            while self._pending_operations and time.monotonic() < deadline:
                self.tick()
            if self._pending_operations:
                self._timeout_pending_operations()
                drain_deadline = time.monotonic() + 1.0
                while self._pending_operations and time.monotonic() < drain_deadline:
                    self.tick()
            self.heartbeat_interval = original_heartbeat
        finally:
            LOG.info("Async gateway final stats %s", self.stats())
            self._shutdown_async()

    def _timeout_pending_operations(self) -> None:
        timed_out = []
        for operation_id, future in tuple(self._async_futures.items()):
            if future.cancel():
                timed_out.append((operation_id, self._pending_async_requests[operation_id]))
        if not timed_out:
            return
        error = TimeoutError(f"Async gateway shutdown timed out with {len(timed_out)} operations pending")
        LOG.error("%s", error)
        self.publish_status(EventType.EXCEPTION, error)
        now = time.monotonic()
        for operation_id, (event_type, order_request, submitted_ts) in timed_out:
            if order_request is not None:
                order_error = OrderError.CREATE if event_type == EVENT_ASYNC_CREATE_RESULT else OrderError.CANCEL
                self.publish_order_error(OrderResponse(order_request, error=order_error))
            latency_ts = now - submitted_ts
            self._total_latency_ts += latency_ts
            self._max_latency_ts = max(self._max_latency_ts, latency_ts)
            self._pending_async_requests.pop(operation_id, None)
            self._async_futures.pop(operation_id, None)
        timed_out_count = len(timed_out)
        self._completed_operations += timed_out_count
        self._failed_operations += timed_out_count
        self._pending_operations -= timed_out_count
        timed_out_creates = {
            order_request.ctx_id
            for _, (event_type, order_request, _) in timed_out
            if event_type == EVENT_ASYNC_CREATE_RESULT and order_request is not None
        }
        self._pending_creates.difference_update(timed_out_creates)
        if not self._pending_creates:
            self._early_events.clear()
        if any(event_type == EVENT_ASYNC_RECONCILIATION_RESULT for _, (event_type, _, _) in timed_out):
            self._record_reconciliation(self._reconciliation_checked_orders, 0)
            self._reconciliation_pending = False
            self._reconciliation_checked_orders = 0

    def _record_reconciliation(self, checked_orders: int, terminal_events: int) -> None:
        self._reconciliation_runs += 1
        self._reconciled_orders += checked_orders
        self._reconciliation_events += terminal_events

    def stats(self) -> AsyncGatewayStats:
        average_latency_ts = self._total_latency_ts / self._completed_operations if self._completed_operations else 0.0
        return AsyncGatewayStats(
            pending_operations=self._pending_operations,
            peak_pending_operations=self._peak_pending_operations,
            submitted_operations=self._submitted_operations,
            completed_operations=self._completed_operations,
            failed_operations=self._failed_operations,
            rejected_operations=self._rejected_operations,
            early_events=self._early_event_count,
            reconciliation_runs=self._reconciliation_runs,
            reconciled_orders=self._reconciled_orders,
            reconciliation_events=self._reconciliation_events,
            average_latency_ms=average_latency_ts * 1_000,
            max_latency_ms=self._max_latency_ts * 1_000,
        )

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


@dataclass(frozen=True)
class AsyncReconOrdersCallable:
    stale_after_ts: float = 10.0
    batch_size: int = 5

    def __call__(self, caller: DefaultGatewayAsync) -> None:
        caller._start_reconciliation(self.stale_after_ts, self.batch_size)
