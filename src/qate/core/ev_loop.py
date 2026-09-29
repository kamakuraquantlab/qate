import queue
import threading
import time
from dataclasses import dataclass, field
from itertools import count
from logging import getLogger
from typing import Callable

from .ev_q import EventQueue
from .ev_type import EventType
from .feed import StatusFeed
from .model import EventLoopExit, FatalException, TimeSeriesData

LOG = getLogger(__name__)


@dataclass
class Task(TimeSeriesData):
    callable: Callable
    delay_ts: float
    interval_ts: float = -1.0
    caller: object = None
    id: int = field(default_factory=count().__next__)
    created_ts: float = field(default_factory=time.time)

    def get_ts(self) -> float:
        return self.created_ts

    def renew(self, now_ts: float):
        self.created_ts = now_ts

    @property
    def scheduled_ts(self) -> float:
        return self.created_ts + self.delay_ts


def create_event_queue() -> EventQueue:
    return queue.Queue()


EVENT_ADD_TASK = "EVENT_ADD_TASK"


class EventLoop(StatusFeed, threading.Thread):
    def __init__(self, event_queue: EventQueue = None, heartbeat_interval: float | None = None):
        super(EventLoop, self).__init__()
        self.event_queue = create_event_queue() if not event_queue else event_queue
        self.handlers: dict[str, list] = {}
        if heartbeat_interval is not None and heartbeat_interval < 0.1:
            raise Exception(f"Unexpected value of heartbeat_interval {heartbeat_interval}")
        self.heartbeat_interval: float | None = heartbeat_interval
        self.tasks: dict[int, Task] = {}
        self.register(EVENT_ADD_TASK, self.handle_add_task)

    @property
    def tick_ts(self) -> float:
        return time.time()

    def stop(self):
        self.event_queue.put(None)

    def before_loop(self):
        pass

    def after_loop(self):
        pass

    def put(self, event_type, event=None):
        self.event_queue.put((event_type, event))

    def register(self, event_type, handler):
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)

    def _next(self):
        try:
            item = self.event_queue.get(timeout=self.heartbeat_interval)
            if not item:
                raise EventLoopExit()
            (event_type, event) = item
            if event_type in self.handlers:
                for handler in self.handlers[event_type]:
                    handler(event)
        except queue.Empty:
            pass

    def tick(self) -> bool:
        try:
            self._next()
            self.heartbeat()
        except EventLoopExit:
            LOG.info(f"{self.__class__.__name__} EventLoopExit received, exiting loop")
            return False
        except FatalException as e:
            LOG.error(f"{self.__class__.__name__} FatalException {e}")
            self.publish_status(EventType.EXCEPTION, e)
            return False
        except BaseException as e:
            LOG.exception(e)
            self.publish_status(EventType.EXCEPTION, e)
        return True

    def heartbeat(self):
        if len(self.tasks) == 0:
            return

        processed_tasks: list[int] = []

        for id, task in list(self.tasks.items()):
            if self.tick_ts < task.scheduled_ts:
                continue
            caller = self if task.caller is None else task.caller
            try:
                task.callable(caller)
            finally:
                if task.interval_ts > 0:  # should repeat
                    task.renew(self.tick_ts)
                else:
                    processed_tasks.append(id)

        for id in processed_tasks:
            del self.tasks[id]

    def run(self):
        self.before_loop()
        LOG.info(f"{self.__class__.__name__} run")
        while self.tick():
            pass
        self.after_loop()
        self.publish_status(EventType.EV_LOOP_EXIT, self)

    def handle_add_task(self, task: Task):
        self.tasks[task.id] = task

    def repeat(self, interval: float, callable: Callable, caller=None):
        task = Task(callable, delay_ts=interval, interval_ts=interval, caller=caller)
        self.put(EVENT_ADD_TASK, task)

    def delay(self, delay: float, callable: Callable, caller=None):
        task = Task(callable, delay_ts=delay, caller=caller)
        self.put(EVENT_ADD_TASK, task)
