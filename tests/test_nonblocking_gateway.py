"""Exercise async gateway races with in-memory APIs, never an exchange."""

import asyncio
import queue
import threading
import time

import pytest

from qate.core.api import Api
from qate.core.ev_type import EventType
from qate.core.feed import ExchangeFeed
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import ExchangeExecution, ExchangeOrder, OrderError, OrderRequest, OrderState
from qate.core.order_api import OrderApi
from qate.core.symbol import Symbol
from qate.trading.exceptions import StopTradingException
from qate.trading.gateways.default import ReconOrdersCallable
from qate.trading.gateways.nonblocking import DefaultGatewayAsync

MARKET = Market(ExchangeName.GMO, Symbol.BTC_JPY)


class FakeApi(Api):
    exchange_name = ExchangeName.GMO

    def __init__(self):
        self.snapshot = None

    def fetch_order(self, order_id, symbol):
        return self.snapshot

    def fetch_executions(self, order_id, symbol):
        return [ExchangeExecution('reconciled', order_id, self.snapshot.exec_price,
                                  self.snapshot.exec_size, self.snapshot.exchange_ts)]


class FakeOrderApi(OrderApi):
    def __init__(self):
        self.create_started = threading.Event()
        self.release_create = threading.Event()
        self.release_create.set()
        self.cancel_started = threading.Event()
        self.release_cancel = threading.Event()
        self.release_cancel.set()
        self.create_error = None
        self.cancel_error = None
        self.cancel_result = True
        self.created = []
        self.closed = threading.Event()

    async def create_order(self, order_request):
        self.create_started.set()
        while not self.release_create.is_set():
            await asyncio.sleep(0.001)
        if self.create_error:
            raise self.create_error
        order_id = str(order_request.ctx_id)
        self.created.append(order_id)
        return order_id

    async def cancel_order(self, order_request):
        self.cancel_started.set()
        while not self.release_cancel.is_set():
            await asyncio.sleep(0.001)
        if self.cancel_error:
            raise self.cancel_error
        return self.cancel_result

    async def fetch_executions(self, order_id, symbol=None):
        return []

    async def aclose(self):
        self.closed.set()


def request():
    return OrderRequest(time.time(), MARKET, Side.BUY, 100, 1, SettleType.OPEN)


def execution(order, size=1, trade_id='fill'):
    return ExchangeExecution(trade_id, str(order.ctx_id), 100, size, time.time())


def snapshot(order, state):
    size = 1 if state == OrderState.FILLED else 0
    return ExchangeOrder(str(order.ctx_id), size, 100, state, time.time())


class ObservedGateway(DefaultGatewayAsync):
    """Expose a barrier after a cancel result has been handled, for race tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_processed = threading.Event()

    def _handle_async_cancel_result(self, result):
        try:
            super()._handle_async_cancel_result(result)
        finally:
            self.cancel_processed.set()


@pytest.fixture
def setup_gateway():
    sync, async_api = FakeApi(), FakeOrderApi()
    gateway = ObservedGateway(sync, async_api, heartbeat_interval_ts=0.1)
    orders, statuses = queue.Queue(), queue.Queue()
    gateway.add_order_listener(orders)
    gateway.add_status_listener(statuses)
    feed = ExchangeFeed()
    gateway.subscribe_exchange_feed(feed)
    gateway.register('TEST_BARRIER', lambda event: event.set())
    gateway.register('TEST_RECONCILE', lambda _: ReconOrdersCallable(stale_after_ts=0)(gateway))
    gateway.start()
    yield gateway, sync, async_api, feed, orders, statuses
    async_api.release_create.set()
    async_api.release_cancel.set()
    gateway.stop()
    gateway.join(timeout=3)
    assert not gateway.is_alive()
    assert not gateway._async_thread.is_alive()
    assert async_api.closed.is_set()


def settle(gateway):
    """Wait until the gateway has finished handling all preceding events."""
    barrier = threading.Event()
    gateway.put('TEST_BARRIER', barrier)
    assert barrier.wait(2)


def created(setup):
    gateway, _, _, _, orders, _ = setup
    order = request()
    gateway.create(order)
    event, response = orders.get(timeout=2)
    assert event == EventType.ORDER_CREATED
    order.order_id = response.order_id
    return order


def test_create_and_fill(setup_gateway):
    gateway, _, _, feed, orders, _ = setup_gateway
    order = created(setup_gateway)
    feed.publish_execution(execution(order))
    kind, fill = orders.get(timeout=2)
    assert kind == EventType.ORDER_FILLED
    assert fill.exec_size == 1
    settle(gateway)
    assert order.order_id not in gateway.exchange_orders


@pytest.mark.parametrize('early_kind', ['execution', 'order'])
def test_private_event_before_create_response_is_replayed(setup_gateway, early_kind):
    gateway, _, api, feed, orders, _ = setup_gateway
    api.release_create.clear()
    order = request()
    gateway.create(order)
    assert api.create_started.wait(2)
    if early_kind == 'execution':
        feed.publish_execution(execution(order))
    else:
        feed.publish_order(snapshot(order, OrderState.FILLED))
    # The queued feed event precedes the create result even if the worker has
    # not yet consumed it. No timing-based delay is needed to force this race.
    api.release_create.set()
    assert orders.get(timeout=2)[0] == EventType.ORDER_CREATED
    kind, fill = orders.get(timeout=2)
    assert kind == EventType.ORDER_FILLED
    assert fill.exec_size == 1
    settle(gateway)
    assert str(order.ctx_id) not in gateway.exchange_orders


@pytest.mark.parametrize('failure', [False, RuntimeError('cancel failed')])
def test_cancel_failure_reports_order_error_and_keeps_tracker(setup_gateway, failure):
    gateway, _, api, _, orders, _ = setup_gateway
    order = created(setup_gateway)
    if isinstance(failure, Exception):
        api.cancel_error = failure
    else:
        api.cancel_result = failure
    gateway.cancel(order)
    kind, response = orders.get(timeout=2)
    assert kind == EventType.ORDER_ERROR
    assert response.error == OrderError.CANCEL
    assert order.order_id in gateway.exchange_orders


def test_cancel_acceptance_keeps_tracking_delayed_fill(setup_gateway):
    gateway, sync, _, feed, orders, _ = setup_gateway
    order = created(setup_gateway)
    sync.snapshot = snapshot(order, OrderState.CREATED)
    gateway.cancel(order)
    assert gateway.cancel_processed.wait(2)
    feed.publish_execution(execution(order))
    kind, fill = orders.get(timeout=2)
    assert kind == EventType.ORDER_FILLED
    assert fill.exec_size == 1
    settle(gateway)
    assert orders.empty()  # No fabricated cancellation was emitted.
    assert order.order_id not in gateway.exchange_orders


@pytest.mark.parametrize('state', [OrderState.CANCELLED, OrderState.FILLED])
def test_cancel_uses_confirmed_terminal_state(setup_gateway, state):
    gateway, sync, _, _, orders, _ = setup_gateway
    order = created(setup_gateway)
    sync.snapshot = snapshot(order, state)
    gateway.cancel(order)
    assert gateway.cancel_processed.wait(2)
    gateway.put('TEST_RECONCILE')
    kind, response = orders.get(timeout=2)
    assert kind == (EventType.ORDER_FILLED if state == OrderState.FILLED else EventType.ORDER_CANCELLED)
    assert response.state == state
    settle(gateway)
    assert order.order_id not in gateway.exchange_orders


def test_fill_before_cancel_failure_wins_without_spurious_error(setup_gateway):
    gateway, _, api, feed, orders, _ = setup_gateway
    order = created(setup_gateway)
    api.release_cancel.clear()
    api.cancel_error = RuntimeError('already filled')
    gateway.cancel(order)
    assert api.cancel_started.wait(2)
    feed.publish_execution(execution(order))
    assert orders.get(timeout=2)[0] == EventType.ORDER_FILLED
    api.release_cancel.set()
    gateway.stop()
    gateway.join(timeout=2)
    assert orders.empty()


@pytest.mark.parametrize('operation', ['create', 'cancel'])
def test_fatal_errors_stop_gateway(setup_gateway, operation):
    gateway, _, api, _, orders, statuses = setup_gateway
    fatal = StopTradingException('stop now')
    if operation == 'create':
        api.create_error = fatal
        gateway.create(request())
    else:
        order = created(setup_gateway)
        api.cancel_error = fatal
        gateway.cancel(order)
    gateway.join(timeout=2)
    assert not gateway.is_alive()
    assert statuses.get(timeout=2) == (EventType.EXCEPTION, fatal)
    assert statuses.get(timeout=2) == (EventType.EV_LOOP_EXIT, gateway)
    assert orders.empty()


def test_shutdown_drains_inflight_create_before_closing_api(setup_gateway):
    gateway, _, api, _, orders, _ = setup_gateway
    api.release_create.clear()
    order = request()
    gateway.create(order)
    assert api.create_started.wait(2)
    gateway.stop()
    assert not api.closed.is_set()
    api.release_create.set()
    gateway.join(timeout=2)
    assert not gateway.is_alive()
    assert api.closed.is_set()
    assert orders.get(timeout=2)[0] == EventType.ORDER_CREATED
    assert api.created == [str(order.ctx_id)]
    assert str(order.ctx_id) in gateway.exchange_orders
    with pytest.raises(RuntimeError, match='stopping'):
        gateway.create(request())


def test_duplicate_execution_does_not_overcount_partial_fill(setup_gateway):
    gateway, _, _, feed, orders, _ = setup_gateway
    order = created(setup_gateway)
    partial = execution(order, 0.5, 'first')
    feed.publish_execution(partial)
    feed.publish_execution(partial)  # e.g. reconciliation repeats a feed event
    feed.publish_execution(execution(order, 0.5, 'second'))
    kind, fill = orders.get(timeout=2)
    assert kind == EventType.ORDER_FILLED
    assert fill.exec_size == 1
    assert orders.empty()


def test_no_status_after_cancel_ack_waits_for_private_confirmation(setup_gateway):
    gateway, sync, _, feed, orders, _ = setup_gateway
    order = created(setup_gateway)
    gateway.cancel(order)
    assert gateway.cancel_processed.wait(2)
    feed.publish_order(snapshot(order, OrderState.CANCELLED))
    assert orders.get(timeout=2)[0] == EventType.ORDER_CANCELLED
    settle(gateway)
    assert order.order_id not in gateway.exchange_orders


@pytest.mark.parametrize('failure', [None, RuntimeError('create failed')])
def test_create_failure_reports_order_error(setup_gateway, failure):
    gateway, _, api, _, orders, _ = setup_gateway
    api.create_error = failure
    if failure is None:
        async def refuse(order):
            return None
        api.create_order = refuse
    gateway.create(request())
    kind, response = orders.get(timeout=2)
    assert kind == EventType.ORDER_ERROR
    assert response.error == OrderError.CREATE
    assert not gateway.exchange_orders


def test_shutdown_drains_inflight_cancel(setup_gateway):
    gateway, sync, api, _, orders, _ = setup_gateway
    order = created(setup_gateway)
    api.release_cancel.clear()
    api.cancel_result = False
    gateway.cancel(order)
    assert api.cancel_started.wait(2)
    gateway.stop()
    api.release_cancel.set()
    gateway.join(timeout=2)
    assert not gateway.is_alive()
    kind, response = orders.get(timeout=2)
    assert kind == EventType.ORDER_ERROR
    assert response.error == OrderError.CANCEL
    assert order.order_id in gateway.exchange_orders


def test_close_failure_still_reports_gateway_exit(setup_gateway):
    gateway, _, api, _, _, statuses = setup_gateway
    error = RuntimeError('close failed')

    async def broken_close():
        api.closed.set()
        raise error

    api.aclose = broken_close
    gateway.stop()
    gateway.join(timeout=2)
    assert not gateway.is_alive()
    assert statuses.get(timeout=2) == (EventType.EXCEPTION, error)
    assert statuses.get(timeout=2) == (EventType.EV_LOOP_EXIT, gateway)


def test_fatal_error_still_drains_other_submitted_creates(setup_gateway):
    gateway, _, api, _, orders, statuses = setup_gateway
    first, second = request(), request()
    api.release_create.clear()
    original_create = api.create_order
    fatal = StopTradingException('second create is fatal')

    async def create(order):
        if order is second:
            raise fatal
        return await original_create(order)

    api.create_order = create
    gateway.create(first)
    assert api.create_started.wait(2)
    gateway.create(second)
    assert statuses.get(timeout=2) == (EventType.EXCEPTION, fatal)
    assert not api.closed.is_set()
    api.release_create.set()
    gateway.join(timeout=2)
    assert not gateway.is_alive()
    kind, response = orders.get(timeout=2)
    assert kind == EventType.ORDER_CREATED
    assert response.order_request is first
    assert str(first.ctx_id) in gateway.exchange_orders
    assert api.closed.is_set()


def test_stop_before_start_closes_async_resources():
    api = FakeOrderApi()
    gateway = DefaultGatewayAsync(FakeApi(), api)
    gateway.stop()
    gateway.stop()  # Closing an unstarted gateway is idempotent too.
    assert api.closed.is_set()
    assert not gateway._async_thread.is_alive()
