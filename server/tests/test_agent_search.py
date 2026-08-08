"""The workspace_search agent tool: the AXI shapes, and the byte budget.

The generic ceiling tests in ``test_agent_tools.py`` already pin this tool's
description and input schema (it is in ``ALL_AGENT_TOOLS``). What is here is the
tool *body*: that it says "none" out loud, states truncation and names how to
widen it, ends with a next step, and that even a pathological result stays inside
``max_result_bytes`` — the ceiling paid on every call.
"""

from pathlib import Path
from typing import Any

from workbench_server.models.search import SearchRequest, SearchResponse
from workbench_server.services.agent_tools import (
    WORKSPACE_SEARCH,
    WORKSPACE_SEARCH_MAX_HITS,
    handle_workspace_search,
)
from workbench_server.services.search import SearchService
from workbench_server.services.workspace import Workspace


def _text(result: dict[str, Any]) -> str:
    text: str = result["content"][0]["text"]
    return text


def _searcher(tmp_path: Path) -> SearchService:
    return SearchService(Workspace(tmp_path))


def test_a_missing_query_is_a_tool_error(tmp_path: Path) -> None:
    result = handle_workspace_search(_searcher(tmp_path), {})
    assert result.get("is_error") is True
    assert "query" in _text(result)


def test_no_matches_says_so_explicitly(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    result = handle_workspace_search(_searcher(tmp_path), {"query": "absent"})
    assert result.get("is_error") is None
    assert "No matches for 'absent'" in _text(result)


def test_hits_are_grouped_and_end_with_a_next_step(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "model.py").write_text("PRICE_AREA = 'SE3'\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("SE3 notes\n", encoding="utf-8")
    text = _text(handle_workspace_search(_searcher(tmp_path), {"query": "SE3"}))
    assert "src/model.py" in text
    assert "notes.md" in text
    assert "1: PRICE_AREA = 'SE3'" in text
    # AXI shape 3: a non-empty result ends by naming a file to open.
    assert "Open a file" in text


def test_truncation_names_how_to_widen(tmp_path: Path) -> None:
    body = "".join(f"needle {i}\n" for i in range(WORKSPACE_SEARCH_MAX_HITS + 20))
    (tmp_path / "many.txt").write_text(body, encoding="utf-8")
    text = _text(handle_workspace_search(_searcher(tmp_path), {"query": "needle"}))
    # AXI shape 1: capped, and it names the argument that widens the window.
    assert "max_results" in text
    assert "stopped at" in text


def test_case_sensitive_flag_reaches_the_service(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("MixedCase\n", encoding="utf-8")
    insensitive = _text(handle_workspace_search(_searcher(tmp_path), {"query": "mixedcase"}))
    assert "a.txt" in insensitive
    sensitive = _text(
        handle_workspace_search(_searcher(tmp_path), {"query": "mixedcase", "case_sensitive": True})
    )
    assert "No matches" in sensitive


class _FloodSearcher:
    """A searcher that returns a worst-case flood: the tool's own max hits, each a
    long line under a long path. The tool must still fit its byte budget."""

    def search(self, request: SearchRequest) -> SearchResponse:
        from workbench_server.models.search import FileMatches, SearchHit

        hits = [
            SearchHit(line=i, col=0, text="x" * 400, line_truncated=True)
            for i in range(1, WORKSPACE_SEARCH_MAX_HITS + 1)
        ]
        files = [FileMatches(path=("deep/" * 40) + "file.py", hits=hits)]
        return SearchResponse(
            query="x",
            files=files,
            total_hits=len(hits),
            files_with_matches=1,
            truncated=True,
        )


def test_a_pathological_result_stays_within_the_byte_budget() -> None:
    result = handle_workspace_search(_FloodSearcher(), {"query": "x"})
    assert len(_text(result).encode()) <= WORKSPACE_SEARCH.max_result_bytes


def test_the_representative_result_is_well_within_budget(tmp_path: Path) -> None:
    # A handful of hits across two files — the everyday call. Comfortably under.
    (tmp_path / "one.py").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("alpha here\n", encoding="utf-8")
    result = handle_workspace_search(_searcher(tmp_path), {"query": "alpha"})
    assert len(_text(result).encode()) <= WORKSPACE_SEARCH.max_result_bytes
