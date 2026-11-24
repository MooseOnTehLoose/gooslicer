import os

# GUI tests run offscreen by default (no windows on the desktop). The GL viewport tests
# need a real GL platform: run scripts/test-gui.sh (Xvfb) to include them.
if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
