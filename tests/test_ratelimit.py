"""공통 토큰 버킷의 동시성·취소·TPS 계약 검증."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import kma._ratelimit as ratelimit

AsyncTokenBucket = ratelimit.AsyncTokenBucket


class TokenBucketValidationTests(unittest.TestCase):
    def test_invalid_rate_and_capacity(self) -> None:
        for rate in (0, -1, float("nan"), float("inf"), -float("inf"), True):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                AsyncTokenBucket(rate)
        for capacity in (0, -1, 0.5, float("nan"), float("inf"), True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                AsyncTokenBucket(5, capacity=capacity)

    def test_capacity_defaults_and_read_only_configuration(self) -> None:
        self.assertEqual(AsyncTokenBucket(0.5).capacity, 1)
        self.assertEqual(AsyncTokenBucket(5).capacity, 5)
        bucket = AsyncTokenBucket(5, capacity=1)
        self.assertEqual(bucket.capacity, 1)
        for name in ("max_rps", "capacity"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(bucket, name, 0)

    def test_cross_loop_use_fails_explicitly(self) -> None:
        bucket = AsyncTokenBucket()
        asyncio.run(bucket.acquire())
        with self.assertRaisesRegex(RuntimeError, "single event loop"):
            asyncio.run(bucket.acquire())


class TokenBucketAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_wait_does_not_spend_the_following_token(self) -> None:
        now = 100.0
        delays: list[float] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            nonlocal now
            delays.append(delay)
            entered.set()
            await release.wait()
            now += delay

        fake_asyncio = SimpleNamespace(
            Lock=asyncio.Lock, get_running_loop=asyncio.get_running_loop, sleep=sleep
        )
        with (
            patch.object(ratelimit, "time", SimpleNamespace(monotonic=lambda: now)),
            patch.object(ratelimit, "asyncio", fake_asyncio),
        ):
            bucket = AsyncTokenBucket(0.5)
            await bucket.acquire()
            head = asyncio.create_task(bucket.acquire())
            await entered.wait()
            head.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await head
            entered.clear()
            follower = asyncio.create_task(bucket.acquire())
            await entered.wait()
            release.set()
            await follower
            self.assertEqual(delays, [2.0, 2.0])
            self.assertEqual(now, 102.0)

    async def test_fractional_tps_refills_and_idle_burst_is_capped(self) -> None:
        now = 100.0
        waits: list[float] = []

        async def sleep(delay: float) -> None:
            nonlocal now
            waits.append(delay)
            now += delay
            await asyncio.sleep(0)

        fake_asyncio = SimpleNamespace(
            Lock=asyncio.Lock, get_running_loop=asyncio.get_running_loop, sleep=sleep
        )
        with (
            patch.object(ratelimit, "time", SimpleNamespace(monotonic=lambda: now)),
            patch.object(ratelimit, "asyncio", fake_asyncio),
        ):
            bucket = AsyncTokenBucket(0.5)
            await bucket.acquire()
            self.assertEqual(waits, [])
            await bucket.acquire()
            self.assertEqual(waits, [2.0])
            now += 100
            await bucket.acquire()
            await bucket.acquire()
            self.assertEqual(waits, [2.0, 2.0])

    async def test_concurrent_callers_observe_fifo_and_total_budget(self) -> None:
        bucket = AsyncTokenBucket(100, capacity=1)
        await bucket.acquire()
        started = time.monotonic()
        grants: list[tuple[int, float]] = []

        async def request(index: int) -> None:
            await bucket.acquire()
            grants.append((index, time.monotonic() - started))

        await asyncio.wait_for(asyncio.gather(*(request(i) for i in range(10))), 3)
        self.assertEqual([i for i, _ in grants], list(range(10)))
        for index, elapsed in grants:
            self.assertGreaterEqual(elapsed + 0.003, (index + 1) / 100)

    async def test_cancelled_head_and_queued_waiter_do_not_stall_followers(self) -> None:
        now = 0.0
        sleeping = asyncio.Event()
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            nonlocal now
            sleeping.set()
            await release.wait()
            now += delay

        fake_asyncio = SimpleNamespace(
            Lock=asyncio.Lock, get_running_loop=asyncio.get_running_loop, sleep=sleep
        )
        with (
            patch.object(ratelimit, "time", SimpleNamespace(monotonic=lambda: now)),
            patch.object(ratelimit, "asyncio", fake_asyncio),
        ):
            bucket = AsyncTokenBucket(100, capacity=1)
            await bucket.acquire()
            head = asyncio.create_task(bucket.acquire())
            await sleeping.wait()
            queued = asyncio.create_task(bucket.acquire())
            follower = asyncio.create_task(bucket.acquire())
            await asyncio.sleep(0)
            head.cancel()
            queued.cancel()
            outcomes = await asyncio.gather(head, queued, return_exceptions=True)
            self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in outcomes))
            release.set()
            await asyncio.wait_for(follower, 1)
            await asyncio.wait_for(bucket.acquire(), 1)

    async def test_cancelling_only_waiter_leaves_no_background_task(self) -> None:
        bucket = AsyncTokenBucket(0.1, capacity=1)
        await bucket.acquire()
        waiting = asyncio.create_task(bucket.acquire())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(bucket._lock.locked())
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})


if __name__ == "__main__":
    unittest.main()
