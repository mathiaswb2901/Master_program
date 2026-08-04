"""shortcuts.md: parser + merge unit tests, and the live-reload pipeline end to end."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.shortcuts import ShortcutEntry, ShortcutsState
from workbench_server.services.shortcuts import merge_shortcuts, parse_shortcuts

FENCE = "```"


def _file(*blocks: str) -> str:
    return "# My shortcuts\n\nPreamble prose the parser ignores.\n\n" + "\n\n".join(blocks) + "\n"


def _block(name: str, meta: str, body: str) -> str:
    lines = [f"## {name}"]
    if meta:
        lines.append(meta)
    lines.extend(["", FENCE, body, FENCE])
    return "\n".join(lines)


# ---- parser ----------------------------------------------------------------


def test_parses_a_full_entry() -> None:
    text = _file(
        _block(
            "Status board",
            "type: shell\nkeys: Alt+G\ndetail: git status + branch",
            "git status -sb",
        )
    )
    parsed = parse_shortcuts(text, "workspace", "f.md")
    assert parsed.problems == []
    assert parsed.entries == [
        ShortcutEntry(
            name="Status board",
            kind="shell",
            body="git status -sb",
            keys="Alt+G",
            detail="git status + branch",
            source="workspace",
        )
    ]


def test_metadata_is_optional_and_defaults_to_shell() -> None:
    parsed = parse_shortcuts(_file(_block("Bare", "", "uv run pytest")), "workspace", "f.md")
    assert parsed.problems == []
    assert [(e.name, e.kind, e.keys, e.detail) for e in parsed.entries] == [
        ("Bare", "shell", None, None)
    ]


def test_prompt_fence_language_selects_the_prompt_kind() -> None:
    text = _file("## Review\n\n```prompt\nReview the diff.\nBe blunt.\n```")
    parsed = parse_shortcuts(text, "global", "f.md")
    assert parsed.problems == []
    assert parsed.entries[0].kind == "prompt"
    assert parsed.entries[0].body == "Review the diff.\nBe blunt."
    assert parsed.entries[0].source == "global"


def test_unknown_type_is_skipped_and_reported() -> None:
    text = _file(
        _block("Bad", "type: python", "print(1)"),
        _block("Good", "", "ls"),
    )
    parsed = parse_shortcuts(text, "workspace", "f.md")
    assert [e.name for e in parsed.entries] == ["Good"]
    assert len(parsed.problems) == 1
    assert "unknown type" in parsed.problems[0].message


def test_unterminated_fence_is_reported_and_earlier_entries_survive() -> None:
    text = _file(_block("Good", "", "ls")) + "\n## Broken\n\n```\ngit status\n"
    parsed = parse_shortcuts(text, "workspace", "f.md")
    assert [e.name for e in parsed.entries] == ["Good"]
    assert len(parsed.problems) == 1
    assert "unterminated" in parsed.problems[0].message


def test_entry_without_a_code_block_is_reported() -> None:
    parsed = parse_shortcuts("## Empty\n\ntype: shell\n", "workspace", "f.md")
    assert parsed.entries == []
    assert "no fenced code block" in parsed.problems[0].message


def test_multi_line_shell_body_is_refused() -> None:
    """A newline in a live PTY is an Enter press — inserting one would execute."""
    parsed = parse_shortcuts(_file(_block("Two", "", "cd repo\nrm -rf build")), "workspace", "f.md")
    assert parsed.entries == []
    assert "single line" in parsed.problems[0].message


def test_duplicate_name_in_one_file_keeps_the_first() -> None:
    text = _file(_block("Dup", "", "first"), _block("Dup", "", "second"))
    parsed = parse_shortcuts(text, "workspace", "f.md")
    assert [e.body for e in parsed.entries] == ["first"]
    assert "duplicate name" in parsed.problems[0].message


def test_chord_colliding_with_a_builtin_is_dropped_but_the_entry_survives() -> None:
    parsed = parse_shortcuts(_file(_block("Save", "keys: Ctrl+S", "ls")), "workspace", "f.md")
    assert [(e.name, e.keys) for e in parsed.entries] == [("Save", None)]
    assert "built-in shortcut" in parsed.problems[0].message


def test_chord_without_ctrl_or_alt_is_rejected() -> None:
    parsed = parse_shortcuts(_file(_block("Plain", "keys: G", "ls")), "workspace", "f.md")
    assert parsed.entries[0].keys is None
    assert "Ctrl or Alt" in parsed.problems[0].message


def test_second_entry_cannot_steal_a_chord() -> None:
    text = _file(_block("One", "keys: Alt+G", "ls"), _block("Two", "keys: Alt+G", "pwd"))
    parsed = parse_shortcuts(text, "workspace", "f.md")
    assert [(e.name, e.keys) for e in parsed.entries] == [("One", "Alt+G"), ("Two", None)]
    assert "already bound" in parsed.problems[0].message


# ---- merge -----------------------------------------------------------------


def test_workspace_entry_shadows_the_global_one_of_the_same_name() -> None:
    workspace = parse_shortcuts(_file(_block("Deploy", "", "just deploy")), "workspace", "ws.md")
    global_ = parse_shortcuts(
        _file(_block("Deploy", "", "old"), _block("Other", "", "ls")), "global", "gl.md"
    )
    state = merge_shortcuts(workspace, global_)
    assert [(e.name, e.body, e.source) for e in state.entries] == [
        ("Deploy", "just deploy", "workspace"),
        ("Other", "ls", "global"),
    ]
    assert state.problems == []  # shadowing is the documented rule, not an error


def test_global_chord_loses_to_the_workspace_chord() -> None:
    workspace = parse_shortcuts(_file(_block("A", "keys: Alt+G", "ls")), "workspace", "ws.md")
    global_ = parse_shortcuts(_file(_block("B", "keys: Alt+G", "pwd")), "global", "gl.md")
    state = merge_shortcuts(workspace, global_)
    assert [(e.name, e.keys) for e in state.entries] == [("A", "Alt+G"), ("B", None)]
    assert state.problems[0].file == "gl.md"


def test_payload_stays_compact() -> None:
    """Ergonomics budget: the whole state rides every /api/shortcuts response."""
    text = _file(
        _block("Status board", "keys: Alt+G\ndetail: git status + branch", "git status -sb"),
        _block("Run tests", "keys: Alt+Y\ndetail: full suite", "uv run pytest"),
        "## Review diff\n\ntype: prompt\n\n```\nReview the working diff for correctness.\n```",
    )
    state = merge_shortcuts(
        parse_shortcuts(text, "workspace", "ws.md"), parse_shortcuts("", "global", "gl.md")
    )
    assert len(state.entries) == 3
    assert len(state.model_dump_json()) < 900


# ---- service (real files, real watcher) ------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_api_serves_both_files_merged(
    settings: Settings, tmp_path: Path, global_shortcuts_file: Path
) -> None:
    _write(tmp_path / ".workbench" / "shortcuts.md", _file(_block("Local", "", "ls")))
    _write(global_shortcuts_file, _file(_block("Everywhere", "type: prompt", "Summarize.")))
    with TestClient(create_app(settings)) as client:
        state = ShortcutsState.model_validate(client.get("/api/shortcuts").json())
    assert [(e.name, e.source) for e in state.entries] == [
        ("Local", "workspace"),
        ("Everywhere", "global"),
    ]


@pytest.mark.timeout(60)
def test_editing_the_file_publishes_shortcuts_changed(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / ".workbench" / "shortcuts.md"
    _write(path, _file(_block("First", "", "ls")))
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        _write(path, _file(_block("First", "", "ls"), _block("Second", "keys: Alt+G", "pwd")))
        for _ in range(20):  # file_changed frames arrive first
            event = json.loads(ws.receive_text())
            if event["type"] == "shortcuts_changed":
                break
        else:  # pragma: no cover - the watcher pipeline is broken if this trips
            pytest.fail("no shortcuts_changed event")
        assert event["entry_count"] == 2
        assert event["problem_count"] == 0
        state = ShortcutsState.model_validate(client.get("/api/shortcuts").json())
    assert [(e.name, e.keys) for e in state.entries] == [("First", None), ("Second", "Alt+G")]
