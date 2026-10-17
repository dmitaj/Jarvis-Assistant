"""A Qt-free stand-in for ``desktop_app.face_widget``'s state API.

The core reports idle/listening/thinking/speaking through
``desktop_app.face_widget.get_jarvis_state().set_state(...)``. The server
installs this module under that name so those reports reach browsers instead
of the desktop face.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Callable


class JarvisState(Enum):
    ASLEEP = "asleep"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DICTATING = "dictating"
    DICTATION_PROCESSING = "dictation_processing"


# Set by the server: called with the new state's value on every change.
on_change: Callable[[str], None] = lambda state: None


class JarvisStateManager:
    def __init__(self) -> None:
        self._state = JarvisState.ASLEEP
        self._lock = threading.Lock()

    @property
    def state(self) -> JarvisState:
        return self._state

    def set_state(self, state: JarvisState) -> None:
        with self._lock:
            if state == self._state:
                return
            self._state = state
        on_change(state.value)


_manager = JarvisStateManager()


def get_jarvis_state() -> JarvisStateManager:
    return _manager
