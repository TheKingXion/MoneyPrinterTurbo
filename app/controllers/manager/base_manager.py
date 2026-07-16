import threading
from typing import Any, Callable, Dict

from loguru import logger


class TaskQueueFullError(ValueError):
    pass


class TaskManager:
    def __init__(self, max_concurrent_tasks: int, max_queued_tasks: int = 100):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.max_queued_tasks = max_queued_tasks
        self.current_tasks = 0
        self.lock = threading.Lock()
        self.queue = self.create_queue()
        self.initialize_queue()

    def initialize_queue(self):
        pass

    def recover_abandoned_tasks(self):
        pass

    def create_queue(self):
        raise NotImplementedError()

    def add_task(self, func: Callable, *args: Any, **kwargs: Any):
        with self.lock:
            if self.current_tasks < self.max_concurrent_tasks:
                logger.info(
                    f"add task: {func.__name__}, current_tasks: {self.current_tasks}"
                )
                self.current_tasks += 1
                try:
                    self.execute_task(func, *args, **kwargs)
                except Exception:
                    self.current_tasks -= 1
                    raise
            else:
                queue_size = self.queue_size()
                # 并发数已满时才进入排队。队列必须有上限，否则匿名接口可以持续
                # 堆积任务对象和请求参数，最终造成内存耗尽或第三方 API 成本失控。
                if queue_size >= self.max_queued_tasks:
                    logger.warning(
                        f"reject task: {func.__name__}, queue_size: {queue_size}, "
                        f"max_queued_tasks: {self.max_queued_tasks}"
                    )
                    raise TaskQueueFullError("task queue is full, please try again later")

                logger.info(
                    f"enqueue task: {func.__name__}, current_tasks: {self.current_tasks}, "
                    f"queue_size: {queue_size}"
                )
                self.enqueue({"func": func, "args": args, "kwargs": kwargs})

    def execute_task(
        self, func: Callable, *args: Any, _task_receipt=None, **kwargs: Any
    ):
        if _task_receipt is not None:
            kwargs = {**kwargs, "_task_receipt": _task_receipt}
        thread = threading.Thread(
            target=self.run_task, args=(func, *args), kwargs=kwargs
        )
        thread.start()

    def run_task(
        self, func: Callable, *args: Any, _task_receipt=None, **kwargs: Any
    ):
        try:
            func(*args, **kwargs)  # call the function here, passing *args and **kwargs.
        except Exception:
            function_name = getattr(func, "__name__", type(func).__name__)
            logger.exception(f"task failed: {function_name}")
            raise
        finally:
            if _task_receipt is None:
                self.task_done()
            else:
                self.task_done(_task_receipt)

    def check_queue(self):
        with self.lock:
            if (
                self.current_tasks < self.max_concurrent_tasks
                and not self.is_queue_empty()
            ):
                task_info = self.dequeue()
                func = task_info["func"]
                args = task_info.get("args", ())
                kwargs = task_info.get("kwargs", {})
                receipt = self.task_receipt(task_info)
                self.current_tasks += 1
                try:
                    if receipt is None:
                        self.execute_task(func, *args, **kwargs)
                    else:
                        self.execute_task(
                            func, *args, _task_receipt=receipt, **kwargs
                        )
                except Exception:
                    self.current_tasks -= 1
                    # Starting a worker can fail transiently (thread limits,
                    # interpreter shutdown). Preserve the dequeued task so a
                    # later queue check can retry it instead of losing work.
                    self.requeue(task_info)
                    raise

    def task_receipt(self, task: Dict):
        return None

    def acknowledge(self, receipt):
        pass

    def requeue(self, task: Dict):
        self.enqueue(task)

    def task_done(self, receipt=None):
        try:
            if receipt is not None:
                try:
                    self.acknowledge(receipt)
                except Exception:
                    logger.exception("failed to acknowledge completed queued task")
        finally:
            with self.lock:
                self.current_tasks -= 1
        self.check_queue()

    def resume_queued_tasks(self):
        """Fill available local worker slots from a queue left by a restart."""
        self.recover_abandoned_tasks()
        while True:
            with self.lock:
                can_start = (
                    self.current_tasks < self.max_concurrent_tasks
                    and not self.is_queue_empty()
                )
            if not can_start:
                return
            self.check_queue()

    def enqueue(self, task: Dict):
        raise NotImplementedError()

    def dequeue(self):
        raise NotImplementedError()

    def is_queue_empty(self):
        raise NotImplementedError()

    def queue_size(self):
        raise NotImplementedError()
