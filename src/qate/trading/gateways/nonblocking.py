import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from qate.core.ev_type import EventType
from qate.core.model import FatalException, Field, Measurement, Metric, Side, Tag
from qate.core.order import (
    ExchangeExecution,
    ExchangeOrder,
    OrderError,
    OrderMetrics,
    OrderRequest,
    OrderResponse,
    OrderState,
    OrderTracker,
)
from qate.core.order_api import OrderApi

from .tracking import OrderTrackingGateway

LOG = getLogger(__name__)

EVENT_ASYNC_CREATE_RESULT = "EVENT_ASYNC_CREATE_ORDER_RESULT"
EVENT_ASYNC_CANCEL_RESULT = "EVENT_ASYNC_CANCEL_ORDER_RESULT"
EVENT_ASYNC_RECONCILIATION_RESULT = "EVENT_ASYNC_RECONCILIATION_RESULT"
_EVENT_SUBMITTED = "EVENT_ASYNC_OPERATION_SUBMITTED"
_EVENT_REJECTED = "EVENT_ASYNC_OPERATION_REJECTED"
_EVENT_STOP = "EVENT_ASYNC_GATEWAY_STOP"
PERF_GATEWAY = "PERF_GATEWAY"

_PERF_ORDER = Measurement.PERF_ORDER._value_
_TAG_EXCHANGE_NAME = Tag.EXCHANGE_NAME._value_
_TAG_SYMBOL = Tag.SYMBOL._value_
_TAG_SIDE = Tag.SIDE._value_
_TAG_SETTLE_TYPE = Tag.SETTLE_TYPE._value_
_TAG_ORDER_TYPE = Tag.ORDER_TYPE._value_
_TAG_PERF_OUTCOME = Tag.PERF_OUTCOME._value_
_FIELD_PERF_BOOK_AGE = Field.PERF_BOOK_AGE._value_
_FIELD_PERF_QUEUE_TIME = Field.PERF_QUEUE_TIME._value_
_FIELD_PERF_CREATE_RTT = Field.PERF_CREATE_RTT._value_
_FIELD_PERF_ACK_TIME = Field.PERF_ACK_TIME._value_
_FIELD_PERF_FILL_TIME = Field.PERF_FILL_TIME._value_
_FIELD_PERF_CANCEL_RTT = Field.PERF_CANCEL_RTT._value_
_FIELD_PERF_CANCEL_TIME = Field.PERF_CANCEL_TIME._value_
_FIELD_PERF_SLIPPAGE = Field.PERF_SLIPPAGE._value_
_FIELD_PERF_SLIPPAGE_BPS = Field.PERF_SLIPPAGE_BPS._value_


@dataclass
class _AsyncOrderResult:
    """One operation, from submission to outcome.

    Created on the submitting thread, completed on the asyncio thread, then put on
    the gateway queue; the queue hand-off is what makes the gateway thread's reads safe.
    """

    order_request: OrderRequest | None
    operation_id: int = -1
    event_type: str = ""
    payload: Any = None
    error: BaseException | None = None
    submitted_ts: float = 0.0  # monotonic, for gateway latency stats
    submit_wall_ts: float = 0.0  # time.time() when the caller handed it over
    sent_ts: float = 0.0  # time.time() when the request started, on the asyncio thread
    done_ts: float = 0.0  # time.time() when the response arrived
    future: concurrent.futures.Future | None = None


@dataclass
class _OrderPerf:
    """What it cost, in time, to get one order to the exchange and back.

    `PERF_ORDER` reports these once per order, at its terminal state.
    """

    decision_ts: float
    submit_ts: float
    sent_ts: float = 0.0
    acked_ts: float = 0.0
    created_ts: float = 0.0
    cancel_submit_ts: float = 0.0
    cancel_sent_ts: float = 0.0
    cancel_acked_ts: float = 0.0


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


class DefaultGatewayAsync(OrderTrackingGateway):
    """Async create/cancel, with all order state owned by the gateway thread.

    `create` and `cancel` hand the request straight to the asyncio thread from the
    caller's thread. Routing it through the gateway thread first added a thread
    wake-up to the one path where time is money: under load each wake-up waits for
    the GIL, and a market order's price is decided when it reaches the exchange.
    Bookkeeping still happens on the gateway thread: a `_EVENT_SUBMITTED` is queued
    before the request is scheduled, so it precedes both the result and any private
    feed event about the order, which is what lets an early execution be buffered.

    Each order's timeline is published once, at its terminal state, as an
    `ORDER_METRICS` status event holding a `PERF_ORDER` metric; see
    `_publish_order_perf`.

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
        super().__init__(api, heartbeat_interval_ts)
        self._order_api = order_api
        self._max_pending_operations = max_pending_operations
        self._shutdown_timeout_ts = shutdown_timeout_ts
        self._pending_operations = 0
        # Admission happens on the caller's thread, so capacity is counted under a
        # lock there; `_pending_operations` is the gateway thread's view of the same.
        self._admission_lock = threading.Lock()
        self._admitted_operations = 0
        self._order_perf: dict[int, _OrderPerf] = {}
        self._pending_creates: set[int] = set()
        self._early_events: dict[str, list[tuple[str, Any]]] = {}
        self._execution_ids: dict[str, set[str]] = {}
        self._stop_requested = threading.Event()
        self._stopping = False
        self._shutdown_deadline: float | None = None
        self._is_async_shutdown = False
        self._async_loop = asyncio.new_event_loop()
        self._async_semaphore = asyncio.Semaphore(max_concurrent_operations)
        self._async_futures: dict[int, _AsyncOrderResult] = {}
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
        self.register(_EVENT_SUBMITTED, self._handle_submitted)
        self.register(_EVENT_REJECTED, self._handle_rejected)
        self.register(_EVENT_STOP, self._handle_stop)
        self.repeat(5, AsyncReconOrdersCallable())
        self._async_thread.start()

    def _run_async_loop(self) -> None:
        asyncio.set_event_loop(self._async_loop)
        self._async_loop.run_forever()

    def create(self, order_request: OrderRequest) -> None:
        if self._stop_requested.is_set():
            raise RuntimeError("Gateway is stopping")
        self._submit_order(order_request, EVENT_ASYNC_CREATE_RESULT, self._order_api.create_order)

    def cancel(self, order_request: OrderRequest) -> None:
        if self._stop_requested.is_set():
            raise RuntimeError("Gateway is stopping")
        # Sent without checking the tracker, which the gateway thread owns. If a
        # fill or cancel already won the race, the venue refuses or no-ops and
        # `_handle_async_cancel_result` drops the answer for an untracked order.
        self._submit_order(order_request, EVENT_ASYNC_CANCEL_RESULT, self._order_api.cancel_order)

    def handle_create_order(self, order_request: OrderRequest):
        """For a caller that queues `EVENT_CREATE_ORDER` rather than calling `create`."""
        self._submit_order(order_request, EVENT_ASYNC_CREATE_RESULT, self._order_api.create_order)

    def handle_cancel_order(self, order_request: OrderRequest):
        """For a caller that queues `EVENT_CANCEL_ORDER` rather than calling `cancel`."""
        self._submit_order(order_request, EVENT_ASYNC_CANCEL_RESULT, self._order_api.cancel_order)

    def _submit_order(
        self,
        order_request: OrderRequest,
        event_type: str,
        operation: Callable[[OrderRequest], Awaitable[Any]],
    ) -> None:
        if not self._submit_operation(order_request, event_type, lambda: operation(order_request)):
            self.put(_EVENT_REJECTED, (event_type, order_request))

    def _submit_operation(
        self,
        order_request: OrderRequest | None,
        event_type: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> bool:
        """Schedule an operation on the asyncio thread. Safe from any thread."""
        with self._admission_lock:
            if self._admitted_operations >= self._max_pending_operations:
                admitted = False
            else:
                admitted = True
                self._admitted_operations += 1
                operation_id = self._next_operation_id
                self._next_operation_id += 1
        if not admitted:
            LOG.error(
                "Async gateway capacity exhausted limit=%s operation=%s ctx_id=%s",
                self._max_pending_operations,
                event_type,
                order_request.ctx_id if order_request is not None else None,
            )
            return False

        result = _AsyncOrderResult(
            order_request,
            operation_id=operation_id,
            event_type=event_type,
            submitted_ts=time.monotonic(),
            submit_wall_ts=time.time(),
        )

        async def runner() -> None:
            try:
                async with self._async_semaphore:
                    result.sent_ts = time.time()
                    result.payload = await operation()
            except (Exception, asyncio.CancelledError) as exc:
                result.error = exc
            result.done_ts = time.time()
            self.put(event_type, result)

        # Bookkeeping first: it must precede the result and any feed event about the order.
        self.put(_EVENT_SUBMITTED, result)
        try:
            result.future = asyncio.run_coroutine_threadsafe(runner(), self._async_loop)
        except RuntimeError as exc:  # the loop is already closed: we are shutting down
            result.error = exc
            self.put(event_type, result)
            raise
        return True

    def _handle_submitted(self, result: _AsyncOrderResult) -> None:
        self._pending_operations += 1
        self._pending_async_requests[result.operation_id] = (
            result.event_type,
            result.order_request,
            result.submitted_ts,
        )
        self._async_futures[result.operation_id] = result
        self._submitted_operations += 1
        self._peak_pending_operations = max(self._peak_pending_operations, self._pending_operations)
        order_request = result.order_request
        if result.event_type == EVENT_ASYNC_CREATE_RESULT:
            self._pending_creates.add(order_request.ctx_id)
            self._order_perf[order_request.ctx_id] = _OrderPerf(order_request.ts, result.submit_wall_ts)
        elif result.event_type == EVENT_ASYNC_CANCEL_RESULT:
            perf = self._order_perf.get(order_request.ctx_id)
            if perf is not None:
                perf.cancel_submit_ts = result.submit_wall_ts

    def _handle_rejected(self, rejected: tuple[str, OrderRequest]) -> None:
        event_type, order_request = rejected
        self._rejected_operations += 1
        error = OrderError.CREATE if event_type == EVENT_ASYNC_CREATE_RESULT else OrderError.CANCEL
        self.publish_order_error(OrderResponse(order_request, error=error))

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
        else:
            self._rejected_operations += 1

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

    def _create_result(self, result: _AsyncOrderResult) -> None:
        if result.operation_id not in self._pending_async_requests:
            return
        perf = self._order_perf.get(result.order_request.ctx_id)
        if perf is not None:
            perf.sent_ts, perf.acked_ts = result.sent_ts, result.done_ts
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
        perf = self._order_perf.get(order_request.ctx_id)
        if perf is not None:
            perf.created_ts = time.time()
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

    def _cancel_result(self, result: _AsyncOrderResult) -> None:
        if result.operation_id not in self._pending_async_requests:
            return
        perf = self._order_perf.get(result.order_request.ctx_id)
        if perf is not None:
            perf.cancel_sent_ts, perf.cancel_acked_ts = result.sent_ts, result.done_ts
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
        with self._admission_lock:
            self._admitted_operations -= 1
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
            LOG.info("%s %s", PERF_GATEWAY, self.stats())
            self._shutdown_async()

    def _timeout_pending_operations(self) -> None:
        timed_out = []
        for operation_id, pending in tuple(self._async_futures.items()):
            # `future` is None only for the instant between queueing and scheduling.
            if pending.future is not None and pending.future.cancel():
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
        with self._admission_lock:
            self._admitted_operations -= timed_out_count
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

    # The response goes out first: the strategy waits on it, and the metric can wait.

    def publish_order_filled(self, order_response: OrderResponse):
        super().publish_order_filled(order_response)
        self._publish_order_perf(order_response, "FILLED")

    def publish_order_cancelled(self, order_response: OrderResponse):
        super().publish_order_cancelled(order_response)
        self._publish_order_perf(order_response, "CANCELLED")

    def publish_order_error(self, order_response: OrderResponse):
        super().publish_order_error(order_response)
        # A failed cancel leaves the order alive; only a failed create ends it.
        if order_response.error == OrderError.CREATE:
            self._publish_order_perf(order_response, "CREATE_ERROR")

    def _publish_order_perf(self, order_response: OrderResponse, outcome: str) -> None:
        """One `PERF_ORDER` metric per order, published on the status feed as `ORDER_METRICS`.

        Tagged like an `ORDER` record, plus `PERF_OUTCOME` (FILLED, CANCELLED or
        CREATE_ERROR). Durations are seconds; a field is absent when it does not
        apply, e.g. no cancel was sent. Slippage positive is worse.

        PERF_BOOK_AGE      submit - order_request.ts. Strategies stamp `ts` with the
                           triggering event's exchange timestamp, so this is how old the
                           market data was when the order left the strategy: venue -> host
                           transport, queueing, and the decision itself. Includes clock
                           offset against the venue.
        PERF_QUEUE_TIME    submit -> request started on the asyncio thread
        PERF_CREATE_RTT    create request round trip
        PERF_ACK_TIME      submit -> gateway published ORDER_CREATED
        PERF_FILL_TIME     submit -> gateway saw the order complete
        PERF_CANCEL_RTT    cancel request round trip
        PERF_CANCEL_TIME   cancel submit -> confirmed cancelled: how long a maker order
                           could still be filled after the strategy gave up on it
        PERF_SLIPPAGE      exec price vs. `order_request.price`, in price units per unit
        PERF_SLIPPAGE_BPS  the same, in basis points of the requested price
        """
        order_request = order_response.order_request
        perf = self._order_perf.pop(order_request.ctx_id, None)
        if perf is None:
            return
        now = time.time()
        fields = [_FIELD_PERF_BOOK_AGE, perf.submit_ts - perf.decision_ts]
        _add_duration(fields, _FIELD_PERF_QUEUE_TIME, perf.submit_ts, perf.sent_ts)
        _add_duration(fields, _FIELD_PERF_CREATE_RTT, perf.sent_ts, perf.acked_ts)
        _add_duration(fields, _FIELD_PERF_ACK_TIME, perf.submit_ts, perf.created_ts)
        _add_duration(fields, _FIELD_PERF_CANCEL_RTT, perf.cancel_sent_ts, perf.cancel_acked_ts)
        if outcome == "FILLED":
            _add_duration(fields, _FIELD_PERF_FILL_TIME, perf.submit_ts, now)
            if order_request.price:
                slippage = order_response.exec_price - order_request.price
                if order_request.side == Side.SELL:
                    slippage = -slippage
                fields += [
                    _FIELD_PERF_SLIPPAGE,
                    slippage,
                    _FIELD_PERF_SLIPPAGE_BPS,
                    slippage / order_request.price * 10_000,
                ]
        elif outcome == "CANCELLED":
            _add_duration(fields, _FIELD_PERF_CANCEL_TIME, perf.cancel_submit_ts, now)
        market = order_request.market
        tags = [
            _TAG_EXCHANGE_NAME,
            market.exchange_name._value_,
            _TAG_SYMBOL,
            market.symbol._value_,
            _TAG_SIDE,
            order_request.side._value_,
            _TAG_SETTLE_TYPE,
            order_request.settle_type._value_,
            _TAG_ORDER_TYPE,
            order_request.order_type._value_,
            _TAG_PERF_OUTCOME,
            outcome,
        ]
        metric = Metric(_PERF_ORDER, now, tags, fields)
        self.publish_status(EventType.ORDER_METRICS, OrderMetrics(order_request, metric))

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


def _add_duration(fields: list, field: int, start: float, end: float) -> None:
    if start and end:
        fields += [field, end - start]


@dataclass(frozen=True)
class AsyncReconOrdersCallable:
    stale_after_ts: float = 10.0
    batch_size: int = 5

    def __call__(self, caller: DefaultGatewayAsync) -> None:
        caller._start_reconciliation(self.stale_after_ts, self.batch_size)
