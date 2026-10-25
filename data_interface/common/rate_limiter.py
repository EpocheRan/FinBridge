"""进程内滑动窗口限流器。

Tushare 2120 积分对应 200 次/分钟/接口。这里先主动限流，避免把配额打满；
如果仍然触发服务端限流，由调用方捕获后抛出 ``RateLimitExceededError``。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """占用一个调用额度，返回实际等待秒数。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window_seconds:
                    self._calls.popleft()

                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return waited

                sleep_for = self.window_seconds - (now - self._calls[0]) + 0.01
            time.sleep(max(sleep_for, 0.01))
            waited += sleep_for

    def try_acquire(self) -> bool:
        """非阻塞占用额度；额度不足返回 False。"""
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.window_seconds:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.window_seconds:
                self._calls.popleft()
            return {
                "max_calls": self.max_calls,
                "window_seconds": self.window_seconds,
                "used": len(self._calls),
                "remaining": self.max_calls - len(self._calls),
            }
