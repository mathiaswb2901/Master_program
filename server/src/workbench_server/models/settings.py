"""Settings schemas: the knobs that were environment variables, made typed.

Three things a settings surface has to get right, and each is a field here
rather than a sentence in the UI:

* **What is stored** — the user's choice (:class:`WorkbenchSettings`), persisted
  as one small versioned document under the machine's app-data dir. Nothing is
  ever written to ``~/.claude`` (the security posture in ``CLAUDE.md``): a
  settings panel that could edit the global Claude scope would be a way to turn
  on the very hooks-and-permissions inheritance the server keeps off by default.
* **What is in force** — :attr:`SettingsState.effective`. An operator who
  exported ``WORKBENCH_OFFICE_NATIVE=off`` outranks the stored choice, and a
  control that silently did nothing about it would be the dead button the pane
  rules forbid. Each such knob is reported in :attr:`SettingsState.overrides`
  with the variable that set it, so the UI can show the value *and* say who
  decided it.
* **What is not a setting** — :class:`TelemetryStance`. Zero telemetry is a
  product position, not a preference, so it is reported as a fact with no way to
  change it. It is a model rather than a hard-coded UI string so the claim comes
  from the server that would have to do the sending.
"""

from typing import Literal

from pydantic import BaseModel, Field

from workbench_server.models.office_host import OfficeNativeMode

#: Bumped when a stored document from an older version can no longer be read as
#: this one. A document stamped with anything else is discarded rather than
#: guessed at (the ``sessions.json`` / ``workspaces.json`` discipline).
SETTINGS_VERSION = 1

#: A settings document is a handful of scalars; anything larger is not one.
MAX_FILE_BYTES = 16 * 1024

#: How the window picks its palette. ``system`` follows the OS preference —
#: which is what a machine with no stored choice already does (``index.html``
#: reads ``prefers-color-scheme`` before the first paint).
ThemeChoice = Literal["system", "dark", "light"]

#: The settings a client can address, and the only keys an override may name.
SettingKey = Literal["theme", "office_native", "voice_input"]


class WorkbenchSettings(BaseModel):
    """A user's stored choices — and the PUT body, which is the same shape.

    Deliberately small and flat: every field is a scalar with a default, so a
    document written by an older version simply arrives with the new field at
    its default rather than failing to parse. Nothing here is a secret, a path
    or anything else that would make the file sensitive to commit — it is
    machine-local only because a theme is about *you*, not about a project.
    """

    #: The palette the window wears. ``system`` follows the OS.
    theme: ThemeChoice = "system"
    #: Whether documents open in the *real* installed Word/Excel docked into a
    #: panel. Same three values as ``WORKBENCH_OFFICE_NATIVE``, which overrides
    #: it when set — see :class:`SettingOverride`. Applied when the server next
    #: starts (the host backend is built once, at launch), which the state below
    #: reports rather than leaving the user to discover.
    office_native: OfficeNativeMode = "auto"
    #: Push-to-talk voice input. Off by default, and the transcriber it needs is
    #: a separate, consent-gated install (M7 §3) — so this is remembered even on
    #: a machine that cannot yet act on it.
    voice_input: bool = False


class SettingsFile(BaseModel):
    """The on-disk document: the choices, stamped with the schema version."""

    version: int = SETTINGS_VERSION
    settings: WorkbenchSettings = Field(default_factory=WorkbenchSettings)


class SettingOverride(BaseModel):
    """A setting this process was configured with from outside the app.

    An environment variable (or ``workbench.toml``) beats the stored choice, and
    the UI shows the control disabled with :attr:`detail` as the reason. The
    stored value is kept exactly as it was — un-setting the variable puts the
    user's own choice back, which is why an override is reported rather than
    written through into the file.
    """

    key: SettingKey
    #: The value actually in force, rendered as text (every overridable setting
    #: is a scalar, and the client only ever shows this).
    value: str
    #: One line naming who decided it, e.g. "Set by WORKBENCH_OFFICE_NATIVE".
    detail: str


class TelemetryStance(BaseModel):
    """Not a setting: the zero-telemetry position, stated by the server.

    :attr:`enabled` is ``Literal[False]`` on purpose — there is no shape of this
    model in which it is true, so "turn it on" is not a request the API can even
    express, and the UI renders a statement instead of a switch.
    """

    enabled: Literal[False] = False
    detail: str


class SettingsState(BaseModel):
    """GET/PUT /api/settings — stored choices, what is in force, and why.

    :attr:`stored` is what the controls show; :attr:`effective` is what the
    server is using. They differ only where an :class:`SettingOverride` says so,
    or where a change is waiting for a restart.
    """

    stored: WorkbenchSettings
    effective: WorkbenchSettings
    #: Settings decided outside the app. Usually empty.
    overrides: list[SettingOverride] = Field(default_factory=list)
    #: Settings whose stored value differs from what this *running* process is
    #: using because it is only read at launch. The UI says "applies when
    #: Workbench restarts" rather than implying an instant effect.
    pending_restart: list[SettingKey] = Field(default_factory=list)
    #: Where the document lives, shown so a user can see it is machine-local
    #: state and not something in their project (and never ``~/.claude``).
    path: str
    telemetry: TelemetryStance
    #: Why the stored document was ignored, when it could not be read. The
    #: defaults are in force; nothing about it stops the server.
    problem: str | None = None
