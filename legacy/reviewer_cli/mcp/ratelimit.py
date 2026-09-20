"""Keep concurrent specialists inside GitHub's secondary rate limits.

GitHub allows 900 REST points per minute: a GET costs 1 point, a mutating call
costs 5. With several specialists reading files at once nothing in the agent
loop is aware of the shared budget, so the accounting has to live at the
transport boundary.

This is a sliding sixty-second window rather than a fixed one — a fixed window
lets a burst at 0:59 and another at 1:01 exceed the limit across the boundary.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

# GET/HEAD/OPTIONS cost 1 point; POST/PATCH/PUT/DELETE cost 5.
READ_POINTS = 1
WRITE_POINTS = 5


class PointBucket:
    """Async sliding-window limiter measured in GitHub's REST points."""

    def __init__(self, points_per_minute: int = 900, window: float = 60.0) -> None:
        self._budget = points_per_minute
        self._window = window
        self._spends: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _spent(self, now: float) -> int:
        while self._spends and now - self._spends[0][0] >= self._window:
            self._spends.popleft()
        return sum(points for _, points in self._spends)

    async def take(self, points: int = READ_POINTS) -> None:
        """Block until ``points`` fit inside the window, then record them."""
        while True:
            async with self._lock:
                now = time.monotonic()
                if self._spent(now) + points <= self._budget:
                    self._spends.append((now, points))
                    return
                # Wait for the oldest spend to age out of the window.
                oldest = self._spends[0][0]
                delay = max(0.0, self._window - (now - oldest))
            await asyncio.sleep(delay + 0.01)
