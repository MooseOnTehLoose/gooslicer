import os

# GUI tests run offscreen by default (no windows on the desktop). The GL viewport tests
# need a real GL platform: run scripts/test-gui.sh (Xvfb) to include them.
if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _discard_unsaved(monkeypatch):
    """Closing a window with unsaved changes asks Save / Don't save / Cancel in a modal box,
    which would hang a test. Tests answer "Don't save" unless they replace this themselves."""
    try:
        from gooslicer_qt import projectio
    except ImportError:                      # no PySide6: the Qt-free tests still run
        return
    monkeypatch.setattr(projectio.ProjectController, "ask_unsaved", lambda self: projectio.DISCARD)
