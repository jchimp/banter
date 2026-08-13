"""Device state machine.

Exists mainly to enforce PRD FR-10: recording and playback are mutually exclusive.
Importable without hardware so it can be unit-tested off the Pi.
"""

import threading
from enum import StrEnum


class State(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    UPLOADING = "uploading"
    PLAYING = "playing"
    ERROR = "error"


#: Allowed transitions. Anything not listed is rejected.
TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.RECORDING, State.PLAYING, State.UPLOADING, State.ERROR},
    State.RECORDING: {State.UPLOADING, State.IDLE, State.ERROR},
    State.UPLOADING: {State.IDLE, State.PLAYING, State.RECORDING, State.ERROR},
    State.PLAYING: {State.IDLE, State.ERROR},
    State.ERROR: {State.IDLE},
}


class StateMachine:
    """Thread-safe. The button callbacks and the uploader thread both touch this."""

    def __init__(self, initial: State = State.IDLE) -> None:
        self._state = initial
        self._lock = threading.RLock()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def can(self, target: State) -> bool:
        with self._lock:
            return target in TRANSITIONS[self._state]

    def to(self, target: State) -> bool:
        """Attempt a transition. Returns False (does not raise) if disallowed."""
        with self._lock:
            if target not in TRANSITIONS[self._state]:
                return False
            self._state = target
            return True

    def is_busy(self) -> bool:
        """True while the box is doing something the other button must not interrupt."""
        return self.state in (State.RECORDING, State.PLAYING)
