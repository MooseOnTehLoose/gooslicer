"""An OpenGL context for GPU slicing on the slicing thread (gooslicer.gpu_raster).

The offscreen surface has to be made on the GUI thread; the context is made, used and
destroyed on the slicing thread, so it never touches the 3D view's context."""
from __future__ import annotations

from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat


def gl_format() -> QSurfaceFormat:
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CoreProfile)
    f.setDepthBufferSize(0)
    f.setStencilBufferSize(0)          # slicing draws into its own framebuffer
    f.setSamples(0)
    return f


class ThreadGL:
    """Create on the GUI thread; `with ThreadGL…:` on the slicing thread makes a context current
    there (and raises RuntimeError if it can't)."""

    def __init__(self):
        self.surface = QOffscreenSurface()
        self.surface.setFormat(gl_format())
        self.surface.create()
        self.ctx = None

    def __enter__(self):
        if not self.surface.isValid():
            raise RuntimeError("no offscreen OpenGL surface")
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(gl_format())
        if not self.ctx.create():
            raise RuntimeError("no OpenGL 3.3 context")
        if not self.ctx.makeCurrent(self.surface):
            raise RuntimeError("couldn't use the OpenGL context")
        return self

    def __exit__(self, *exc):
        if self.ctx is not None:
            self.ctx.doneCurrent()
            self.ctx = None
        return False

    def destroy(self) -> None:
        """On the GUI thread, after the slice."""
        self.surface.destroy()
