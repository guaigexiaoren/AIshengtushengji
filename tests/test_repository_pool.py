from __future__ import annotations

import unittest

from yixin.collaboration.repository import _ConnectionPool


class FakeConnection:
    def __init__(self) -> None:
        self.healthy = True
        self.closed = False

    def ping(self, reconnect: bool = False) -> bool:
        if not self.healthy:
            raise OSError("connection lost")
        return True

    def close(self) -> None:
        self.closed = True


class ConnectionPoolTests(unittest.TestCase):
    def test_reuses_released_connection(self) -> None:
        created: list[FakeConnection] = []

        def factory() -> FakeConnection:
            connection = FakeConnection()
            created.append(connection)
            return connection

        pool = _ConnectionPool(factory, max_size=2)
        first = pool.acquire()
        pool.release(first)
        second = pool.acquire()
        self.assertIs(first, second)
        self.assertEqual(len(created), 1)

    def test_replaces_dead_connection(self) -> None:
        created: list[FakeConnection] = []

        def factory() -> FakeConnection:
            connection = FakeConnection()
            created.append(connection)
            return connection

        pool = _ConnectionPool(factory, max_size=2)
        dead = pool.acquire()
        dead.healthy = False
        pool.release(dead)
        replacement = pool.acquire()
        self.assertIsNot(dead, replacement)
        self.assertTrue(dead.closed)
        self.assertEqual(len(created), 2)

    def test_release_beyond_capacity_closes_connection(self) -> None:
        def factory() -> FakeConnection:
            return FakeConnection()

        pool = _ConnectionPool(factory, max_size=1)
        connection = pool.acquire()
        # 队列已满（容量 1）时再次归还同一个连接应被直接关闭。
        pool.release(connection)
        pool.release(connection)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
