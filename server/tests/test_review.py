"""The adversarial review — M6 staged review, PR 2.

Fake-first, and the split follows ``test_gates.py``. Everything about
*judgement* — what a set of findings becomes, what a refusal says, what happens
when the reviewer never answers — runs against a scripted session factory, so it
is deterministic and needs no Claude login. Everything about **the diff** runs
against **real git and a real working tree**, because the one claim that cannot
be faked is what ``git`` was asked and what came back: a stub that answers
whatever a test handed it can only show the plumbing.

Two claims carry this file, and both are load-bearing for the milestone:

**The diff includes untracked files.** ``git diff`` cannot see a file git has
never heard of, so an agent that wrote three brand-new modules would review as
having changed nothing — a *silent green*, arriving through the check written to
stop it. :class:`TestTheDiffSeesNewFiles` drives that against a real repository
with a real untracked file, and it is the regression test the plan asked for by
name.

**The check never approves.** Asserted two ways, the ``test_orchestrator.py``
precedent: a ``must_fix`` review leaves ``result.approval is None`` at ``high``
risk — i.e. still awaiting a human — *and* the module is asserted to reference no
approval API at all, read off its AST rather than its prose.
"""

import ast
import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from workbench_server.config import Settings
from workbench_server.models.gates import SlotRef
from workbench_server.models.review import (
    MAX_FILE_DIFF_BYTES,
    MAX_FINDINGS,
    SEVERITY_ORDER,
    SEVERITY_OUTCOME,
    ReportFindingsRequest,
    ReviewFinding,
    ReviewReport,
    ReviewSpec,
)
from workbench_server.models.validation import ValidationSpec, ValidationSubject
from workbench_server.services.agent_sessions import SessionManager, TooManySessionsError
from workbench_server.services.event_bus import EventBus
from workbench_server.services.fake_agent import CLEAN_REVIEW_TRIGGER, fake_client_factory
from workbench_server.services.review import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    DIFF_MARKER,
    SETTING_MAX_BUDGET,
    SETTING_MAX_SESSIONS,
    SETTING_MAX_TURNS,
    SETTING_TIMEOUT,
    AdversarialReviewCheck,
    build_diff,
    review_prompt,
    split_sections,
)
from workbench_server.services.validation import (
    ValidationContext,
    ValidationService,
    derive_risk,
)
from workbench_server.services.worktrees import run_git

REVIEW_SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "workbench_server" / "services" / "review.py"
).read_text(encoding="utf-8")
REVIEW_AST = ast.parse(REVIEW_SOURCE)

#: Every name and attribute the review module actually *references*. Read off the
#: AST rather than the source text, because the module documents at length the
#: things it refuses to do — a substring scan would fail on its own prose and
#: would start passing the day that prose was deleted.
REVIEW_REFERENCED: set[str] = {
    node.id for node in ast.walk(REVIEW_AST) if isinstance(node, ast.Name)
} | {node.attr for node in ast.walk(REVIEW_AST) if isinstance(node, ast.Attribute)}


# --------------------------------------------------------------------------- helpers


def git(root: Path, *args: str) -> str:
    """One git command in ``root``. The arguments are this file's own literals."""
    done = subprocess.run(  # noqa: S603 - the arguments are this file's own literals
        ["git", *args],  # noqa: S607 - git is on PATH wherever this suite runs
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def git_repo(root: Path) -> str:
    """A real repository with one commit. Returns its HEAD.

    Real git, because the one thing a scripted ``GitRunner`` cannot show is what
    git was actually asked and what came back — which is the whole of the
    untracked-files claim below.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    git(root, "config", "commit.gpgsign", "false")
    (root / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD")


class _Locator:
    """A ``SlotLocator`` answering with one slot (or none)."""

    def __init__(self, ref: SlotRef | None) -> None:
        self._ref = ref

    def slot_of(self, session_id: str) -> SlotRef | None:
        return self._ref


class _Session:
    """A reviewer ``AgentSession`` stand-in driven by a script.

    ``script`` is called with this session's id when the check sends its prompt,
    and is what decides whether findings arrive, whether the turn merely ends, or
    whether nothing happens at all (the wall-clock case).
    """

    def __init__(self, session_id: str, script: Any, log: list[str]) -> None:
        self.session_id = session_id
        self._script = script
        self._queues: list[Any] = []
        #: Shared with the manager, because a reviewer is *reaped* on the way out
        #: — a test reading prompts off the live session map would find it empty.
        self.prompts = log
        self.closed = False

    def subscribe(self) -> Any:
        import asyncio

        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: Any) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def emit(self, event: Any) -> None:
        for queue in self._queues:
            queue.put_nowait(event)

    def send_user_message(self, text: str) -> None:
        self.prompts.append(text)
        self._script(self, text)

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _TurnDone:
    type = "turn_done"
    is_error = False


class _AgentError:
    type = "agent_error"

    def __init__(self, message: str) -> None:
        self.message = message


class _Sessions:
    """A ``SessionManager`` stand-in that hands back scripted reviewer sessions."""

    def __init__(self, script: Any, *, cap: int | None = None) -> None:
        self._script = script
        self._cap = cap
        self.sessions: dict[str, Any] = {}
        self.created: list[tuple[Path, str, str]] = []
        self.prompts: list[str] = []
        self.max_concurrent = 4

    def active_count(self) -> int:
        return len(self.sessions)

    def create_at(
        self,
        folder: Path,
        folder_label: str,
        resume_session_id: str | None = None,
        kind: str = "chat",
    ) -> Any:
        if self._cap is not None and len(self.created) >= self._cap:
            raise TooManySessionsError(self.max_concurrent)
        self.created.append((folder, folder_label, kind))
        session = _Session(f"rev-{len(self.created)}", self._script, self.prompts)
        self.sessions[session.session_id] = session
        return session


def reports(findings: list[dict[str, Any]], note: str = "") -> Any:
    """A script that files ``findings`` through the real receiver, then ends."""

    def script(session: _Session, prompt: str) -> None:
        check = script.check  # type: ignore[attr-defined]
        check.receive_findings(
            session.session_id,
            ReportFindingsRequest.model_validate({"findings": findings, "note": note}),
        )
        session.emit(_TurnDone())

    return script


def silent(message: str | None = None) -> Any:
    """A script whose turn ends without ever calling ``report_findings``."""

    def script(session: _Session, prompt: str) -> None:
        if message is not None:
            session.emit(_AgentError(message))
        session.emit(_TurnDone())

    return script


def never_answers() -> Any:
    """A script that does nothing at all — the wall-clock case."""

    def script(session: _Session, prompt: str) -> None:
        return None

    return script


MUST_FIX = {
    "severity": "must_fix",
    "file": "server/x.py",
    "line": 12,
    "claim": "Gate closure is compared in local time.",
    "refutation": "At 02:30 on the DST spring-forward date the naive stamp lands an "
    "hour early and the bid misses the window.",
    "confidence": "likely",
}
NIT = {
    "severity": "nit",
    "claim": "No units in the docstring.",
    "refutation": "Nothing breaks; a reader must open the caller to learn it is MWh.",
}


async def run_check(
    check: AdversarialReviewCheck,
    root: Path,
    *,
    params: dict[str, Any] | None = None,
    session_ref: str = "sess-1",
) -> list[Any]:
    ctx = ValidationContext(
        subject=ValidationSubject(kind="session_output", ref=session_ref, label="the work"),
        root=root,
        params=params or {},
    )
    return await check.run(ctx)


def build(
    slot: SlotRef | None,
    script: Any,
    *,
    root: Path | None = None,
    cap: int | None = None,
    timeout_s: float = 5.0,
) -> tuple[AdversarialReviewCheck, _Sessions]:
    check = AdversarialReviewCheck(_Locator(slot), timeout_s=timeout_s)
    sessions = _Sessions(script, cap=cap)
    check.bind(sessions)  # type: ignore[arg-type]
    script.check = check
    return check, sessions


# --------------------------------------------------------------------------- the diff


class TestTheDiffSeesNewFiles:
    """Real git, real working tree. The regression test for the silent green."""

    async def test_an_untracked_file_is_in_the_diff(self, tmp_path: Path) -> None:
        """The trap the plan named: an agent that wrote three brand-new modules
        has an empty ``git diff``, and a review of nothing that reports nothing
        is a green light nobody earned."""
        base = git_repo(tmp_path)
        (tmp_path / "brand_new.py").write_text("def added():\n    return 42\n", encoding="utf-8")
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "brand_new.py" in diff.text
        assert "def added():" in diff.text
        assert diff.files_total == 1
        # And the plain ``git diff`` really is empty, which is what makes the
        # assertion above a fix rather than a coincidence.
        plain = await asyncio.to_thread(git, tmp_path, "diff", base)
        assert plain == ""

    async def test_tracked_and_untracked_arrive_together(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
        (tmp_path / "brand_new.py").write_text("NEW = True\n", encoding="utf-8")
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert diff.files_total == 2
        assert "existing.py" in diff.text
        assert "brand_new.py" in diff.text

    async def test_ignored_files_stay_out(self, tmp_path: Path) -> None:
        """``--exclude-standard``: a slot's own ``.venv`` is not the change."""
        base = git_repo(tmp_path)
        (tmp_path / ".gitignore").write_text("junk/\n", encoding="utf-8")
        (tmp_path / "junk").mkdir()
        (tmp_path / "junk" / "big.bin").write_text("noise\n", encoding="utf-8")
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "junk/big.bin" not in diff.text

    async def test_a_clean_tree_reviews_as_nothing_to_review(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert diff.files_total == 0

    async def test_a_binary_new_file_is_named_not_pasted(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 400)
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "blob.bin" in diff.text
        assert "new binary file" in diff.text

    async def test_invalid_bytes_without_a_nul_are_still_binary(self, tmp_path: Path) -> None:
        """The classification survives the fix. Binary is now decided by a NUL
        byte first, so the case that has none — and is caught only by the decode
        failing — has to be shown still working."""
        base = git_repo(tmp_path)
        (tmp_path / "latin.bin").write_bytes(b"\xff\xfe\xff\xfe" * 200)
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "new binary file" in diff.text

    async def test_git_that_will_not_answer_is_none_never_clean(self, tmp_path: Path) -> None:
        """``None`` is not "no changes" and a caller must never read it as one:
        a checkout whose state git will not report is one no verdict may be
        attributed to."""
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert await build_diff(run_git, plain, "") is None


#: A new file that is *ordinary text*, longer than the per-file cap, and whose
#: character at the cut is multi-byte. Built in bytes rather than written through
#: text mode on purpose: Windows would translate the ``\n`` and move the boundary
#: this whole fixture exists to land on.
STRADDLE_MARKER = "# STRADDLE_MARKER — an em dash, three bytes\n"


def straddling_file(target: Path, cap: int = MAX_FILE_DIFF_BYTES) -> None:
    """Write ``target`` so its ``cap``-th byte is *inside* an em dash.

    An em dash (U+2014) is three bytes, and this codebase writes them by the
    hundred. Placing one so it begins at ``cap - 1`` means every byte-counted
    read near the cap — whether it stops at ``cap`` or at ``cap + 1`` — lands
    between two of its bytes, which is the condition a strict ``decode`` cannot
    survive and a truncated *source file* has no business being punished for.
    """
    lead = STRADDLE_MARKER.encode()
    filler = b"a" * (cap - 1 - len(lead))
    target.write_bytes(lead + filler + "—".encode() + b"z" * 5_000)


class TestABigTextFileIsTruncatedNotCalledBinary:
    """The regression test for a silent green wearing the wrong label.

    A new file read up to a **byte** ceiling is almost never cut on a character
    boundary. While the cut bytes were handed to a strict ``decode``, any new
    module with a multi-byte character near the cap raised ``UnicodeDecodeError``
    and came out of the diff as ``(new binary file, not shown)`` — so the
    reviewer saw *none* of a perfectly reviewable file and the report described
    the omission as binary rather than as truncation. A bug in that file was then
    unreviewable by construction, which is the exact failure this check exists to
    refuse.

    Real git, a real file on disk, and the **production** cap, because the bug
    lives in the arithmetic between the cap and the read.
    """

    async def test_the_text_is_shown_and_the_cut_is_named(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        straddling_file(tmp_path / "brand_new.py")
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "new binary file" not in diff.text
        assert "STRADDLE_MARKER" in diff.text
        # …and the omission reads as what it is, with a size (AXI shape 1).
        assert "more bytes of this file were not shown" in diff.text

    async def test_the_stated_remainder_is_the_files_own(self, tmp_path: Path) -> None:
        """The count is taken from ``stat``, not from the byte the read overshot
        by. Reading ``cap + 1`` and reporting the difference would say "1 more
        byte" of a 2 GB artifact — AXI shape 1 violated with a number attached,
        which is worse than no number."""
        base = git_repo(tmp_path)
        straddling_file(tmp_path / "brand_new.py")
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        match = re.search(r"([\d,]+) more bytes of this file were not shown", diff.text)
        assert match is not None
        # The fixture puts 5,000 bytes past the cap; the read alone could only
        # ever have known about a hundred or so of them.
        assert int(match.group(1).replace(",", "")) >= 5_000

    async def test_the_reviewer_is_actually_shown_it(self, tmp_path: Path) -> None:
        """End to end, the way a user hits it: a worker writes a new module into
        its slot and a review is run. What is asserted is the **prompt the
        reviewer was sent** — the only place "the reviewer never saw this file"
        can be observed."""
        base = git_repo(tmp_path)
        straddling_file(tmp_path / "brand_new.py")
        check, sessions = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "pass"
        prompt = sessions.prompts[0]
        assert "STRADDLE_MARKER" in prompt
        assert "new binary file" not in prompt

    async def test_a_short_multibyte_file_is_untouched(self, tmp_path: Path) -> None:
        """The control: nothing about the fix depends on the file being large,
        and a small file must still arrive whole and unclipped."""
        base = git_repo(tmp_path)
        (tmp_path / "small.py").write_bytes("VALUE = '— dash —'\n".encode())
        diff = await build_diff(run_git, tmp_path, base)
        assert diff is not None
        assert "— dash —" in diff.text
        assert "more bytes of this file were not shown" not in diff.text


class TestTheDiffIsBounded:
    async def test_one_huge_file_cannot_crowd_out_the_rest(self, tmp_path: Path) -> None:
        """The per-file cap's whole job. Without it a regenerated lockfile eats
        the window and the twelve source files that changed are never shown."""
        base = git_repo(tmp_path)
        (tmp_path / "lock.txt").write_text("x" * 50_000, encoding="utf-8")
        (tmp_path / "small.py").write_text("REAL = 1\n", encoding="utf-8")
        diff = await build_diff(run_git, tmp_path, base, max_bytes=20_000, per_file_bytes=4_000)
        assert diff is not None
        assert "small.py" in diff.text
        assert "more bytes of this file were not shown" in diff.text

    async def test_truncation_states_its_size_and_reaches_the_prompt(self, tmp_path: Path) -> None:
        """AXI shape 1, and it is stated **to the reviewer** — a reviewer that
        does not know it was shown a slice reports absence as evidence of
        absence."""
        base = git_repo(tmp_path)
        for index in range(6):
            (tmp_path / f"file{index}.py").write_text("y" * 3_000, encoding="utf-8")
        diff = await build_diff(run_git, tmp_path, base, max_bytes=6_000, per_file_bytes=3_500)
        assert diff is not None
        assert diff.truncated is not None
        assert diff.files_shown < diff.files_total
        assert "of 6 changed files shown" in diff.truncated.detail
        prompt = review_prompt(diff, "")
        assert diff.truncated.detail in prompt
        assert "you are being shown a slice" in prompt

    def test_sections_split_per_file(self) -> None:
        text = "diff --git a/one.py b/one.py\n@@\n+a\ndiff --git a/two.py b/two.py\n@@\n+b\n"
        assert [s.path for s in split_sections(text)] == ["one.py", "two.py"]


class TestTheBrief:
    def test_it_is_refute_first_and_says_a_bare_claim_is_a_nit(self) -> None:
        prompt = review_prompt(_diff_stub(), "watch the settlement period boundary")
        assert "REFUTE" in prompt
        assert "nit" in prompt
        assert "report_findings" in prompt
        # The caller's focus is appended to a server-owned brief, never replacing it.
        assert "watch the settlement period boundary" in prompt

    def test_the_focus_is_bounded(self) -> None:
        """It is a sentence handed to a read-only session, not an argv — but it
        is still the one field a caller fills, so it is bounded."""
        with pytest.raises(ValidationError):
            ReviewSpec(focus="x" * 5_000)


def _diff_stub() -> Any:
    from workbench_server.services.review import _Diff

    return _Diff(
        text="diff --git a/x.py b/x.py\n+1\n",
        files_shown=1,
        files_total=1,
        bytes_shown=30,
        bytes_total=30,
        head="a" * 40,
        base="b" * 40,
    )


# --------------------------------------------------------------------------- evidence


class TestFindingsBecomeEvidence:
    async def test_the_worst_severity_decides_the_outcome(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(
            SlotRef(slot="s1", path=str(tmp_path), base=base), reports([NIT, MUST_FIX])
        )
        evidence = await run_check(check, tmp_path)
        assert len(evidence) == 1
        assert evidence[0].kind == "diff"
        assert evidence[0].outcome == "fail"
        assert "2 finding(s)" in evidence[0].detail
        assert "1 must_fix" in evidence[0].detail
        # And that is one *grouped* line, not one per finding — the detail lives
        # in the payload the gallery's expander redeems.
        assert evidence[0].payload_ref is not None
        assert derive_risk(evidence) == "high"

    async def test_only_nits_is_a_pass(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([NIT]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "pass"
        assert derive_risk(evidence) == "pass"

    async def test_no_findings_says_so_explicitly(self, tmp_path: Path) -> None:
        """AXI shape 2. A review that found nothing and a review that never ran
        must never look alike, which is the whole reason this line has words in
        it instead of being an absence."""
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "pass"
        assert "No findings" in evidence[0].detail
        assert "reviewed" in evidence[0].detail

    async def test_the_payload_carries_the_findings(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([MUST_FIX]))
        service = ValidationService(tmp_path, EventBus())
        service.register(check)
        result = await service.run(
            ValidationSpec(
                subject=ValidationSubject(kind="session_output", ref="sess-1", label="w"),
                checks=["review"],
            )
        )
        ref = result.evidence[0].payload_ref
        assert ref is not None
        payload = service.payload("diff", ref)
        assert isinstance(payload, ReviewReport)
        assert payload.findings[0].claim == MUST_FIX["claim"]
        assert payload.findings[0].refutation == MUST_FIX["refutation"]
        assert payload.reviewer_session_id == "rev-1"

    async def test_the_reviewer_note_reaches_the_line(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(
            SlotRef(slot="s1", path=str(tmp_path), base=base),
            reports([], note="I could not read the migration."),
        )
        evidence = await run_check(check, tmp_path)
        assert "could not read the migration" in evidence[0].detail

    def test_every_severity_maps_to_an_outcome(self) -> None:
        """A severity with no entry would raise inside the check, which the
        service reports as *the check failing* rather than as the typo it is."""
        assert set(SEVERITY_OUTCOME) == set(SEVERITY_ORDER)


class TestTheReviewerIsSpawnedInTheSessionsOwnSlot:
    async def test_it_runs_in_the_slot_and_is_a_reviewer(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, sessions = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        await run_check(check, tmp_path)
        folder, _label, kind = sessions.created[0]
        assert folder == tmp_path
        assert kind == "reviewer"

    async def test_the_reviewer_is_reaped(self, tmp_path: Path) -> None:
        """A disposable session left in the manager holds a slot against
        ``WORKBENCH_MAX_CONCURRENT_SESSIONS`` for a conversation nobody can
        open."""
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, sessions = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        await run_check(check, tmp_path)
        assert sessions.sessions == {}

    async def test_the_prompt_carries_the_diff_and_the_brief(self, tmp_path: Path) -> None:
        """What the reviewer was actually sent — the brief *and* the change, in
        one message, so the instruction and the diff it is about arrive together.
        """
        base = git_repo(tmp_path)
        (tmp_path / "brand_new.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
        check, sessions = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        await run_check(check, tmp_path)
        assert len(sessions.prompts) == 1
        prompt = sessions.prompts[0]
        assert "SECRET_MARKER" in prompt
        assert "REFUTE" in prompt


class TestRefusals:
    async def test_a_session_with_no_slot_is_refused_not_run(self, tmp_path: Path) -> None:
        """Refusing is the feature. Reviewing the live workspace root would judge
        the user's unsaved edits as though the agent had made them."""
        check, _ = build(None, reports([]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "skipped"
        assert "no worktree slot" in evidence[0].detail
        assert derive_risk(evidence) == "low"

    async def test_a_non_session_subject_is_refused(self, tmp_path: Path) -> None:
        check, _ = build(None, reports([]))
        ctx = ValidationContext(
            subject=ValidationSubject(kind="file", ref="a.xlsx", label="a"),
            root=tmp_path,
        )
        evidence = await check.run(ctx)
        assert evidence[0].outcome == "skipped"
        assert "session output" in evidence[0].detail

    async def test_a_clean_tree_is_skipped_with_a_reason(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        check, sessions = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "skipped"
        assert "nothing to review" in evidence[0].detail
        # …and no reviewer was spawned, so a no-op change costs nothing.
        assert sessions.created == []

    async def test_a_vanished_checkout_is_refused(self, tmp_path: Path) -> None:
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path / "gone"), base="abc"), reports([]))
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "skipped"
        assert "is gone" in evidence[0].detail

    async def test_the_session_cap_names_the_setting(self, tmp_path: Path) -> None:
        """A cap that is hit renders as the cap and the way out, never a dead
        button — the ``SpawnRefusal`` idiom."""
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]), cap=0)
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "skipped"
        assert SETTING_MAX_SESSIONS in evidence[0].detail

    def test_every_named_setting_is_real(self) -> None:
        """A refusal that quotes a variable nobody can set is worse than no hint."""
        fields = Settings.model_fields
        for name in (SETTING_TIMEOUT, SETTING_MAX_TURNS, SETTING_MAX_BUDGET):
            assert name.removeprefix("WORKBENCH_").lower() in fields
        assert "max_concurrent_sessions" in fields


class TestTheConfiguredCeilingsAreCeilings:
    """A caller may ask for less. It may not ask for more.

    ``ReviewSpec``'s own bounds are 100 turns and $100 — the schema's outer edge,
    not a licence. Starting a review takes no per-run human approval, so a spec
    honoured as written would let one ``POST /api/validation/run`` spend fifty
    times what an operator who configured $2 "so a mistake survives" agreed to.
    """

    async def test_a_request_cannot_raise_the_operators_ceiling(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        evidence = await run_check(
            check, tmp_path, params={"max_turns": 100, "max_budget_usd": 100.0}
        )
        assert check.reviewer_caps() == (DEFAULT_MAX_TURNS, DEFAULT_MAX_BUDGET_USD)
        # …and the clamp is stated, not applied quietly: a caller that does not
        # learn its number was lowered reads a shorter review as a complete one.
        assert SETTING_MAX_TURNS in evidence[0].detail
        assert SETTING_MAX_BUDGET in evidence[0].detail
        assert evidence[0].outcome == "pass"  # a clamp changes cost, never verdict

    async def test_a_request_may_ask_for_less(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        evidence = await run_check(check, tmp_path, params={"max_turns": 5, "max_budget_usd": 0.5})
        assert check.reviewer_caps() == (5, 0.5)
        assert SETTING_MAX_TURNS not in evidence[0].detail

    async def test_an_absent_request_takes_the_configured_one(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([]))
        await run_check(check, tmp_path)
        assert check.reviewer_caps() == (DEFAULT_MAX_TURNS, DEFAULT_MAX_BUDGET_USD)

    def test_the_schema_bound_is_wider_than_the_default(self) -> None:
        """The premise. If the two ever met, the clamp above would be untestable
        and this class would silently stop proving anything."""
        assert ReviewSpec(max_turns=100).max_turns == 100
        assert DEFAULT_MAX_TURNS < 100
        assert DEFAULT_MAX_BUDGET_USD < 100.0


class TestAReviewThatCouldNotRunIsAFailure:
    """Never an absence read as approval — the whole milestone, in one class."""

    async def test_a_reviewer_that_never_reports_is_a_fail(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), silent())
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "fail"
        assert "NOT reviewed" in evidence[0].detail
        assert derive_risk(evidence) == "high"

    async def test_a_reviewer_that_errored_names_the_ceilings(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(
            SlotRef(slot="s1", path=str(tmp_path), base=base), silent("budget exhausted")
        )
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "fail"
        assert "budget exhausted" in evidence[0].detail
        assert SETTING_MAX_TURNS in evidence[0].detail

    async def test_a_reviewer_that_hangs_hits_the_wall_clock(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, sessions = build(
            SlotRef(slot="s1", path=str(tmp_path), base=base),
            never_answers(),
            timeout_s=0.2,
        )
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "fail"
        assert SETTING_TIMEOUT in evidence[0].detail
        # …and it was cleaned up rather than left running.
        assert sessions.sessions == {}


class TestTheReceiver:
    async def test_findings_for_an_unknown_session_are_refused(self, tmp_path: Path) -> None:
        """A reviewer that believes it filed a report it did not file is the one
        failure mode of this tool that could look like a clean review."""
        check, _ = build(None, reports([]))
        assert check.receive_findings("nobody", ReportFindingsRequest()) is None

    async def test_a_second_report_does_not_overwrite_the_first(self, tmp_path: Path) -> None:
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")

        def script(session: _Session, prompt: str) -> None:
            check.receive_findings(
                session.session_id,
                ReportFindingsRequest.model_validate({"findings": [MUST_FIX]}),
            )
            second = check.receive_findings(
                session.session_id, ReportFindingsRequest.model_validate({"findings": [NIT]})
            )
            assert second is not None
            assert "already recorded" in second
            session.emit(_TurnDone())

        check = AdversarialReviewCheck(
            _Locator(SlotRef(slot="s1", path=str(tmp_path), base=base)), timeout_s=5.0
        )
        sessions = _Sessions(script)
        check.bind(sessions)  # type: ignore[arg-type]
        evidence = await run_check(check, tmp_path)
        assert evidence[0].outcome == "fail"  # the must_fix, not the nit


class TestAFindingMustNameWhatBreaksIt:
    """Refute-first, enforced by the schema rather than by the prompt alone."""

    def test_a_finding_without_a_refutation_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ReviewFinding(severity="must_fix", claim="this is bad", refutation="")

    def test_whitespace_is_not_a_refutation(self) -> None:
        """``min_length`` alone accepts ``" "``, and a model with nothing to say
        reaches for exactly that when a field is required."""
        with pytest.raises(ValidationError):
            ReviewFinding(severity="must_fix", claim="bad", refutation="   ")

    def test_the_report_is_bounded(self) -> None:
        too_many = [MUST_FIX] * (MAX_FINDINGS + 1)
        with pytest.raises(ValidationError):
            ReportFindingsRequest.model_validate({"findings": too_many})


class TestTheScriptedReviewerDrivesTheRealChain:
    """``WORKBENCH_FAKE_AGENT``'s reviewer, end to end, with nothing stubbed.

    Everywhere else in this file the reviewer is a scripted ``AgentSession``
    calling :meth:`receive_findings` directly — deterministic, and blind to the
    two links in front of it. Here the objects are the shipped ones all the way
    down: a real :class:`SessionManager` over
    :func:`~workbench_server.services.fake_agent.fake_client_factory`, a real
    :class:`AgentSession`, the real ``report_findings`` tool body with its schema
    validation, the real receiver, ``derive_risk``.

    **Both answers, not one.** The canned script leads with a ``must_fix``, which
    left the other outcome — an empty report, which must arrive as a ``pass`` and
    never as silence — proven through this chain by nothing at all. That is the
    same gap, pointed the other way, that the canned findings were chosen to
    close.
    """

    async def _review_with(self, tmp_path: Path, focus: str) -> tuple[list[Any], Any]:
        base = git_repo(tmp_path)
        (tmp_path / "brand_new.py").write_text("X = 1\n", encoding="utf-8")
        check = AdversarialReviewCheck(
            _Locator(SlotRef(slot="s1", path=str(tmp_path), base=base)), timeout_s=20.0
        )
        manager = SessionManager(tmp_path, fake_client_factory(None, check), max_sessions=4)
        check.bind(manager)
        service = ValidationService(tmp_path, EventBus())
        service.register(check)
        result = await service.run(
            ValidationSpec(
                subject=ValidationSubject(kind="session_output", ref="sess-1", label="w"),
                checks=["review"],
                params={"focus": focus},
            )
        )
        return list(result.evidence), (service, result)

    @pytest.mark.timeout(60)
    async def test_a_clean_review_is_a_pass_through_the_real_tool_body(
        self, tmp_path: Path
    ) -> None:
        """The half that had no coverage. An empty findings list is schema-valid,
        reaches the receiver, and becomes a ``pass`` line that *says* it found
        nothing — never blankness a reader has to interpret (AXI shape 2)."""
        evidence, (service, result) = await self._review_with(tmp_path, CLEAN_REVIEW_TRIGGER)
        assert len(evidence) == 1
        assert evidence[0].outcome == "pass"
        assert "No findings" in evidence[0].detail
        assert result.risk == "pass"
        ref = evidence[0].payload_ref
        assert ref is not None
        payload = service.payload("diff", ref)
        assert isinstance(payload, ReviewReport)
        assert payload.findings == []

    @pytest.mark.timeout(60)
    async def test_the_canned_findings_still_fail_and_await_a_human(self, tmp_path: Path) -> None:
        """The control, and the half the E2E journey lands on: the same chain
        with the default script is a ``fail`` at ``high`` risk with no approval
        on it."""
        evidence, (_service, result) = await self._review_with(tmp_path, "")
        assert evidence[0].outcome == "fail"
        assert "1 must_fix" in evidence[0].detail
        assert result.risk == "high"
        assert result.approval is None

    def test_the_trigger_is_read_from_the_brief_and_not_from_the_diff(self) -> None:
        """A trigger scanned across the whole prompt would let the change under
        review decide what its own review says — a diff that merely *contained*
        the words would come back clean."""
        diff = _diff_stub()
        diff.text = f"+# {CLEAN_REVIEW_TRIGGER}\n"
        prompt = review_prompt(diff, "")
        assert CLEAN_REVIEW_TRIGGER not in prompt.split(DIFF_MARKER, 1)[0].lower()
        assert CLEAN_REVIEW_TRIGGER in prompt


class TestTheCheckNeverApproves:
    """Asserted two ways — the ``test_orchestrator.py`` precedent."""

    async def test_a_must_fix_result_is_still_awaiting_a_human(self, tmp_path: Path) -> None:
        """The behavioural half. A review that found a blocking problem produces
        a ``high`` result with **no approval on it** — the human gate is still
        the only thing that can clear this."""
        base = git_repo(tmp_path)
        (tmp_path / "new.py").write_text("X = 1\n", encoding="utf-8")
        check, _ = build(SlotRef(slot="s1", path=str(tmp_path), base=base), reports([MUST_FIX]))
        service = ValidationService(tmp_path, EventBus())
        service.register(check)
        result = await service.run(
            ValidationSpec(
                subject=ValidationSubject(kind="session_output", ref="sess-1", label="w"),
                checks=["review"],
            )
        )
        assert result.risk == "high"
        assert result.approval is None

    def test_the_module_references_no_approval_api(self) -> None:
        """The structural half, read off the AST rather than the prose. An agent
        that could commission a reviewer *and* count its verdict as approval has
        quietly become its own merge queue."""
        for forbidden in ("approve", "approval", "ValidationApproval", "set_approval"):
            assert forbidden not in REVIEW_REFERENCED, forbidden

    def test_no_agent_facing_tool_starts_a_review(self) -> None:
        """A session that could commission its own reviewer could also loop on
        it, and the money that pays for that loop is the user's."""
        from workbench_server.services.agent_tools import ALL_AGENT_TOOLS

        for spec in ALL_AGENT_TOOLS:
            assert "review" not in spec.name or spec.name == "report_findings"
        assert not any(spec.name == "start_review" for spec in ALL_AGENT_TOOLS)
