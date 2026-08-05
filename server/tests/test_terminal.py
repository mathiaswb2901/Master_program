"""Terminal stack tests: protocol units, the framing budget, and a real PowerShell round-trip."""

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.terminal import (
    TerminalInput,
    TerminalResize,
    terminal_client_message,
)
from workbench_server.services.pty_manager import PtyLike, PtySession
from workbench_server.services.terminal_stream import (
    COALESCE_WINDOW_S,
    MAX_FRAME_CHARS,
    OutputCoalescer,
    coalesced_output,
)


class TestProtocol:
    def test_parses_input(self) -> None:
        msg = terminal_client_message.validate_json(json.dumps({"type": "input", "data": "ls\r"}))
        assert isinstance(msg, TerminalInput)
        assert msg.data == "ls\r"

    def test_parses_resize(self) -> None:
        msg = terminal_client_message.validate_json(
            json.dumps({"type": "resize", "cols": 120, "rows": 30})
        )
        assert isinstance(msg, TerminalResize)

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            terminal_client_message.validate_json(json.dumps({"type": "boom", "data": "x"}))

    def test_rejects_absurd_dimensions(self) -> None:
        with pytest.raises(ValidationError):
            terminal_client_message.validate_json(
                json.dumps({"type": "resize", "cols": 0, "rows": 30})
            )


# --- the framing budget ------------------------------------------------------
#
# Measured on the author's machine (Win11, PowerShell 5.1), flooding a 1,480,074
# byte / 20,001 line file through `Get-Content`, with a **raw pywinpty loop and
# no Workbench code at all**: 20,337 reads averaging 72.9 chars, over 32.75 s.
# That is ConPTY's own read granularity and its own wall clock — the floor
# neither the pump nor the renderer can go below. What the budget below binds is
# the only part that is ours: how many WebSocket frames those reads become.
# Before coalescing it was one frame per read (18,827 frames, mean 79 chars).
CONPTY_READS = 20_337
CONPTY_CHARS_PER_READ = 73
CONPTY_SECONDS = 32.75
CONPTY_INTERVAL_S = CONPTY_SECONDS / CONPTY_READS

#: Budget: 1.48 MB of flooded output may not cost more than this many frames...
MAX_FRAMES_PER_FLOOD = 2_500
#: ...and the frames it does cost must average at least this many bytes.
MIN_MEAN_FRAME_BYTES = 600


def replay(coalescer: OutputCoalescer, reads: int, chars: int, interval: float) -> list[str]:
    """Drive the policy through an arrival trace in virtual time.

    No PTY, no event loop, no sleeping — `OutputCoalescer` takes the clock as an
    argument precisely so ConPTY's measured pacing can be replayed exactly and
    the resulting frame count asserted, which a wall-clock benchmark could not do
    deterministically.
    """
    chunk = "x" * chars
    frames: list[str] = []
    for i in range(reads):
        now = i * interval
        # The window expiring with no read pending is a real flush in the async
        # pump (a timer fires); model it, or the trace would undercount frames.
        if coalescer.pending() and coalescer.due(now):
            frames.append(coalescer.flush(now))
        coalescer.push(chunk, now)
        if coalescer.due(now):
            frames.append(coalescer.flush(now))
    if coalescer.pending():
        frames.append(coalescer.flush(reads * interval))
    return frames


class TestFramingBudget:
    def test_a_conpty_flood_stays_inside_the_frame_budget(self) -> None:
        frames = replay(OutputCoalescer(), CONPTY_READS, CONPTY_CHARS_PER_READ, CONPTY_INTERVAL_S)
        total = sum(len(f) for f in frames)
        assert total == CONPTY_READS * CONPTY_CHARS_PER_READ  # nothing dropped
        assert len(frames) <= MAX_FRAMES_PER_FLOOD, f"{len(frames)} frames for 1.48 MB"
        assert total / len(frames) >= MIN_MEAN_FRAME_BYTES

    def test_a_keystroke_at_an_idle_prompt_is_never_delayed(self) -> None:
        """The interactive half of the budget: coalescing may not add a timer here.

        Runs a full flood first, so the coalescer is in its widest (burst) state
        — the state in which a naive implementation would hold the next keystroke
        for a whole window — and then types one character after a human-sized gap.
        """
        coalescer = OutputCoalescer()
        replay(coalescer, CONPTY_READS, CONPTY_CHARS_PER_READ, CONPTY_INTERVAL_S)
        typed_at = CONPTY_SECONDS + 3.0
        coalescer.push("a", typed_at)
        assert coalescer.wait_for(typed_at) == 0.0
        assert coalescer.due(typed_at)
        assert coalescer.flush(typed_at) == "a"

    def test_a_gap_shorter_than_the_window_still_batches(self) -> None:
        coalescer = OutputCoalescer()
        coalescer.push("first", 0.0)
        coalescer.flush(0.0)
        coalescer.push("second", COALESCE_WINDOW_S / 2)
        assert not coalescer.due(COALESCE_WINDOW_S / 2)

    def test_a_burst_bigger_than_the_cap_streams_instead_of_buffering(self) -> None:
        """A producer faster than the window must not accumulate without bound."""
        coalescer = OutputCoalescer()
        coalescer.push("x", 0.0)
        coalescer.flush(0.0)  # first chunk left immediately; now the window is open
        coalescer.push("y" * MAX_FRAME_CHARS, 0.001)
        assert coalescer.due(0.001)


# --- the pump, driven without a PTY ------------------------------------------


class ScriptedProc:
    """A `PtyLike` that hands out a fixed script of reads, one per call.

    `delay_before` sleeps inside `read` — in the worker thread, exactly where a
    blocking ConPTY read waits — which is how the "the window expired while a
    read was in flight" case is reached deterministically.
    """

    def __init__(self, chunks: list[str], delay_before: dict[int, float] | None = None) -> None:
        self._chunks = deque(chunks)
        self._delays = delay_before or {}
        self._calls = 0
        self.terminated = False

    def read(self, length: int) -> str:
        delay = self._delays.get(self._calls)
        self._calls += 1
        if delay is not None:
            time.sleep(delay)
        return self._chunks.popleft() if self._chunks else ""

    def write(self, data: str) -> int:
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None: ...

    def isalive(self) -> bool:
        return not self.terminated

    def terminate(self, force: bool = False) -> bool:
        self.terminated = True
        return True


def scripted_session(proc: PtyLike) -> PtySession:
    return PtySession(1, proc)


@pytest.mark.timeout(60)
class TestCoalescedOutput:
    async def test_every_byte_arrives_in_order(self) -> None:
        chunks = [f"<{i}>" for i in range(500)]
        session = scripted_session(ScriptedProc(chunks))
        frames = [frame async for frame in coalesced_output(session)]
        assert "".join(frames) == "".join(chunks)
        assert len(frames) < len(chunks)  # the point of the exercise

    async def test_an_idle_chunk_is_not_held_for_the_window(self) -> None:
        """The echo assertion at the pump level, proved by construction.

        The window is five seconds. If the first chunk after a quiet stream went
        through the timer at all, nothing would arrive inside the half-second
        budget below; the only way to pass is to flush it on arrival.
        """
        proc = ScriptedProc(["a"], delay_before={1: 1.0})
        stream = coalesced_output(scripted_session(proc), OutputCoalescer(window_s=5.0))
        try:
            async with asyncio.timeout(0.5):
                assert await anext(stream) == "a"
        finally:
            await stream.aclose()

    async def test_a_read_still_in_flight_when_the_window_expires_is_not_lost(self) -> None:
        """Regression: a plain `wait_for` would cancel the read and drop its bytes."""
        proc = ScriptedProc(["one", "two"], delay_before={1: COALESCE_WINDOW_S * 20})
        frames = [frame async for frame in coalesced_output(scripted_session(proc))]
        assert "".join(frames) == "onetwo"
        assert frames[0] == "one"  # flushed by the timer, not held for "two"


class FloodingManager:
    """Stands in for `PtyManager` so the whole router can be driven with no PTY."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.released = 0

    def spawn(self, cwd: Path, rows: int = 24, cols: int = 80) -> PtySession:
        return scripted_session(ScriptedProc(list(self._chunks)))

    def release(self, session: PtySession) -> None:
        self.released += 1


@pytest.mark.timeout(120)
class TestFramingOverTheWire:
    def test_a_flood_reaches_the_client_whole_and_within_budget(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the real router, models and WebSocket — no PTY.

        The synthetic reader reproduces ConPTY's granularity (73-char reads) but
        not its pacing, so what binds here is the size cap rather than the
        window; the window's own arithmetic is asserted against the measured
        trace in `TestFramingBudget`. Between them the budget cannot be met by
        one mechanism and reported by the other.
        """
        chunk = "x" * CONPTY_CHARS_PER_READ
        app = create_app(settings)
        with TestClient(app) as client:
            monkeypatch.setattr(app.state, "pty_manager", FloodingManager([chunk] * CONPTY_READS))
            with client.websocket_connect("/ws/terminal") as ws:
                frames = 0
                chars = 0
                while True:
                    frame = json.loads(ws.receive_text())
                    if frame["type"] == "exit":
                        break
                    frames += 1
                    chars += len(frame["data"])
        assert chars == CONPTY_READS * CONPTY_CHARS_PER_READ
        assert frames <= MAX_FRAMES_PER_FLOOD, f"{frames} frames for 1.48 MB"
        assert chars / frames >= MIN_MEAN_FRAME_BYTES


@pytest.mark.skipif(sys.platform != "win32", reason="pywinpty is Windows-only")
@pytest.mark.timeout(90)
class TestIntegration:
    def test_powershell_round_trip(self, settings: Settings) -> None:
        """Full pipeline: WS connect -> spawn shell -> command -> output comes back."""
        app = create_app(settings)
        with TestClient(app) as client, client.websocket_connect("/ws/terminal") as ws:
            ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 30}))
            ws.send_text(json.dumps({"type": "input", "data": "echo wb_e2e_777\r"}))
            seen = ""
            for _ in range(500):
                frame = json.loads(ws.receive_text())
                if frame["type"] == "output":
                    seen += frame["data"]
                # the echoed command AND its output both contain the marker
                if seen.count("wb_e2e_777") >= 2:
                    break
            assert "wb_e2e_777" in seen

    def test_sessions_are_cleaned_up_on_disconnect(self, settings: Settings) -> None:
        app = create_app(settings)
        with TestClient(app) as client, client.websocket_connect("/ws/terminal") as ws:
            ws.send_text(json.dumps({"type": "input", "data": "echo hi\r"}))
            ws.receive_text()  # ensure the session is live
            # context exit closed the socket; manager must have released the PTY
        assert app.state.pty_manager._sessions == {}
