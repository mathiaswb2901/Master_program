"""The host lifecycle as a state machine, separate from anything that runs.

Kept in its own module because it is the part that must be exhaustively true:
the service drives it across ``await`` points where a close request, a crashed
process and a slow embed can all arrive in any order, and "which transitions are
legal" is then the only thing standing between that and a window nobody owns.

Terminal states are terminal. A host that has settled — closed, crashed,
failed — never moves again; the record stays as the answer to "what happened to
that document", and a new host gets a new id.
"""

import time
from collections.abc import Callable

from workbench_server.models.office_host import (
    HostAppKind,
    HostReason,
    HostState,
    OfficeHostInfo,
)

#: Where a host may go from each state. Everything absent is a bug, not a
#: tolerated no-op: reaching ``embedded`` without passing through ``embedding``
#: would mean a window was hosted without anyone reparenting it.
LEGAL_TRANSITIONS: dict[HostState, frozenset[HostState]] = {
    "launching": frozenset({"embedding", "closed", "crashed", "failed"}),
    "embedding": frozenset({"embedded", "closed", "crashed", "failed"}),
    "embedded": frozenset({"detached", "closed", "crashed", "failed"}),
    # Re-embedding is the reason ``detached`` is not terminal: the panel was
    # closed or moved, the document stayed open, and it can come back.
    "detached": frozenset({"embedding", "closed", "crashed", "failed"}),
    "closed": frozenset(),
    "crashed": frozenset(),
    "failed": frozenset(),
}

#: States a host never leaves.
TERMINAL_STATES: frozenset[HostState] = frozenset({"closed", "crashed", "failed"})

#: Where every host starts. ``closed`` is where one *ends*; a document that was
#: never opened has no record at all.
INITIAL_STATE: HostState = "launching"


class IllegalTransitionError(Exception):
    """A transition the lifecycle does not allow."""

    def __init__(self, source: HostState, target: HostState) -> None:
        super().__init__(f"{source} -> {target}")
        self.source = source
        self.target = target


class ForeignProcessError(Exception):
    """A handle that is not the process this host launched.

    The other half of "never adopt a process we did not launch": the pid is
    bound once, at launch, and every later backend call is checked against it.
    Without this a backend could hand back a window the *user* opened and we
    would reparent — or close — their document.
    """


def is_terminal(state: HostState) -> bool:
    return state in TERMINAL_STATES


def can_transition(source: HostState, target: HostState) -> bool:
    return target in LEGAL_TRANSITIONS[source]


class HostLifecycle:
    """One host's state, and the only thing allowed to change it."""

    def __init__(
        self,
        host_id: str,
        path: str,
        kind: HostAppKind,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.host_id = host_id
        self.path = path
        self.kind = kind
        self._clock = clock
        self.state: HostState = INITIAL_STATE
        self.reason: HostReason | None = None
        self.pid: int | None = None
        self.since: float = clock()

    @property
    def terminal(self) -> bool:
        return is_terminal(self.state)

    def bind_pid(self, pid: int) -> None:
        """Record the process this host launched. Once, and only once.

        A host is bound to exactly one instance for its whole life, which is
        what makes every later ownership check a comparison rather than a guess.
        """
        if self.pid is not None and self.pid != pid:
            raise ForeignProcessError(f"host {self.host_id} is bound to pid {self.pid}, not {pid}")
        self.pid = pid

    def to(self, target: HostState, *, reason: HostReason | None = None) -> None:
        """Move to ``target``. Raises :class:`IllegalTransitionError` otherwise.

        A reason belongs to a terminal state; moving on to a live state clears
        whatever was there, so a UI can render ``reason`` without also checking
        which state it belongs to.
        """
        if not can_transition(self.state, target):
            raise IllegalTransitionError(self.state, target)
        self.state = target
        self.reason = reason if is_terminal(target) else None
        self.since = self._clock()

    def info(self) -> OfficeHostInfo:
        return OfficeHostInfo(
            host_id=self.host_id,
            path=self.path,
            kind=self.kind,
            state=self.state,
            reason=self.reason,
            pid=self.pid,
            since=self.since,
        )
