import json
from typing import Dict

import redis
from loguru import logger

from app.controllers.manager.base_manager import TaskManager, TaskQueueFullError
from app.models.schema import VideoParams
from app.services import task as tm

FUNC_MAP = {
    "start": tm.start,
    # 'start_test': tm.start_test
}

PENDING_QUEUE = "mpt:queue:video"
PROCESSING_QUEUE = "mpt:processing:video"
LEGACY_QUEUE = "task_queue"
RECEIPT_KEY = "_queue_receipt"

MIGRATE_LEGACY_SCRIPT = """
local tasks = redis.call('LRANGE', KEYS[1], 0, -1)
for _, task in ipairs(tasks) do
    redis.call('RPUSH', KEYS[2], task)
end
if #tasks > 0 then
    redis.call('DEL', KEYS[1])
end
return #tasks
"""

RECOVER_PROCESSING_SCRIPT = """
local tasks = redis.call('LRANGE', KEYS[1], 0, -1)
for index = #tasks, 1, -1 do
    redis.call('LPUSH', KEYS[2], tasks[index])
end
if #tasks > 0 then
    redis.call('DEL', KEYS[1])
end
return #tasks
"""

REQUEUE_SCRIPT = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[1])
end
return removed
"""


class RedisTaskManager(TaskManager):
    def __init__(
        self,
        max_concurrent_tasks: int,
        redis_url: str,
        max_queued_tasks: int = 100,
    ):
        self.redis_client = redis.Redis.from_url(redis_url)
        self.processing_queue = PROCESSING_QUEUE
        super().__init__(max_concurrent_tasks, max_queued_tasks=max_queued_tasks)

    def create_queue(self):
        return PENDING_QUEUE

    def initialize_queue(self):
        self.redis_client.eval(
            MIGRATE_LEGACY_SCRIPT, 2, LEGACY_QUEUE, self.queue
        )
        self.recover_abandoned_tasks()

    def recover_abandoned_tasks(self):
        self.redis_client.eval(
            RECOVER_PROCESSING_SCRIPT, 2, self.processing_queue, self.queue
        )

    def add_task(self, func, *args, **kwargs):
        with self.lock:
            queue_size = self.queue_size()
            if (
                self.current_tasks >= self.max_concurrent_tasks
                and queue_size >= self.max_queued_tasks
            ):
                logger.warning(
                    f"reject task: {func.__name__}, queue_size: {queue_size}, "
                    f"max_queued_tasks: {self.max_queued_tasks}"
                )
                raise TaskQueueFullError("task queue is full, please try again later")
            self.enqueue({"func": func, "args": args, "kwargs": kwargs})
        self.check_queue()

    def enqueue(self, task: Dict):
        task_with_serializable_params = {
            **{key: value for key, value in task.items() if key != RECEIPT_KEY},
            "kwargs": dict(task.get("kwargs", {})),
        }

        if "params" in task["kwargs"] and isinstance(
            task["kwargs"]["params"], VideoParams
        ):
            task_with_serializable_params["kwargs"]["params"] = task["kwargs"][
                "params"
            ].model_dump(warnings=False)

        # 将函数对象转换为其名称
        task_with_serializable_params["func"] = task["func"].__name__
        self.redis_client.rpush(self.queue, json.dumps(task_with_serializable_params))

    def dequeue(self):
        task_json = self.redis_client.lmove(
            self.queue, self.processing_queue, "LEFT", "RIGHT"
        )
        if task_json:
            task_info = json.loads(task_json)
            task_info[RECEIPT_KEY] = task_json
            # 将函数名称转换回函数对象
            task_info["func"] = FUNC_MAP[task_info["func"]]

            if "params" in task_info["kwargs"] and isinstance(
                task_info["kwargs"]["params"], dict
            ):
                task_info["kwargs"]["params"] = VideoParams(
                    **task_info["kwargs"]["params"]
                )

            return task_info
        return None

    def task_receipt(self, task: Dict):
        return task.get(RECEIPT_KEY)

    def acknowledge(self, receipt):
        self.redis_client.lrem(self.processing_queue, 1, receipt)

    def requeue(self, task: Dict):
        receipt = self.task_receipt(task)
        if receipt is None:
            return super().requeue(task)
        self.redis_client.eval(
            REQUEUE_SCRIPT, 2, self.processing_queue, self.queue, receipt
        )

    def is_queue_empty(self):
        return self.redis_client.llen(self.queue) == 0

    def queue_size(self):
        return self.redis_client.llen(self.queue)
