import json
import unittest
from unittest.mock import MagicMock, patch

from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import (
    LEGACY_QUEUE,
    MIGRATE_LEGACY_SCRIPT,
    PROCESSING_QUEUE,
    RECEIPT_KEY,
    RECOVER_PROCESSING_SCRIPT,
    REQUEUE_SCRIPT,
    RedisTaskManager,
)
from app.models.schema import VideoParams
from app.services import task as task_service


class TestInMemoryTaskManager(unittest.TestCase):
    def test_queue_operations_preserve_task_payload(self):
        """内存队列应保持函数、位置参数和关键字参数，不得改变任务内容。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        task = {"func": len, "args": ([1, 2],), "kwargs": {}}

        manager.enqueue(task)

        self.assertFalse(manager.is_queue_empty())
        self.assertEqual(manager.queue_size(), 1)
        self.assertEqual(manager.dequeue(), task)
        self.assertTrue(manager.is_queue_empty())

    def test_add_task_rejects_only_after_queue_limit(self):
        """并发名额用尽后允许排队到上限，超过上限才返回明确错误。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=0, max_queued_tasks=1)

        manager.add_task(len, [1])

        with self.assertRaises(TaskQueueFullError):
            manager.add_task(len, [2])

    def test_add_task_reserves_slot_before_background_thread_runs(self):
        """
        并发名额必须在线程启动前预占；即使 mock 的线程尚未进入 run_task，
        第二个请求也应进入队列，不能突破 max_concurrent_tasks。
        """
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)

        with patch.object(manager, "execute_task") as execute_task:
            manager.add_task(len, [1])
            manager.add_task(len, [2])

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1])
        self.assertEqual(manager.queue_size(), 1)

    def test_add_task_rolls_back_slot_when_thread_cannot_start(self):
        """线程启动失败不能永久占用并发名额，异常仍应交给调用方处理。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.add_task(len, [1])

        self.assertEqual(manager.current_tasks, 0)

    def test_task_done_starts_next_queued_task(self):
        """当前任务结束后应释放并发名额，并立即调度队列中的下一个任务。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        manager.current_tasks = 1
        manager.enqueue({"func": len, "args": ([1, 2],), "kwargs": {}})

        with patch.object(manager, "execute_task") as execute_task:
            manager.task_done()

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1, 2])
        self.assertTrue(manager.is_queue_empty())

    def test_task_done_requeues_task_when_thread_cannot_start(self):
        """出队后若线程启动失败，应回滚名额并把任务放回队列，避免任务丢失。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)
        manager.current_tasks = 1
        queued_task = {"func": len, "args": ([1, 2],), "kwargs": {}}
        manager.enqueue(queued_task)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.task_done()

        self.assertEqual(manager.current_tasks, 0)
        self.assertEqual(manager.dequeue(), queued_task)

    def test_run_task_releases_slot_after_failure(self):
        """任务函数抛出异常时 finally 仍必须释放名额，避免队列永久阻塞。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        manager.current_tasks = 1

        with patch.object(manager, "task_done") as task_done:
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                manager.run_task(MagicMock(side_effect=RuntimeError("task failed")))

        self.assertEqual(manager.current_tasks, 1)
        task_done.assert_called_once_with()

    def test_resume_queued_tasks_fills_available_slots(self):
        manager = InMemoryTaskManager(max_concurrent_tasks=2)
        first = MagicMock()
        second = MagicMock()
        manager.enqueue({"func": first, "args": (), "kwargs": {}})
        manager.enqueue({"func": second, "args": (), "kwargs": {}})

        with patch.object(manager, "execute_task") as execute:
            manager.resume_queued_tasks()

        self.assertEqual(manager.current_tasks, 2)
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(manager.is_queue_empty())

    def test_execute_task_starts_background_thread(self):
        """任务执行入口必须启动线程，并把函数参数完整传给 run_task。"""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        fake_thread = MagicMock()

        with patch(
            "app.controllers.manager.base_manager.threading.Thread",
            return_value=fake_thread,
        ) as thread:
            manager.execute_task(len, [1, 2])

        thread.assert_called_once_with(
            target=manager.run_task,
            args=(len, [1, 2]),
            kwargs={},
        )
        fake_thread.start.assert_called_once_with()


class TestRedisTaskManager(unittest.TestCase):
    def setUp(self):
        self.redis_client = MagicMock()
        patcher = patch(
            "app.controllers.manager.redis_manager.redis.Redis.from_url",
            return_value=self.redis_client,
        )
        self.addCleanup(patcher.stop)
        from_url = patcher.start()
        self.manager = RedisTaskManager(
            max_concurrent_tasks=1,
            redis_url="redis://localhost:6379/0",
            max_queued_tasks=3,
        )
        from_url.assert_called_once_with("redis://localhost:6379/0")
        self.redis_client.reset_mock()

    def test_enqueue_serializes_video_params_without_mutating_task(self):
        """
        Redis 只能存 JSON；VideoParams 应转换成字典，但原任务仍需保留模型，
        避免序列化副作用影响日志、重试或调用方后续读取。
        """
        params = VideoParams(video_subject="Coffee")
        task = {
            "func": task_service.start,
            "args": (),
            "kwargs": {"task_id": "task-1", "params": params},
        }

        self.manager.enqueue(task)

        self.assertIs(task["kwargs"]["params"], params)
        queue_name, payload = self.redis_client.rpush.call_args.args
        decoded = json.loads(payload)
        self.assertEqual(queue_name, "mpt:queue:video")
        self.assertEqual(decoded["func"], "start")
        self.assertEqual(decoded["kwargs"]["task_id"], "task-1")
        self.assertEqual(decoded["kwargs"]["params"]["video_subject"], "Coffee")

    def test_add_task_is_persisted_before_a_worker_starts(self):
        self.redis_client.llen.side_effect = [0, 1]
        self.redis_client.lmove.return_value = json.dumps(
            {"func": "start", "args": [], "kwargs": {"task_id": "task-1"}}
        )

        with patch.object(self.manager, "execute_task") as execute_task:
            self.manager.add_task(task_service.start, task_id="task-1")

        self.assertEqual(
            [call[0] for call in self.redis_client.method_calls],
            ["llen", "rpush", "llen", "lmove"],
        )
        queue_name, payload = self.redis_client.rpush.call_args.args
        self.assertEqual(queue_name, "mpt:queue:video")
        self.assertEqual(json.loads(payload)["kwargs"]["task_id"], "task-1")
        execute_task.assert_called_once_with(
            task_service.start,
            _task_receipt=self.redis_client.lmove.return_value,
            task_id="task-1",
        )

    def test_dequeue_restores_function_and_video_params(self):
        """从 Redis 取出的任务应恢复可调用函数和 VideoParams 模型。"""
        payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-1",
                "params": VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ),
            },
        }
        receipt = json.dumps(payload)
        self.redis_client.lmove.return_value = receipt

        task = self.manager.dequeue()

        self.redis_client.lmove.assert_called_once_with(
            "mpt:queue:video", PROCESSING_QUEUE, "LEFT", "RIGHT"
        )
        self.assertEqual(task[RECEIPT_KEY], receipt)
        self.assertIs(task["func"], task_service.start)
        self.assertIsInstance(task["kwargs"]["params"], VideoParams)
        self.assertEqual(task["kwargs"]["params"].video_subject, "Coffee")

    def test_empty_queue_and_size_use_redis_length(self):
        """队列判空和长度必须直接反映 Redis 当前列表长度。"""
        self.redis_client.lmove.return_value = None
        self.redis_client.llen.side_effect = [0, 2]

        self.assertIsNone(self.manager.dequeue())
        self.assertTrue(self.manager.is_queue_empty())
        self.assertEqual(self.manager.queue_size(), 2)

    def test_initialization_migrates_legacy_and_recovers_abandoned_tasks(self):
        redis_client = MagicMock()
        with patch(
            "app.controllers.manager.redis_manager.redis.Redis.from_url",
            return_value=redis_client,
        ):
            RedisTaskManager(1, "redis://localhost:6379/0")

        self.assertEqual(
            redis_client.eval.call_args_list,
            [
                unittest.mock.call(
                    MIGRATE_LEGACY_SCRIPT,
                    2,
                    LEGACY_QUEUE,
                    "mpt:queue:video",
                ),
                unittest.mock.call(
                    RECOVER_PROCESSING_SCRIPT,
                    2,
                    PROCESSING_QUEUE,
                    "mpt:queue:video",
                ),
            ],
        )

    def test_resume_recovers_processing_before_starting_pending_tasks(self):
        self.redis_client.llen.return_value = 0

        self.manager.resume_queued_tasks()

        self.redis_client.eval.assert_called_once_with(
            RECOVER_PROCESSING_SCRIPT,
            2,
            PROCESSING_QUEUE,
            "mpt:queue:video",
        )

    def test_receipt_is_acked_only_after_task_finishes(self):
        events = []
        receipt = b'{"func":"start"}'
        self.redis_client.lrem.side_effect = lambda *args: events.append("ack")
        self.redis_client.llen.return_value = 0

        self.manager.current_tasks = 1
        self.manager.run_task(
            lambda: events.append("finished"), _task_receipt=receipt
        )

        self.assertEqual(events, ["finished", "ack"])
        self.redis_client.lrem.assert_called_once_with(
            PROCESSING_QUEUE, 1, receipt
        )

    def test_thread_start_failure_atomically_requeues_receipt_without_enqueue(self):
        payload = json.dumps(
            {"func": "start", "args": [], "kwargs": {"task_id": "task-1"}}
        )
        self.redis_client.llen.return_value = 1
        self.redis_client.lmove.return_value = payload
        self.manager.current_tasks = 1

        with patch.object(
            self.manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                self.manager.task_done()

        self.redis_client.eval.assert_called_once_with(
            REQUEUE_SCRIPT,
            2,
            PROCESSING_QUEUE,
            "mpt:queue:video",
            payload,
        )
        self.redis_client.rpush.assert_not_called()
        self.assertEqual(self.manager.current_tasks, 0)


if __name__ == "__main__":
    unittest.main()
