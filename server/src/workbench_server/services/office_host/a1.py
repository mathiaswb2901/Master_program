"""A1 spreadsheet notation — the little that the read bridge and its tool share.

Zero-based row/column indices internally; A1 strings on the edges. Kept in one
place so the fake bridge, the real COM reader and the ``office_read`` tool
formatter all agree on what ``A1:H50`` means and how ``AA`` is spelled.
"""

import re

_CELL = re.compile(r"^([A-Za-z]+)(\d+)$")


def column_letter(index: int) -> str:
    """Zero-based column index to its letters: 0 -> ``A``, 26 -> ``AA``."""
    if index < 0:
        raise ValueError(f"negative column index {index}")
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    """Column letters to a zero-based index: ``A`` -> 0, ``AA`` -> 26."""
    if not letters or not letters.isalpha():
        raise ValueError(f"not column letters: {letters!r}")
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def cell_ref(row: int, col: int) -> str:
    """Zero-based ``(row, col)`` to an A1 cell, e.g. ``(0, 0)`` -> ``A1``."""
    return f"{column_letter(col)}{row + 1}"


def parse_cell(text: str) -> tuple[int, int]:
    """An A1 cell to zero-based ``(row, col)``. Raises ``ValueError`` if malformed."""
    match = _CELL.match(text.strip())
    if match is None:
        raise ValueError(f"not an A1 cell: {text!r}")
    return int(match.group(2)) - 1, column_index(match.group(1))


def parse_range(text: str) -> tuple[int, int, int | None, int | None]:
    """An A1 range to ``(row1, col1, row2, col2)``, zero-based, inclusive.

    Accepts a single cell (``A1`` — the corner, open to the used-range end), a
    rectangle (``A1:H50``), or an open corner (``A51:`` — from there to the end).
    ``row2``/``col2`` are ``None`` when the range is open-ended. Raises
    ``ValueError`` on anything else, which the caller maps to ``range_invalid``.
    """
    text = text.strip()
    if ":" not in text:
        row1, col1 = parse_cell(text)
        return row1, col1, None, None
    start, end = text.split(":", 1)
    row1, col1 = parse_cell(start)
    if end.strip() == "":
        return row1, col1, None, None
    row2, col2 = parse_cell(end)
    if row2 < row1 or col2 < col1:
        raise ValueError(f"range end before start: {text!r}")
    return row1, col1, row2, col2
