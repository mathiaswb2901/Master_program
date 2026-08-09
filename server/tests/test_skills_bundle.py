"""The bundled skills plugin: package data, path resolution, and the wiring.

Three things can silently break this feature and none of them fail loudly at
runtime: a skill file with unparseable frontmatter (the CLI drops it), a wheel
that ships the code without the skills, and a session whose options lost the
plugin. Each gets a test here.
"""

import json
import os
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from workbench_server.config import Settings
from workbench_server.models.office_bridge import (
    CellEdit,
    CellWindow,
    DocStructure,
    SlideText,
    WordEdit,
    WordText,
)
from workbench_server.models.plans import PlanArtifact, PlanResponse
from workbench_server.services import skills_bundle
from workbench_server.services.commands import CommandRelay
from workbench_server.services.event_bus import EventBus
from workbench_server.services.office_host.document_bridge import DocNotHostedError
from workbench_server.services.sdk_factory import UiStateStore, build_agent_options
from workbench_server.services.search import SearchService
from workbench_server.services.skills_bundle import PLUGIN_NAME, bundled_plugin_path
from workbench_server.services.validation import ValidationService
from workbench_server.services.workspace import Workspace

EXPECTED_SKILLS = {"plan-visual", "remember", "workbench-dev"}
#: Skills are progressive disclosure: the body is loaded into context on use, so
#: a bloated one is a cost paid on every invocation.
MAX_SKILL_LINES = 150
#: Reference files a SKILL.md points at, loaded only when the agent goes looking
#: — the second level of the same disclosure. They ship as package data like the
#: skill bodies, and a reference a wheel dropped is guidance nobody ever reads.
EXPECTED_REFERENCES = {"plan-visual/reference/visual.md"}


class StubBridge:
    """SessionBridge stub — the factory only stores the callbacks."""

    #: The orchestrator toolset acts *as* a session, so the bridge names one.
    session_id = "stub-session"

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        return True

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:  # pragma: no cover
        raise AssertionError("not exercised")


class StubReader:
    """OfficeDocumentAccess stub — the factory only stores it on the options."""

    async def document_structure(self, path: str) -> DocStructure:  # pragma: no cover
        raise DocNotHostedError(path)

    async def read_document(  # pragma: no cover
        self,
        path: str,
        *,
        max_chars: int,
        max_cells: int,
        sheet: str | None = None,
        a1_range: str | None = None,
        start_paragraph: int = 0,
        start_slide: int = 1,
    ) -> WordText | CellWindow | SlideText:
        raise DocNotHostedError(path)

    async def write_document(  # pragma: no cover
        self,
        path: str,
        *,
        content: str,
        paragraph: int | None = None,
        sheet: str | None = None,
        cell: str | None = None,
    ) -> WordEdit | CellEdit:
        raise DocNotHostedError(path)


def options_for(settings: Settings, resume: str | None = None) -> Any:
    return build_agent_options(
        UiStateStore(),
        settings,
        Path.cwd(),
        resume,
        StubBridge(),
        StubReader(),
        CommandRelay(EventBus()),
        ValidationService(Path.cwd(), EventBus()),
        SearchService(Workspace(Path.cwd())),
    )


def parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal ``key: value`` frontmatter reader.

    Deliberately not a YAML parser: the point is to pin that every SKILL.md
    opens with a delimited block carrying flat, single-line ``name`` and
    ``description`` keys — the shape the CLI needs to list the skill at all.
    """
    lines = text.split("\n")
    assert lines[0] == "---", "SKILL.md must open with a --- frontmatter fence"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        assert ":" in line, f"non key: value frontmatter line: {line!r}"
        key, _, value = line.partition(":")
        assert not key.startswith(" "), f"nested frontmatter is not supported: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def skill_files() -> Iterator[Path]:
    root = bundled_plugin_path()
    assert root is not None
    yield from sorted(root.joinpath("skills").glob("*/SKILL.md"))


def reference_files() -> Iterator[Path]:
    root = bundled_plugin_path()
    assert root is not None
    yield from sorted(root.joinpath("skills").glob("*/reference/*.md"))


class TestPathResolution:
    def test_resolves_to_a_real_directory_with_a_manifest(self) -> None:
        path = bundled_plugin_path()
        assert path is not None
        assert path.is_dir()
        assert path.joinpath(".claude-plugin", "plugin.json").is_file()
        # The CLI resolves --plugin-dir against its own cwd, not ours.
        assert path.is_absolute()

    def test_missing_manifest_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken install must not take agent sessions down with it."""
        monkeypatch.setattr(skills_bundle, "files", lambda package: tmp_path)
        assert bundled_plugin_path() is None

    def test_absent_bundle_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(skills_bundle, "files", lambda package: tmp_path / "gone")
        assert bundled_plugin_path() is None


class TestManifest:
    def test_plugin_json_parses_and_is_named_workbench(self) -> None:
        root = bundled_plugin_path()
        assert root is not None
        manifest = json.loads(
            root.joinpath(".claude-plugin", "plugin.json").read_text(encoding="utf-8")
        )
        # The name is the namespace: skills are addressed as ``workbench:<name>``.
        assert manifest["name"] == PLUGIN_NAME == "workbench"
        assert manifest["version"] and manifest["description"]


class TestSkillFiles:
    def test_exactly_the_expected_skills_ship(self) -> None:
        assert {path.parent.name for path in skill_files()} == EXPECTED_SKILLS

    @pytest.mark.parametrize("skill", sorted(EXPECTED_SKILLS))
    def test_frontmatter_is_complete_and_matches_the_directory(self, skill: str) -> None:
        path = next(p for p in skill_files() if p.parent.name == skill)
        fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["name"] == skill, "name must match the directory the CLI keys on"
        # Triggering is driven entirely by the description; an empty or terse one
        # means the skill is invisible in practice.
        assert len(fields["description"]) >= 60

    @pytest.mark.parametrize("skill", sorted(EXPECTED_SKILLS))
    def test_body_stays_within_the_context_budget(self, skill: str) -> None:
        path = next(p for p in skill_files() if p.parent.name == skill)
        assert len(path.read_text(encoding="utf-8").splitlines()) <= MAX_SKILL_LINES

    def test_no_secrets_or_personal_paths_leak_into_the_bundle(self) -> None:
        """The bundle is tracked, published package data (CLAUDE.md danger zone)."""
        for path in [*skill_files(), *reference_files()]:
            body = path.read_text(encoding="utf-8")
            assert "C:\\Users\\" not in body
            assert "/home/" not in body


class TestReferenceFiles:
    """The second level of progressive disclosure.

    A reference is how a skill grows without growing what every invocation
    costs — but only if the SKILL.md actually points at it and the file
    actually ships. Both fail silently otherwise: the agent reads a body that
    promises detail it can never find.
    """

    def test_exactly_the_expected_references_ship(self) -> None:
        root = bundled_plugin_path()
        assert root is not None
        found = {path.relative_to(root / "skills").as_posix() for path in reference_files()}
        assert found == EXPECTED_REFERENCES

    def test_every_reference_is_named_by_its_own_skill(self) -> None:
        for path in reference_files():
            body = path.parent.parent.joinpath("SKILL.md").read_text(encoding="utf-8")
            assert f"reference/{path.name}" in body, path.name

    def test_a_reference_stays_out_of_the_skill_body_budget(self) -> None:
        """The whole point: detail lives here, not in the body that is loaded on
        every invocation. If a reference is shorter than the body's own section
        about it, it is not carrying its weight."""
        for path in reference_files():
            assert len(path.read_text(encoding="utf-8").splitlines()) > MAX_SKILL_LINES // 2


class TestAgentOptions:
    def test_bundle_is_wired_as_a_local_plugin_by_default(self) -> None:
        options = options_for(Settings())
        expected = bundled_plugin_path()
        assert expected is not None
        assert options.plugins == [{"type": "local", "path": str(expected)}]

    def test_workspace_scoped_settings_load_but_the_global_scope_does_not(self) -> None:
        """``local`` is ``.claude/settings.local.json`` — the machine-local file
        Claude Code writes "always allow" rules and local hooks into. Dropping it
        would make a folder behave differently here than in plain Claude Code;
        only the user's global ~/.claude scope is meant to be excluded."""
        assert options_for(Settings()).setting_sources == ["project", "local"]

    def test_inherit_flag_restores_the_sdk_default(self) -> None:
        """``None`` is "load every source", which is what sessions did before."""
        options = options_for(Settings(inherit_user_settings=True))
        assert options.setting_sources is None
        assert options.plugins  # the bundle is orthogonal to inheritance

    def test_bundle_can_be_switched_off(self) -> None:
        assert options_for(Settings(bundled_skills=False)).plugins == []

    def test_skills_option_is_never_used(self) -> None:
        """``skills="all"`` appends a bare ``Skill`` to allowed_tools, which
        shadows can_use_tool and auto-allows every discovered skill; a list would
        also hide the user's own project skills. Neither is acceptable here."""
        options = options_for(Settings())
        assert options.skills is None
        assert "Skill" not in options.allowed_tools

    def test_the_skills_the_agent_is_told_to_use_are_allowed_narrowly(self) -> None:
        """Instructing a skill the agent cannot invoke without a dialog is worse
        than not instructing it: the user who declines gets the ungrounded output.
        The rules stay per-skill so nothing else is auto-approved."""
        allowed = options_for(Settings()).allowed_tools
        assert "Skill(workbench:plan-visual)" in allowed
        assert "Skill(workbench:remember)" in allowed
        # workbench-dev is a reference the agent reaches for on its own judgment;
        # no prompt pushes it, so it keeps the normal permission prompt.
        assert "Skill(workbench:workbench-dev)" not in allowed
        # A rule whose name no longer matches a shipped directory grants nothing
        # and fails silently, so the two are pinned to each other here.
        named = {
            rule[len("Skill(workbench:") : -1] for rule in allowed if rule.startswith("Skill(")
        }
        assert named <= {path.parent.name for path in skill_files()}

    def test_narrow_skill_rules_do_not_shadow_the_permission_callback(self) -> None:
        """The SDK's own rule parser decides this: an entry with a real
        specifier allows only matching invocations, so can_use_tool still runs
        for every other skill. Asserted against the SDK, not our reading of it."""
        from claude_agent_sdk.types import _get_can_use_tool_shadowed_warning

        options = options_for(Settings())
        message = _get_can_use_tool_shadowed_warning(
            options.permission_mode, [rule for rule in options.allowed_tools if "Skill" in rule]
        )
        assert message is None

    def test_skill_rules_are_dropped_with_the_bundle(self) -> None:
        """A rule naming a skill no session can see is dead config."""
        allowed = options_for(Settings(bundled_skills=False)).allowed_tools
        assert not [rule for rule in allowed if rule.startswith("Skill")]

    def test_system_prompt_points_the_agent_at_the_plan_skill(self) -> None:
        append = options_for(Settings()).system_prompt["append"]
        assert "workbench:plan-visual" in append


class TestResumedSessions:
    def test_plugin_dir_is_passed_on_resume_too(self) -> None:
        """Resuming rebuilds the CLI argv from scratch; a session restored from
        history must get the same skills as a fresh one. Drives the SDK's own
        command builder rather than trusting that."""
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        expected = bundled_plugin_path()
        assert expected is not None
        for resume in (None, "0193f2a1b2c34d5e"):
            transport = SubprocessCLITransport(prompt="hi", options=options_for(Settings(), resume))
            transport._cli_path = "claude"  # no CLI discovery; we only want argv
            cmd = transport._build_command()

            assert "--plugin-dir" in cmd
            assert cmd[cmd.index("--plugin-dir") + 1] == str(expected)
            assert "--setting-sources=project,local" in cmd
            assert ("--resume=0193f2a1b2c34d5e" in cmd) is (resume is not None)

    def test_inherit_flag_omits_the_setting_sources_flag(self) -> None:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = SubprocessCLITransport(
            prompt="hi", options=options_for(Settings(inherit_user_settings=True))
        )
        transport._cli_path = "claude"
        assert not [arg for arg in transport._build_command() if "--setting-sources" in arg]


@pytest.mark.timeout(300)
@pytest.mark.skipif(
    not (os.environ.get("CI") or os.environ.get("WORKBENCH_PACKAGING_TEST") == "1"),
    reason="builds a wheel; runs in CI, or set WORKBENCH_PACKAGING_TEST=1 to run locally",
)
def test_wheel_ships_the_skill_files(tmp_path: Path) -> None:
    """Package data is only data if the build backend picks it up.

    hatchling selects files through the VCS ignore rules, and the bundle hides
    behind a dot-directory (``.claude-plugin``) — exactly the shape that gets
    dropped silently, leaving an installed Workbench whose agents have no skills.

    Gated on ``CI`` because it shells out to a real build: on a machine that is
    offline or has a cold uv cache the build fails for reasons unrelated to any
    code change, and it would tax every local ``uv run pytest`` with a packaging
    step. The gate the release depends on still runs it on every push and PR.
    """
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover - CI and dev machines both have uv
        pytest.skip("uv is not on PATH")
    repo_root = Path(__file__).resolve().parents[2]

    try:
        subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
            [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - build must succeed
        # Without this the failure surfaces as a bare CalledProcessError and the
        # build's own diagnostics stay inside the captured pipes.
        pytest.fail(
            f"uv build --wheel failed (exit {exc.returncode})\n"
            f"stdout:\n{exc.stdout.decode(errors='replace')}\n"
            f"stderr:\n{exc.stderr.decode(errors='replace')}"
        )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "workbench_server/skills_bundle/.claude-plugin/plugin.json" in names
    assert {
        f"workbench_server/skills_bundle/skills/{skill}/SKILL.md" for skill in EXPECTED_SKILLS
    } <= names
    # A reference the wheel dropped is a SKILL.md pointing at nothing.
    assert {
        f"workbench_server/skills_bundle/skills/{reference}" for reference in EXPECTED_REFERENCES
    } <= names


@pytest.mark.timeout(300)
@pytest.mark.skipif(
    os.environ.get("WORKBENCH_LIVE_AGENT") != "1",
    reason="live agent smoke test; set WORKBENCH_LIVE_AGENT=1 to run",
)
async def test_live_session_loads_the_plugin_and_lists_its_skills(tmp_path: Path) -> None:
    """The only assertion the CLI itself can make: its init frame.

    Everything above proves we *ask* for the plugin. This proves the CLI found
    it, parsed the manifest, namespaced the skills — and, because the user's
    global skills are absent from the same list, that ``setting_sources`` really
    does keep ``~/.claude`` out of a session.
    """
    from claude_agent_sdk import ClaudeSDKClient, SystemMessage

    options = options_for(Settings())
    options.cwd = str(tmp_path)
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        await client.query("Reply with exactly the word: pong.")
        init: dict[str, Any] = {}
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                init = message.data
                break
    finally:
        await client.disconnect()

    assert [plugin["name"] for plugin in init["plugins"]] == [PLUGIN_NAME]
    assert {f"{PLUGIN_NAME}:{skill}" for skill in EXPECTED_SKILLS} <= set(init["skills"])
