"""The conversation browser: grouping, honesty, and what it refuses to read twice.

Every test here runs against a fixture transcript directory shaped like Claude
Code's real one — ``<projects>/<encoded-cwd>/<session-id>.jsonl`` — because the
encoding is the part that cannot be inverted and the part everything else hangs
off.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services import conversations as conversations_module
from workbench_server.services.conversations import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ConversationBrowser,
)
from workbench_server.services.session_index import (
    SessionIndex,
    encode_project_dir,
    scan_transcript,
)
from workbench_server.services.titles import derive_title

# ---- fixture transcripts -----------------------------------------------------


def user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def tool_result(output: str) -> dict[str, Any]:
    """A tool result — stored as a ``user`` record, and never a turn."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}],
        },
    }


def write_transcript(
    projects: Path, folder: Path, session_id: str, records: list[dict[str, Any]], mtime: float
) -> Path:
    """One transcript where Claude Code would have put it."""
    directory = projects / encode_project_dir(folder)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    return root


@pytest.fixture
def browser(projects: Path, workspace: Path) -> ConversationBrowser:
    return ConversationBrowser(projects, workspace)


# ---- grouping and ordering ---------------------------------------------------


def test_groups_conversations_by_folder_newest_first(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace, "old-root", [user("the oldest thing")], 1_000.0)
    write_transcript(projects, workspace, "new-root", [user("the newest thing")], 3_000.0)
    write_transcript(projects, workspace / "src", "mid", [user("a src thing")], 2_000.0)

    store = browser.browse()

    assert [group.workspace_relative for group in store.projects] == ["", "src"]
    root = store.projects[0]
    assert [c.session_id for c in root.conversations] == ["new-root", "old-root"]
    assert store.total_projects == 2
    assert store.total_conversations == 3
    assert store.returned_conversations == 3


def test_orders_folders_by_their_newest_conversation(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace, "root", [user("root")], 1_000.0)
    write_transcript(projects, workspace / "src", "src", [user("src")], 9_000.0)

    store = browser.browse()

    assert [group.workspace_relative for group in store.projects] == ["src", ""]


# ---- the title is the live path's title --------------------------------------


def test_title_matches_the_live_sessions_derivation(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """PR #16's rule: a conversation must not visibly retitle when it goes to disk.

    Asserted against both other producers of a title — ``derive_title`` itself
    and the per-folder listing ``GET /api/agents/sessions`` renders — because
    the failure mode is two of the three agreeing.
    """
    first = "   Fix   the DST bug in the   SE3 settlement window\n\nplease  "
    write_transcript(projects, workspace, "sess-1", [user(first), assistant("ok")], 1_000.0)

    row = browser.browse().projects[0].conversations[0]
    from_index = SessionIndex(projects).list_sessions(workspace, "")[0]

    assert row.title == derive_title(first)
    assert row.title == from_index.title
    assert row.title == "Fix the DST bug in the SE3 settlement window please"


def test_long_titles_are_truncated_exactly_as_live_ones_are(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    first = "word " * 60
    write_transcript(projects, workspace, "sess-1", [user(first)], 1_000.0)
    assert browser.browse().projects[0].conversations[0].title == derive_title(first)


# ---- turns -------------------------------------------------------------------


def test_counts_what_you_said_and_not_tool_results(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """Claude Code stores tool results as ``user`` records; they are not turns."""
    write_transcript(
        projects,
        workspace,
        "sess-1",
        [
            user("first"),
            assistant("thinking"),
            tool_result("500 lines of grep output"),
            assistant("done"),
            user("second"),
            tool_result("more output"),
        ],
        1_000.0,
    )

    row = browser.browse().projects[0].conversations[0]
    assert row.turns == 2
    assert row.turns_capped is False


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": ")])
def test_counts_turns_whether_the_writer_emitted_compact_or_spaced_json(
    tmp_path: Path, separators: tuple[str, str]
) -> None:
    """The pre-filter that makes a full pass cheap may not change the answer.

    Found by this test rather than in production: a filter matching only
    ``"type":"user"`` skips every record a writer spaced out, and a conversation
    with twelve turns then reports zero and calls itself unreadable.
    """
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text(
        "".join(
            json.dumps(record, separators=separators) + "\n"
            for record in [user("one"), assistant("hi"), user("two")]
        ),
        encoding="utf-8",
    )
    facts = scan_transcript(transcript)
    assert facts.turns == 2
    assert facts.first_user_text == "one"


def test_turn_count_is_a_floor_when_the_byte_budget_is_hit(tmp_path: Path) -> None:
    transcript = tmp_path / "big.jsonl"
    transcript.write_text(
        "".join(json.dumps(user(f"message {i}")) + "\n" for i in range(50)), encoding="utf-8"
    )
    facts = scan_transcript(transcript, byte_budget=200)
    assert facts.truncated is True
    assert 0 < facts.turns < 50
    assert facts.first_user_text == "message 0"


# ---- what it cannot read -----------------------------------------------------


def test_a_truncated_transcript_keeps_its_row(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """A half-flushed last line is the normal state of a transcript being written."""
    path = write_transcript(projects, workspace, "sess-1", [user("hello"), user("wor")], 1_000.0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"user","message":{"role":"user","content":"cut off her')

    row = browser.browse().projects[0].conversations[0]
    assert row.title == "hello"
    assert row.turns == 2  # the two whole records; the half line is skipped
    assert row.problem is None


def test_a_transcript_with_nothing_readable_says_so_and_is_still_listed(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    directory = projects / encode_project_dir(workspace)
    directory.mkdir(parents=True)
    (directory / "broken.jsonl").write_text("}}}not json at all\n\x00\x00\n", encoding="utf-8")

    store = browser.browse()

    row = store.projects[0].conversations[0]
    assert row.session_id == "broken"
    assert row.turns == 0
    assert row.problem is not None
    assert "damaged" in row.problem or "empty" in row.problem


def test_an_empty_transcript_is_listed_with_a_reason(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    directory = projects / encode_project_dir(workspace)
    directory.mkdir(parents=True)
    (directory / "empty.jsonl").write_text("", encoding="utf-8")

    row = browser.browse().projects[0].conversations[0]
    assert row.problem is not None
    assert row.title == "(unreadable transcript)"


def test_an_empty_store_says_it_is_empty_and_where_it_looked(
    projects: Path, workspace: Path
) -> None:
    missing = projects / "never-created"
    store = ConversationBrowser(missing, workspace).browse()

    assert store.projects == []
    assert store.total_conversations == 0
    assert store.root_exists is False
    assert store.projects_root == str(missing)


def test_a_store_that_exists_but_holds_nothing_is_not_a_missing_store(
    browser: ConversationBrowser, projects: Path
) -> None:
    store = browser.browse()
    assert store.projects == []
    assert store.root_exists is True


def test_subagent_transcripts_are_not_rows_of_their_own(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """They belong to a conversation that is already listed."""
    write_transcript(projects, workspace, "sess-1", [user("parent")], 1_000.0)
    nested = projects / encode_project_dir(workspace) / "sess-1" / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-1.jsonl").write_text(json.dumps(user("child")) + "\n", encoding="utf-8")

    store = browser.browse()
    assert store.total_conversations == 1
    assert [c.session_id for c in store.projects[0].conversations] == ["sess-1"]


# ---- the jail: half A shows everything and opens only what it may ------------


def test_a_folder_inside_the_workspace_is_openable(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace / "src", "sess-1", [user("hi")], 1_000.0)

    group = browser.browse().projects[0]
    assert group.resolved is True
    assert group.workspace_relative == "src"
    assert group.openable is True
    assert group.reason is None
    assert group.label == str(workspace / "src")


def test_a_folder_outside_the_workspace_is_shown_and_refused_with_a_reason(
    browser: ConversationBrowser, projects: Path, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    write_transcript(projects, outside, "sess-1", [user("another project")], 1_000.0)

    group = browser.browse().projects[0]
    assert group.resolved is True  # it exists, so it gets a real name
    assert group.folder == str(outside)
    assert group.workspace_relative is None
    assert group.openable is False
    assert group.reason is not None
    assert "Outside this workspace" in group.reason
    # …and the conversation itself is still there to be seen.
    assert [c.title for c in group.conversations] == ["another project"]


def test_an_unmatchable_key_shows_the_key_itself_and_never_a_guess(
    browser: ConversationBrowser, projects: Path
) -> None:
    key = "C--Users-nobody-a-project-that-was-deleted"
    (projects / key).mkdir()
    (projects / key / "sess-1.jsonl").write_text(json.dumps(user("gone")) + "\n", encoding="utf-8")

    group = browser.browse().projects[0]
    assert group.resolved is False
    assert group.folder is None
    assert group.label == key
    assert group.openable is False
    assert group.reason is not None
    assert "moved or deleted" in group.reason


def test_the_workspace_root_itself_resolves(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace, "sess-1", [user("at the root")], 1_000.0)
    group = browser.browse().projects[0]
    assert group.workspace_relative == ""
    assert group.openable is True


def test_a_deeply_nested_folder_resolves_through_the_workspace(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    deep = workspace / "src" / "models" / "day-ahead"
    deep.mkdir(parents=True)
    write_transcript(projects, deep, "sess-1", [user("deep")], 1_000.0)

    group = browser.browse().projects[0]
    assert group.workspace_relative == "src/models/day-ahead"
    assert group.openable is True


def test_a_hyphenated_segment_still_resolves_despite_the_lossy_encoding(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """``a-b/c`` and ``a/b-c`` encode identically; the match is against real dirs."""
    real = workspace / "price-area" / "se3"
    real.mkdir(parents=True)
    write_transcript(projects, real, "sess-1", [user("se3")], 1_000.0)

    group = browser.browse().projects[0]
    assert group.folder == str(real)
    assert group.workspace_relative == "price-area/se3"


def test_resolution_survives_the_folder_being_deleted_between_scans(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    folder = workspace / "temporary"
    folder.mkdir()
    write_transcript(projects, folder, "sess-1", [user("hi")], 1_000.0)
    assert browser.browse().projects[0].resolved is True

    folder.rmdir()
    group = browser.browse().projects[0]
    assert group.resolved is False
    assert group.openable is False


# ---- live sessions -----------------------------------------------------------


def test_a_conversation_already_running_names_the_session_continuing_it(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace, "sdk-1", [user("resumed")], 1_000.0)
    write_transcript(projects, workspace, "sdk-2", [user("not resumed")], 900.0)

    store = browser.browse(live_by_sdk_id={"sdk-1": "local-abc"})

    rows = {row.session_id: row.live_session_id for row in store.projects[0].conversations}
    assert rows == {"sdk-1": "local-abc", "sdk-2": None}


# ---- the cost ----------------------------------------------------------------


def test_an_unchanged_transcript_is_never_read_twice(
    browser: ConversationBrowser,
    projects: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for i in range(5):
        write_transcript(projects, workspace, f"sess-{i}", [user(f"m{i}")], 1_000.0 + i)

    reads: list[Path] = []

    def counted(path: Path, byte_budget: int = 0, **kwargs: object) -> Any:
        reads.append(path)
        return scan_transcript(path)

    monkeypatch.setattr(conversations_module, "scan_transcript", counted)

    browser.browse()
    assert len(reads) == 5
    browser.browse()
    assert len(reads) == 5, "a second browse of an unchanged store read a transcript again"


def test_an_appended_transcript_is_read_again(
    browser: ConversationBrowser,
    projects: Path,
    workspace: Path,
) -> None:
    path = write_transcript(projects, workspace, "sess-1", [user("one")], 1_000.0)
    assert browser.browse().projects[0].conversations[0].turns == 1

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(user("two")) + "\n")
    os.utime(path, (2_000.0, 2_000.0))

    row = browser.browse().projects[0].conversations[0]
    assert row.turns == 2
    assert row.updated_at == pytest.approx(2_000.0, abs=0.01)


def test_the_cache_forgets_transcripts_that_are_gone(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    path = write_transcript(projects, workspace, "sess-1", [user("one")], 1_000.0)
    browser.browse()
    # Reaching into the private cache is the point: the leak it would otherwise
    # be is invisible from outside, and a long-running server's memory quietly
    # remembering deleted conversations is exactly the kind of thing nobody
    # notices until it matters.
    assert len(browser._facts) == 1
    path.unlink()
    browser.browse()
    assert browser._facts == {}


def test_the_limit_bounds_the_reading_and_says_what_it_withheld(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    for i in range(10):
        write_transcript(projects, workspace, f"sess-{i}", [user(f"m{i}")], 1_000.0 + i)

    store = browser.browse(limit=3)

    assert store.total_conversations == 10
    assert store.returned_conversations == 3
    assert store.limit == 3
    # The newest three, not the first three the filesystem handed back.
    assert [c.session_id for c in store.projects[0].conversations] == [
        "sess-9",
        "sess-8",
        "sess-7",
    ]


def test_browsing_writes_nothing_to_somebody_elses_storage(
    browser: ConversationBrowser, projects: Path, workspace: Path
) -> None:
    """The one property this feature must never break."""
    write_transcript(projects, workspace, "sess-1", [user("hello"), assistant("hi")], 1_000.0)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in projects.rglob("*")
        if path.is_file()
    }

    browser.browse()
    browser.browse(limit=1)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in projects.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert sorted(p.name for p in projects.rglob("*")) == [
        encode_project_dir(workspace),
        "sess-1.jsonl",
    ]


# ---- the budget --------------------------------------------------------------


class _ScandirCounter:
    """Counts directory listings under one root, the way test_perf_budgets does.

    Work-shaped rather than wall-clock: 26 listings is 26 listings on a laptop
    and on a throttled runner, and when the number moves, the code moved.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self.listings: list[Path] = []
        self._scandir = os.scandir

    @property
    def count(self) -> int:
        return len(self.listings)

    def __enter__(self) -> "_ScandirCounter":
        outer = self

        def scandir(path: Any = ".") -> Any:
            try:
                resolved = Path(os.fspath(path)).resolve()
            except (TypeError, ValueError, OSError):
                resolved = None
            if resolved is not None and (
                resolved == outer._root or outer._root in resolved.parents
            ):
                outer.listings.append(resolved)
            return outer._scandir(path)

        os.scandir = scandir
        return self

    def __exit__(self, *_: object) -> None:
        os.scandir = self._scandir


def test_the_scan_costs_one_listing_per_project_and_no_reread(
    browser: ConversationBrowser,
    projects: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget against a realistically sized store: 25 folders, 200 conversations.

    Measured on the author's *real* store (17 project folders, 80 conversations,
    397 MB) on 2026-08-06: enumeration **2.3 ms**, a cold browse **1.26 s** (every
    transcript read once), a warm browse **82 ms** (nothing read). The counts
    below are what those milliseconds are made of.
    """
    folders = [workspace / f"project-{i:02d}" for i in range(25)]
    for i, folder in enumerate(folders):
        folder.mkdir(parents=True, exist_ok=True)
        for j in range(8):
            write_transcript(projects, folder, f"s{i:02d}-{j}", [user(f"m{j}")], 1_000.0 + j)

    reads: list[Path] = []

    def counted(path: Path, byte_budget: int = 0, **kwargs: object) -> Any:
        reads.append(path)
        return scan_transcript(path)

    monkeypatch.setattr(conversations_module, "scan_transcript", counted)

    with _ScandirCounter(projects) as counter:
        store = browser.browse(limit=MAX_LIMIT)
    # One listing of the store, one per project directory. Nothing walks a
    # transcript's neighbours, and nothing lists a project twice.
    assert counter.count == 26, [str(p) for p in counter.listings]
    assert store.total_conversations == 200
    assert len(reads) == 200

    reads.clear()
    with _ScandirCounter(projects) as second:
        browser.browse(limit=MAX_LIMIT)
    assert second.count == 26
    assert reads == [], "a warm browse read a transcript that had not changed"

    # And the limit really does bound the expensive half.
    reads.clear()
    browser.browse(limit=10)
    assert reads == []
    # Force the cold path back: the cache is what this budget is about.
    browser._facts.clear()
    browser.browse(limit=10)
    assert len(reads) == 10


# ---- through the real app ----------------------------------------------------


@pytest.fixture
async def app_client(tmp_path: Path, projects: Path, workspace: Path):  # type: ignore[no-untyped-def]
    settings = Settings(workspace_root=workspace, claude_projects_dir=projects)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_the_endpoint_answers_with_groups(
    app_client: AsyncClient, projects: Path, workspace: Path
) -> None:
    write_transcript(projects, workspace, "sess-1", [user("in the workspace")], 2_000.0)
    write_transcript(projects, Path("/nowhere/at/all"), "sess-2", [user("somewhere else")], 1_000.0)

    response = await app_client.get("/api/conversations")

    assert response.status_code == 200
    body = response.json()
    assert body["total_conversations"] == 2
    assert body["limit"] == DEFAULT_LIMIT
    assert body["root_exists"] is True
    inside = next(g for g in body["projects"] if g["openable"])
    assert inside["workspace_relative"] == ""
    assert inside["conversations"][0]["title"] == "in the workspace"
    outside = next(g for g in body["projects"] if not g["openable"])
    assert outside["reason"]


async def test_the_endpoint_refuses_an_unbounded_limit(app_client: AsyncClient) -> None:
    assert (await app_client.get(f"/api/conversations?limit={MAX_LIMIT + 1}")).status_code == 422
    assert (await app_client.get("/api/conversations?limit=0")).status_code == 422


async def test_the_endpoint_is_empty_and_honest_on_a_machine_with_no_history(
    tmp_path: Path, workspace: Path
) -> None:
    settings = Settings(workspace_root=workspace, claude_projects_dir=tmp_path / "no-such-dir")
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/conversations")).json()
    assert body["projects"] == []
    assert body["root_exists"] is False
    assert body["projects_root"].endswith("no-such-dir")
