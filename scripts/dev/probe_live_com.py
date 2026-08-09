"""Probe: what a *live* Excel really hands back over COM, versus the file on disk.

Run from the repo root, on a Windows machine with Excel installed::

    uv run python scripts/dev/probe_live_com.py

It is a **probe, not a gate**. No CI job runs it and nothing imports it — its
absence from the quality gate is deliberate, not an oversight. It exists because
three facts the live reconciliation reader is built on belong to *pywin32 and
Office*, not to us, and a version bump on either is the thing that would change
them:

1. **A docked, unsaved workbook makes the file on disk a lie.** ``Workbook.Saved``
   flips to ``False`` on an edit, the live cell carries the new number, and the
   ``.xlsx`` still holds the old one — which is exactly the false PASS
   ``services/reconciliation.py``'s front gate exists to refuse.
2. **A formula cell that Excel has never calculated has no cached value on disk.**
   ``openpyxl(data_only=True)`` returns ``None`` for it while the live read
   returns the number, so "read the file" is not a conservative fallback; it is a
   different answer.
3. **``Range.Value`` returns tz-*aware* datetimes whose offset is not the
   machine's zone.** ``.replace(tzinfo=None)`` is the correct conversion to naive
   local wall clock and ``.astimezone()`` is an hour wrong on the fall-back date
   — the one day the DST gate exists for. The probe asserts this on
   ``2024-10-27 02:00`` (Europe/Oslo's fall-back hour) so the claim is measured
   rather than remembered.

**It cleans up after itself, and says whether it succeeded.** One private Excel
instance is launched with ``DispatchEx`` (never the user's), the workbook is
closed without saving, the application is quit, and the probe then reports
whether the pid it created is still running — a leaked Office process is the one
failure mode a probe like this must never be quiet about.

structlog is deliberately not used: this is a developer script run by hand, and
its output is a report a person reads, not a log line a service emits.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# The fall-back hour the DST gate exists for: Europe/Oslo writes 02:00 twice on
# this date, and a normalisation that moves it is wrong on precisely this row.
FALL_BACK = datetime(2024, 10, 27, 2, 0)


def _fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


def _seed(path: Path) -> None:
    """A workbook with one number, one formula that was never calculated, and
    one naive local timestamp — the three shapes the reader has to get right."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    if sheet is None:  # pragma: no cover - a new Workbook always has one
        raise RuntimeError("openpyxl gave a workbook with no active sheet")
    sheet["A1"] = "value"
    sheet["B1"] = 1234.5
    sheet["C1"] = "=B1*2"  # written by openpyxl, so Excel has cached nothing
    sheet["A2"] = FALL_BACK
    book.save(path)
    book.close()


def _disk(path: Path, cell: str) -> Any:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        if sheet is None:  # pragma: no cover - the probe writes its own workbook
            raise RuntimeError("the probe workbook has no active sheet")
        return sheet[cell].value
    finally:
        book.close()


def _still_running(pid: int) -> bool:
    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]

    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    except Exception:
        return False
    try:
        return bool(win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT)
    finally:
        win32api.CloseHandle(handle)


def main() -> int:
    if sys.platform != "win32":
        return _fail("this probe needs Windows and a real Excel")
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    problems: list[str] = []
    pythoncom.CoInitialize()
    with TemporaryDirectory(prefix="wb-probe-") as tmp:
        book_path = Path(tmp) / "probe.xlsx"
        _seed(book_path)

        excel = win32com.client.DispatchEx("Excel.Application")
        pid = 0
        try:
            excel.Visible = False
            excel.DisplayAlerts = False
            workbook = excel.Workbooks.Open(str(book_path))
            pid = _pid_of(excel)
            print(f"launched a private Excel (pid {pid})")

            sheet = workbook.Worksheets(1)
            print(f"  Workbook.Saved (clean)        {workbook.Saved!r}")
            if workbook.Saved is not True:
                problems.append("a freshly opened workbook did not report Saved=True")

            # ---- 3. the timestamp, before anything is edited --------------------
            live_ts = sheet.Range("A2").Value
            disk_ts = _disk(book_path, "A2")
            print(f"  disk A2 (openpyxl)            {disk_ts!r}")
            print(f"  live A2 (Range.Value)         {live_ts!r}")
            replaced = live_ts.replace(tzinfo=None)
            print(f"    .replace(tzinfo=None)       {replaced}")
            if replaced != FALL_BACK:
                problems.append(f".replace(tzinfo=None) gave {replaced}, expected {FALL_BACK}")
            if live_ts.tzinfo is not None:
                shifted = live_ts.astimezone().replace(tzinfo=None)
                print(f"    .astimezone().replace()     {shifted}")
                if shifted == FALL_BACK:
                    problems.append(
                        "astimezone() agreed with replace() here — the offset COM attaches "
                        "may have changed, so re-read correction 4 of the plan"
                    )
            else:
                print("    (COM returned a naive datetime — the offset claim no longer holds)")

            # ---- 1 + 2. the live/disk divergence -------------------------------
            sheet.Range("B1").Value = 9999
            excel.CalculateFull()
            for _ in range(50):  # CalculationState: 0 xlDone, 1 xlCalculating, 2 xlPending
                if int(excel.CalculationState) == 0:
                    break
                time.sleep(0.05)
            print(f"  Application.CalculationState  {int(excel.CalculationState)} (0 = done)")
            print(f"  Workbook.Saved (after edit)   {workbook.Saved!r}")
            if workbook.Saved is not False:
                problems.append("an edited workbook did not report Saved=False")

            live_b1 = sheet.Range("B1").Value
            live_c1 = sheet.Range("C1").Value
            disk_b1 = _disk(book_path, "B1")
            disk_c1 = _disk(book_path, "C1")
            print(f"  live B1                       {live_b1!r}")
            print(f"  disk B1                       {disk_b1!r}   <- what a disk gate judges")
            print(f"  live C1 (=B1*2)               {live_c1!r}")
            print(f"  disk C1                       {disk_c1!r}   <- never calculated")
            if live_b1 == disk_b1:
                problems.append("the live edit reached disk without a save — the premise is gone")
            if disk_c1 is not None:
                problems.append("an uncalculated formula had a cached disk value")

            workbook.Close(SaveChanges=False)
        finally:
            try:
                excel.Quit()
            except Exception as error:  # the probe still has to report the pid
                problems.append(f"Quit() raised: {error}")
            del excel
            pythoncom.CoUninitialize()

        for _ in range(40):
            if not _still_running(pid):
                break
            time.sleep(0.1)
        leaked = _still_running(pid)
        print(f"  leaked an Office process?     {'YES' if leaked else 'no'} (pid {pid})")
        if leaked:
            problems.append(f"pid {pid} is still running — kill it by hand")

    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1
    print("OK    live read, dirty detection and the naive-replace conversion all hold")
    return 0


def _pid_of(app: Any) -> int:
    """The pid behind an Office ``Application``, via its top-level window."""
    import win32process  # type: ignore[import-not-found]

    _, pid = win32process.GetWindowThreadProcessId(int(app.Hwnd))
    return int(pid)


if __name__ == "__main__":
    raise SystemExit(main())
