import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models import const
from app.services.state import MemoryState, RedisState


class _FakeRedis:
    def __init__(self, batches=()):
        self.batches = batches
        self.data = {}
        self.scan_calls = []
        self.hgetall_calls = []
        self.hset_calls = []
        self.delete_calls = []
        for key in [key for batch in batches for key in batch]:
            index = int(key.decode("utf-8").split(":")[-1])
            self.data[key] = {
                b"task_id": key,
                b"state": b"1",
                b"progress": str(index).encode("utf-8"),
            }

    def scan(self, cursor, match, count):
        self.scan_calls.append((cursor, match, count))
        batch_index = int(cursor)
        next_cursor = batch_index + 1
        if next_cursor >= len(self.batches):
            next_cursor = 0
        return next_cursor, self.batches[batch_index]

    def hgetall(self, key):
        self.hgetall_calls.append(key)
        return self.data.get(key, {})

    def hset(self, key, mapping):
        self.hset_calls.append((key, mapping))

    def delete(self, *keys):
        self.delete_calls.append(keys)


class TestMemoryState(unittest.TestCase):
    def test_get_task_and_get_all_tasks_return_isolated_snapshots(self):
        state = MemoryState()
        state.update_task(
            "task-1",
            state=const.TASK_STATE_PROCESSING,
            progress=25,
            videos=["first.mp4"],
        )

        task = state.get_task("task-1")
        task["videos"].append("mutated.mp4")

        tasks, total = state.get_all_tasks(page=1, page_size=10)
        tasks[0]["videos"].append("mutated-again.mp4")

        self.assertEqual(total, 1)
        self.assertEqual(state.get_task("task-1")["videos"], ["first.mp4"])

    def test_concurrent_memory_updates_are_preserved(self):
        state = MemoryState()
        thread_count = 5
        tasks_per_thread = 50

        def update_tasks(thread_index):
            for task_index in range(tasks_per_thread):
                state.update_task(
                    f"task-{thread_index}-{task_index}",
                    state=const.TASK_STATE_PROCESSING,
                    progress=task_index,
                )

        threads = [
            threading.Thread(target=update_tasks, args=(thread_index,))
            for thread_index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        tasks, total = state.get_all_tasks(page=1, page_size=thread_count * tasks_per_thread)

        self.assertEqual(total, thread_count * tasks_per_thread)
        self.assertEqual(len(tasks), total)


class TestRedisState(unittest.TestCase):
    def _build_state(self, batch_sizes):
        keys = [f"mpt:task:{i}".encode("utf-8") for i in range(sum(batch_sizes))]
        batches = []
        offset = 0
        for batch_size in batch_sizes:
            batches.append(keys[offset : offset + batch_size])
            offset += batch_size

        state = RedisState.__new__(RedisState)
        state._redis = _FakeRedis(batches)
        return state

    def test_get_all_tasks_paginates_across_scan_batches(self):
        """
        Redis SCAN 分批返回 key 时，分页切片必须按当前批次起始位置计算。

        这个用例复现 PR #890 描述的 18 条任务、page_size=10 场景：
        第一批 10 条，第二批 8 条。旧逻辑第一页会返回空列表，第二页
        只返回 2 条；修复后第一页返回 10 条，第二页返回剩余 8 条。
        """
        state = self._build_state([10, 8])

        first_page, first_total = state.get_all_tasks(page=1, page_size=10)
        second_page, second_total = state.get_all_tasks(page=2, page_size=10)

        self.assertEqual(first_total, 18)
        self.assertEqual(second_total, 18)
        self.assertEqual(len(first_page), 10)
        self.assertEqual(len(second_page), 8)
        self.assertEqual(
            [task["task_id"] for task in first_page],
            [f"mpt:task:{i}" for i in range(10)],
        )
        self.assertEqual(
            [task["task_id"] for task in second_page],
            [f"mpt:task:{i}" for i in range(10, 18)],
        )

        self.assertTrue(
            all(call[1] == "mpt:task:*" for call in state._redis.scan_calls)
        )

    def test_update_task_uses_one_namespaced_atomic_hset(self):
        state = RedisState.__new__(RedisState)
        state._redis = _FakeRedis()

        state.update_task("abc", state=2, progress=25, videos=["video.mp4"])

        self.assertEqual(len(state._redis.hset_calls), 1)
        key, mapping = state._redis.hset_calls[0]
        self.assertEqual(key, "mpt:task:abc")
        self.assertEqual(
            mapping,
            {
                "task_id": "abc",
                "state": "2",
                "progress": "25",
                "videos": "['video.mp4']",
            },
        )

    def test_get_task_falls_back_to_exact_legacy_key_without_scan(self):
        state = RedisState.__new__(RedisState)
        state._redis = _FakeRedis()
        state._redis.data["legacy-id"] = {
            b"task_id": b"legacy-id",
            b"state": b"1",
        }

        task = state.get_task("legacy-id")

        self.assertEqual(task, {"task_id": "legacy-id", "state": 1})
        self.assertEqual(
            state._redis.hgetall_calls, ["mpt:task:legacy-id", "legacy-id"]
        )
        self.assertEqual(state._redis.scan_calls, [])

    def test_delete_task_removes_namespaced_and_legacy_state(self):
        state = RedisState.__new__(RedisState)
        state._redis = _FakeRedis()

        state.delete_task("abc")

        self.assertEqual(state._redis.delete_calls, [("mpt:task:abc", "abc")])


if __name__ == "__main__":
    unittest.main()
