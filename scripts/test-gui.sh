#!/bin/sh
# Run the whole suite, including OpenGL viewport tests, on a virtual X server.
cd "$(dirname "$0")/.." || exit 1
exec env -u WAYLAND_DISPLAY xvfb-run -a -s "-screen 0 1600x1000x24" \
  env QT_QPA_PLATFORM=xcb GOOSLICER_GL_TESTS=1 .venv/bin/python -m pytest "$@"
