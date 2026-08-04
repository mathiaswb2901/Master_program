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
from workbench_server.models.plans import PlanArtifact, PlanResponse
from workbench_server.services import skills_bundle
from workbench_server.services.sdk_factory import UiStateStore, build_agent_options
from workbench_server.services.skills_bundle import PLUGIN_NAME, bundled_plugin_path

EXPECTED_SKILLS = {"plan-visual", "remember", "workbench-dev"}
#: Skills are progressive disclosure: the body is loaded into context on use, so
#: a bloated one is a cost paid on every invocation.
MAX_SKILL_LINES = 150


class StubBridge:
    """SessionBridge stub — the factory only stores the callbacks."""

    async def ask_permission(self, tool: str, tool_input: dict[str, Any]) -> bool:
        return True

    async def present_plan(self, artifact: PlanArtifact) -> PlanResponse:  # pragma: no cover
        raise AssertionError("not exercised")


def options_for(settings: Settings, resume: str | None = None) -> Any:
    return build_agent_options(UiStateStore(), settings, Path.cwd(), resume, StubBridge())


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
        for path in skill_files():
            body = path.read_text(encoding="utf-8")
            assert "C:\\Users\\" not in body
            assert "/home/" not in body


class TestAgentOptions:
    def test_bundle_is_wired_as_a_local_plugin_by_default(self) -> None:
        options = options_for(Settings())
        expected = bundled_plugin_path()
        assert expected is not None
        assert options.plugins == [{"type": "local", "path": str(expected)}]

    def test_sessions_do_not_inherit_user_settings_by_default(self) -> None:
        assert options_for(Settings()).setting_sources == ["project"]

    def test_inherit_flag_restores_the_sdk_default(self) -> None:
        """``None`` is "load every source", which is what sessions did before."""
        options = options_for(Settings(skills_inherit_user=True))
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
            assert "--setting-sources=project" in cmd
            assert ("--resume=0193f2a1b2c34d5e" in cmd) is (resume is not None)

    def test_inherit_flag_omits_the_setting_sources_flag(self) -> None:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = SubprocessCLITransport(
            prompt="hi", options=options_for(Settings(skills_inherit_user=True))
        )
        transport._cli_path = "claude"
        assert not [arg for arg in transport._build_command() if "--setting-sources" in arg]


@pytest.mark.timeout(300)
def test_wheel_ships_the_skill_files(tmp_path: Path) -> None:
    """Package data is only data if the build backend picks it up.

    hatchling selects files through the VCS ignore rules, and the bundle hides
    behind a dot-directory (``.claude-plugin``) — exactly the shape that gets
    dropped silently, leaving an installed Workbench whose agents have no skills.
    """
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover - CI and dev machines both have uv
        pytest.skip("uv is not on PATH")
    repo_root = Path(__file__).resolve().parents[2]

    subprocess.run(  # noqa: S603 - fixed argv, path from shutil.which
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "workbench_server/skills_bundle/.claude-plugin/plugin.json" in names
    assert {
        f"workbench_server/skills_bundle/skills/{skill}/SKILL.md" for skill in EXPECTED_SKILLS
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
