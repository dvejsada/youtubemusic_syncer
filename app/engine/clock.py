"""Time hooks. Everything that waits or reads the clock goes through clock.sleep / clock.now, so tests
can patch them (engine.clock.sleep / engine.clock.now) and waits run instantly."""
from __future__ import annotations

import time


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def now() -> float:
    return time.time()


def sleep_interruptible(seconds: float, should_stop) -> None:
    """Sleep in <=1 s slices so an app shutdown is noticed quickly."""
    remaining = float(seconds)
    while remaining > 0 and not should_stop():
        step = min(1.0, remaining)
        sleep(step)
        remaining -= step
