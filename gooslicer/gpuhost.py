"""The host's ROCm libraries, for the image model's GPU provider inside the Flatpak (11 §2,
Select by Picture → GPU) [GOOSLICER].

The Flatpak runtime has no ROCm. With `--filesystem=host-os:ro` the host's /usr is visible under
/run/host/usr, but pointing LD_LIBRARY_PATH there would let the host's copies of everything
(Qt, libstdc++, …) win over the runtime's. So the libraries are loaded here, by absolute path and
RTLD_GLOBAL, only where the runtime has no library of that name: once a name is loaded, a later
dlopen of it (by onnxruntime's provider, or by HIP at run time) gets the loaded one.
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
from pathlib import Path

HOST_LIBS = ("/run/host/usr/lib/x86_64-linux-gnu", "/run/host/usr/lib/x86_64-linux-gnu/migraphx/lib",
             "/run/host/usr/lib64", "/run/host/usr/lib", "/run/host/opt/rocm/lib",
             "/run/host/opt/rocm/lib/migraphx/lib")             # MIGraphX's own (reached by RUNPATH)
# what the MIGraphX provider needs, and what HIP and MIGraphX open later by name (not linked)
WANTED = ("libamdhip64.so.7", "libmigraphx_c.so.3")
LATER = ("libamd_comgr.so.*", "libhiprtc.so.7", "libhiprtc-builtins.so.7", "libmigraphx_gpu.so.*",
         "librocblas.so.5", "libhipblaslt.so.1", "libMIOpen.so.1", "librocm_smi64.so.1")
_MISSING = re.compile(r"(lib[^\s:/]+\.so[^\s:]*): cannot open shared object file")
_loaded: dict[str, str] = {}
_failed: str | None = None


def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists()


def _host_path(name: str) -> str | None:
    for d in HOST_LIBS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def _load(name: str, depth: int = 0) -> None:
    """Load `name`: the runtime's own if it has one, else the host's, loading whatever the
    host's needs and the runtime lacks first."""
    if name in _loaded or depth > 60:
        return
    try:
        ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)          # the runtime's (or already loaded)
        _loaded[name] = "runtime"
        return
    except OSError:
        pass
    path = _host_path(name)
    if path is None:
        raise OSError(f"{name} isn't in the runtime or on the host")
    for _ in range(200):
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            _loaded[name] = path
            return
        except OSError as e:
            m = _MISSING.search(str(e))
            if not m or m.group(1) in _loaded:
                raise
            _load(m.group(1), depth + 1)
    raise OSError(f"{name}: too many missing libraries")


def preload() -> str | None:
    """In the Flatpak, load the host's ROCm libraries the GPU provider needs. Returns None when
    done (or not in the Flatpak), else why it couldn't."""
    global _failed
    if not in_flatpak() or _loaded:
        return _failed
    if not os.path.isdir("/run/host/usr"):
        _failed = "the host system isn't visible to the Flatpak"
        return _failed
    try:
        for name in WANTED:
            _load(name)
        for pat in LATER:                                     # best effort: used later, if at all
            for d in HOST_LIBS:
                for p in sorted(glob.glob(os.path.join(d, pat))):
                    try:
                        _load(os.path.basename(p))
                    except OSError:
                        pass
                    break
    except OSError as e:
        _failed = str(e).splitlines()[0]
    return _failed


def loaded() -> dict[str, str]:
    return dict(_loaded)
