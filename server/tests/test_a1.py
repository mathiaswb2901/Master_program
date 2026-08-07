"""A1 notation helpers — the little the read bridge and its tool share."""

import pytest

from workbench_server.services.office_host.a1 import (
    cell_ref,
    column_index,
    column_letter,
    parse_cell,
    parse_range,
)


@pytest.mark.parametrize(
    ("index", "letters"),
    [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"), (701, "ZZ"), (702, "AAA")],
)
def test_column_letter_round_trips(index: int, letters: str) -> None:
    assert column_letter(index) == letters
    assert column_index(letters) == index


def test_cell_ref_and_parse_cell_round_trip() -> None:
    assert cell_ref(0, 0) == "A1"
    assert cell_ref(49, 7) == "H50"
    assert parse_cell("H50") == (49, 7)
    assert parse_cell(" a1 ") == (0, 0)


def test_parse_range_forms() -> None:
    assert parse_range("A1") == (0, 0, None, None)
    assert parse_range("A1:H50") == (0, 0, 49, 7)
    assert parse_range("A51:") == (50, 0, None, None)


@pytest.mark.parametrize("bad", ["", "1A", "A0-B2", "H50:A1", "AB", ":B2"])
def test_parse_range_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_range(bad)
