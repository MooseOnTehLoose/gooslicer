"""A headless OpenGL 3.3 core context (EGL, no window or display server), for GPU work in a
worker process: the island check draws its layers with gpu_raster (08 §5).

EGL is called through ctypes on libEGL, not through PyOpenGL, so it works whichever platform
PyOpenGL picked when it was loaded (the worker processes have the app, and so PyOpenGL for
GLX or Wayland, imported already): GL calls go through libglvnd's dispatch to the context
current on the thread, and gpu_raster uses the raw entry points for that reason. Mesa's
surfaceless platform needs no display at all, so this also works under Xvfb, over SSH and in
the Flatpak sandbox (with --device=dri). Not for the GUI process: Qt draws there.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from contextlib import contextmanager

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_EXTENSIONS = 0x3055
EGL_NONE = 0x3038
EGL_RENDERABLE_TYPE = 0x3040
EGL_SURFACE_TYPE = 0x3033
EGL_PBUFFER_BIT = 0x0001
EGL_OPENGL_BIT = 0x0008
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_MAJOR_VERSION = 0x3098
EGL_CONTEXT_MINOR_VERSION = 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT = 0x0001
GL_RENDERER = 0x1F01

_vp, _int = ctypes.c_void_p, ctypes.c_int32


class HeadlessUnavailable(RuntimeError):
    pass


def _lib():
    for name in ("libEGL.so.1", ctypes.util.find_library("EGL")):
        if name:
            try:
                egl = ctypes.CDLL(name)
                break
            except OSError:
                continue
    else:
        raise HeadlessUnavailable("no libEGL")
    sig = {"eglGetProcAddress": (_vp, [ctypes.c_char_p]),
           "eglInitialize": (ctypes.c_uint, [_vp, _vp, _vp]),
           "eglQueryString": (ctypes.c_char_p, [_vp, _int]),
           "eglBindAPI": (ctypes.c_uint, [ctypes.c_uint]),
           "eglChooseConfig": (ctypes.c_uint, [_vp, _vp, _vp, _int, _vp]),
           "eglCreateContext": (_vp, [_vp, _vp, _vp, _vp]),
           "eglMakeCurrent": (ctypes.c_uint, [_vp, _vp, _vp, _vp]),
           "eglDestroyContext": (ctypes.c_uint, [_vp, _vp]),
           "eglTerminate": (ctypes.c_uint, [_vp])}
    for fn, (res, args) in sig.items():
        f = getattr(egl, fn)
        f.restype, f.argtypes = res, args
    return egl


def _ints(*v):
    return (_int * len(v))(*v)


@contextmanager
def egl_context():
    """`with egl_context() as renderer:` an OpenGL 3.3 core context is current on this thread.
    Raises HeadlessUnavailable if one can't be made."""
    egl = _lib()
    dpy = ctx = None
    try:
        addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if not addr:
            raise HeadlessUnavailable("EGL has no platform displays")
        get_display = ctypes.CFUNCTYPE(_vp, ctypes.c_uint, _vp, _vp)(addr)
        dpy = get_display(EGL_PLATFORM_SURFACELESS_MESA, None, None)
        major, minor = _int(), _int()
        if not dpy or not egl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor)):
            dpy = None
            raise HeadlessUnavailable("no surfaceless EGL display")
        ext = (egl.eglQueryString(dpy, EGL_EXTENSIONS) or b"").decode()
        if "EGL_KHR_surfaceless_context" not in ext:
            raise HeadlessUnavailable("EGL can't make a context without a surface")
        if not egl.eglBindAPI(EGL_OPENGL_API):
            raise HeadlessUnavailable("EGL has no desktop OpenGL")
        # the config search wants window surfaces by default, which a surfaceless display has
        # none of: ask for pbuffer ones, or go without a config where EGL allows that
        cfg, n = _vp(), _int()
        attrs = _ints(EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_NONE)
        if not egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(n)) or n.value < 1:
            if "EGL_KHR_no_config_context" not in ext:
                raise HeadlessUnavailable("no OpenGL config")
            cfg = _vp()                                     # EGL_NO_CONFIG_KHR
        cattrs = _ints(EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 3,
                       EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT, EGL_NONE)
        ctx = egl.eglCreateContext(dpy, cfg, None, cattrs)
        if not ctx or not egl.eglMakeCurrent(dpy, None, None, ctx):
            raise HeadlessUnavailable("no OpenGL 3.3 context")
        from OpenGL.raw.GL.VERSION.GL_1_0 import glGetString
        renderer = glGetString(GL_RENDERER)
        if not renderer:
            raise HeadlessUnavailable("OpenGL calls don't reach the EGL context")
        renderer = ctypes.cast(renderer, ctypes.c_char_p).value.decode() \
            if not isinstance(renderer, bytes) else renderer.decode()
    except HeadlessUnavailable:
        _release(egl, dpy, ctx)
        raise
    except Exception as e:  # noqa: BLE001 (drivers fail in many ways)
        _release(egl, dpy, ctx)
        raise HeadlessUnavailable(f"{type(e).__name__}: {e}") from None
    try:
        yield renderer
    finally:
        _release(egl, dpy, ctx)


def _release(egl, dpy, ctx) -> None:
    try:
        if dpy:
            egl.eglMakeCurrent(dpy, None, None, None)
            if ctx:
                egl.eglDestroyContext(dpy, ctx)
            egl.eglTerminate(dpy)
    except Exception:  # noqa: BLE001 (tearing down)
        pass
