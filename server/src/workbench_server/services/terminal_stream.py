"""Batching PTY reads into WebSocket frames without costing interactive latency.

ConPTY hands PowerShell's output over in tiny pieces: measured on this machine,
flooding a 1.48 MB file through `Get-Content` produced **20,337 reads averaging
73 chars**, arriving at ~620/s — and a raw pywinpty loop with no Workbench code
in it at all sees the same shape, so the granularity is ConPTY's and is not ours
to fix. What *was* ours is that the pump sent one Pydantic model, one
``model_dump_json()`` and one ``send_text()`` per read: 18,827 frames for that
flood, a mean of 79 chars each.

The rule here is Nagle's, with one twist for bursts:

* **The first chunk after a quiet stream goes out immediately.** No timer, no
  wait. That is the interactive path — the echo of a keystroke at an idle prompt
  is exactly "the first chunk after a gap" — and it is why coalescing can be
  added without the terminal feeling laggier to type in.
* **While the stream is busy, frames leave at most once per window** and every
  read in between rides along. `COALESCE_WINDOW_S` bounds how long any byte can
  be held.
* **Unbroken output widens the window** to `BURST_WINDOW_S` after
  `BURST_AFTER_FRAMES` back-to-back frames (~130 ms of output that never paused).
  The arithmetic forces this: ConPTY delivers ~49 KB/s for that flood, so mean
  frame size is throughput x window, and an 8 ms window can only ever produce
  ~390-char frames. A stream that has been saturated for 130 ms is a flood, not
  an interaction; nobody reads 620 lines/s, so trading 16 ms of latency there for
  3x fewer frames is free. Any gap at all drops straight back to the base window.
* **A batch that reaches `MAX_FRAME_CHARS` is sent at once**, so a producer
  faster than the window keeps streaming instead of buffering without bound.

`OutputCoalescer` holds no I/O and no clock: the caller passes seconds in. That is
what lets `test_terminal.py` replay the measured ConPTY arrival trace in virtual
time and count frames deterministically, with no PTY and no sleeping.

**Windows clocks.** The policy is driven by `time.perf_counter` and deliberately
not by `loop.time()`: on CPython 3.11 `time.monotonic()` is Windows' tick count,
whose resolution is the system timer period — 15.6 ms by default, and finer only
while some process on the box has raised it. Measured with `loop.time()` here,
1.6 ms apart ConPTY reads reported gaps of 0 ms or 15 ms and nothing between,
which made the idle test above (`now - last_flush >= window`) fire at random: a
keystroke could be held for a window, and a flood could reset the burst state on
every tick. `perf_counter` is QPC and has no such problem. The event loop's own
timer still runs on the coarse clock, so the *effective* window can be as long as
one system tick — but coarser only ever means bigger frames, never a slower
keystroke, because the interactive path never goes through a timer at all.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable

from workbench_server.services.pty_manager import PtySession

#: Longest any byte waits while the stream is merely busy.
COALESCE_WINDOW_S = 0.008
#: Window used once output has been unbroken for `BURST_AFTER_FRAMES` frames.
BURST_WINDOW_S = 0.024
#: Back-to-back frames before a stream counts as a flood rather than an interaction.
BURST_AFTER_FRAMES = 16
#: Hard cap on one frame, so a fast producer streams rather than buffers.
MAX_FRAME_CHARS = 64 * 1024
#: Floor on a re-arm after the loop woke us early (see `coalesced_output`).
MIN_WAIT_S = 0.001


class OutputCoalescer:
    """Decides when a run of PTY reads becomes one WebSocket frame.

    Pure and clock-free — every method takes `now` (monotonic seconds) from the
    caller, so the policy can be replayed against a recorded arrival trace.
    """

    def __init__(
        self,
        window_s: float = COALESCE_WINDOW_S,
        burst_window_s: float = BURST_WINDOW_S,
        burst_after_frames: int = BURST_AFTER_FRAMES,
        max_chars: int = MAX_FRAME_CHARS,
    ) -> None:
        self._window = window_s
        self._burst_window = burst_window_s
        self._burst_after = burst_after_frames
        self._max_chars = max_chars
        self._buf: list[str] = []
        self._size = 0
        self._deadline = 0.0
        self._last_flush: float | None = None
        self._streak = 0

    def push(self, data: str, now: float) -> None:
        """Add a read to the open batch, starting one if none is open."""
        if not self._buf:
            last = self._last_flush
            # Idle is always judged against the *base* window: a stream that
            # paused is interactive again, whatever it was doing before.
            if last is None or now - last >= self._window:
                self._streak = 0
                self._deadline = now
            else:
                self._streak += 1
                busy = self._streak >= self._burst_after
                self._deadline = last + (self._burst_window if busy else self._window)
        self._buf.append(data)
        self._size += len(data)

    def pending(self) -> bool:
        return self._size > 0

    def due(self, now: float) -> bool:
        """Must the open batch go out now?"""
        return self._size >= self._max_chars or now >= self._deadline

    def wait_for(self, now: float) -> float:
        """Seconds until the open batch is due (0 when it already is)."""
        return max(0.0, self._deadline - now)

    def flush(self, now: float) -> str:
        data = "".join(self._buf)
        self._buf.clear()
        self._size = 0
        self._last_flush = now
        return data


async def coalesced_output(
    session: PtySession,
    coalescer: OutputCoalescer | None = None,
    clock: Callable[[], float] | None = None,
) -> AsyncGenerator[str, None]:
    """Yield the session's output as whole frames; ends when the process exits.

    The in-flight read is a task that is *never* dropped: when the coalescing
    window expires first, the task keeps running and is awaited again next time
    round. Cancelling it (what a plain ``wait_for`` would do) would discard a
    chunk ConPTY had already handed over.
    """
    now = clock if clock is not None else time.perf_counter
    batch = coalescer if coalescer is not None else OutputCoalescer()
    read: asyncio.Task[str | None] | None = None
    try:
        while True:
            if read is None:
                read = asyncio.create_task(session.read())
            timeout = max(batch.wait_for(now()), MIN_WAIT_S) if batch.pending() else None
            done, _ = await asyncio.wait({read}, timeout=timeout)
            if not done:
                # `asyncio` arms its timers off `loop.time()`, which on Windows
                # is a tick counter that lags real time by up to one system tick
                # — so a 24 ms timeout can fire 15 ms early. `perf_counter` is
                # the authority: an early wake re-arms rather than cutting the
                # frame short (measured: without this, the burst window produced
                # 9 ms frames and 3,577 of them instead of ~1,400).
                if batch.due(now()):
                    yield batch.flush(now())
                continue
            data = read.result()
            read = None
            if data is None:
                if batch.pending():
                    yield batch.flush(now())
                return
            batch.push(data, now())
            if batch.due(now()):
                yield batch.flush(now())
    finally:
        if read is not None:
            read.cancel()
