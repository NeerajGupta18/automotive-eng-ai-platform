"""Software-in-the-loop (SIL): the production C code (firmware/aeb) compiled into a shared library and
driven through ctypes with exactly the same interface as the Python reference model.

  * builds are cached by a hash of the sources, compiler flags and platform, so a library is only
    rebuilt when the code changes; the hash is recorded in every campaign as evidence of what was tested
  * libraries are loaded only from build/sil in the code directory (render.yaml and the Dockerfile build
    them during deployment), an operator-set AEB_SIL_DIR or a private per-process temp directory, never
    from the data directory; without a library and without a C compiler, SIL is reported as
    unavailable and campaigns run on the model only
  * only sources from the repository (and fixed, predefined mutations of them) are ever compiled; the
    API never compiles code supplied by a user"""
from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.modules.adas.model import HIST_LEN, Calibration

ROOT = Path(__file__).resolve().parents[3]
FIRMWARE = ROOT / "firmware" / "aeb"
C_FILE, H_FILE = "aeb_ctrl.c", "aeb_ctrl.h"
CFLAGS = ["-std=c99", "-O2", "-Wall", "-Wextra", "-fPIC", "-shared"]


class SilUnavailable(RuntimeError):
    pass


class CalT(ctypes.Structure):
    _fields_ = [("v_min_mps", ctypes.c_float), ("v_max_mps", ctypes.c_float), ("confirm_cycles", ctypes.c_uint16),
                ("closing_min_mps", ctypes.c_float), ("release_closing_mps", ctypes.c_float),
                ("ttc_warn_s", ctypes.c_float), ("ttc_partial_s", ctypes.c_float), ("ttc_full_s", ctypes.c_float),
                ("areq_warn_mps2", ctypes.c_float), ("areq_full_mps2", ctypes.c_float), ("a_tgt_min_mps2", ctypes.c_float),
                ("a_partial_mps2", ctypes.c_float), ("a_full_mps2", ctypes.c_float), ("a_hold_mps2", ctypes.c_float),
                ("stop_gap_m", ctypes.c_float), ("standstill_mps", ctypes.c_float), ("ttc_max_s", ctypes.c_float)]


class InT(ctypes.Structure):
    _fields_ = [("ego_speed_mps", ctypes.c_float), ("obj_valid", ctypes.c_uint8), ("distance_m", ctypes.c_float),
                ("closing_mps", ctypes.c_float), ("driver_brake_mps2", ctypes.c_float)]


class OutT(ctypes.Structure):
    _fields_ = [("brake_req_mps2", ctypes.c_float), ("aeb_req_mps2", ctypes.c_float), ("ttc_s", ctypes.c_float),
                ("a_req_mps2", ctypes.c_float), ("a_tgt_mps2", ctypes.c_float), ("fcw", ctypes.c_uint8),
                ("state", ctypes.c_uint8)]


class CtxT(ctypes.Structure):
    _fields_ = [("cal", CalT), ("confirm", ctypes.c_uint16), ("state", ctypes.c_uint8), ("hist_i", ctypes.c_uint8),
                ("hist_n", ctypes.c_uint8), ("hist", ctypes.c_float * HIST_LEN)]


assert [f[0] for f in CalT._fields_] == [f.name for f in dataclasses.fields(Calibration)], "C and Python calibration differ"


def sources(c_text: str | None = None) -> tuple[str, str]:
    return (c_text if c_text is not None else (FIRMWARE / C_FILE).read_text(encoding="utf-8"),
            (FIRMWARE / H_FILE).read_text(encoding="utf-8"))


def source_hash(c_text: str, h_text: str) -> str:
    h = hashlib.sha256()
    for part in (c_text, h_text, " ".join(CFLAGS), platform.system(), platform.machine()):
        h.update(part.encode("utf-8") + b"\0")
    return h.hexdigest()


def compiler() -> str | None:
    for name in (os.getenv("CC", ""), "cc", "gcc", "clang"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


_PRIVATE: Path | None = None


def _private_dir() -> Path:
    """Per-process directory only this process writes (mkdtemp creates it with mode 0700)."""
    global _PRIVATE
    if _PRIVATE is None:
        _PRIVATE = Path(tempfile.mkdtemp(prefix="aeai-sil-"))
    return _PRIVATE


def _dirs(cache_dir: Path | None) -> list[Path]:
    """Where libraries may be loaded from: an explicit cache, the operator's AEB_SIL_DIR, the code
    directory's build/sil (built with the deployment) and this process's private directory. Never the
    data directory: a restored backup or an upload must not be able to plant native code."""
    out = [Path(cache_dir)] if cache_dir else []
    if os.getenv("AEB_SIL_DIR"):
        out.append(Path(os.environ["AEB_SIL_DIR"]))
    out.append(ROOT / "build" / "sil")
    if _PRIVATE is not None:
        out.append(_PRIVATE)
    return out


def _out_dir(cache_dir: Path | None) -> Path:
    if cache_dir:
        return Path(cache_dir)
    if os.getenv("AEB_SIL_DIR"):
        return Path(os.environ["AEB_SIL_DIR"])
    d = ROOT / "build" / "sil"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _private_dir()
    return d if os.access(d, os.W_OK) else _private_dir()       # read-only container: private temp dir


def build(c_text: str | None = None, cache_dir: Path | None = None, *, timeout: int = 60) -> dict:
    """Compile (or reuse) the library for these sources. -> {path, hash, cached, warnings, compiler}"""
    c_text, h_text = sources(c_text)
    digest = source_hash(c_text, h_text)
    name = f"libaeb_{digest[:16]}.so"
    for d in _dirs(cache_dir):
        if (d / name).is_file():
            return {"path": str(d / name), "hash": digest, "cached": True, "warnings": "", "compiler": None}
    cc = compiler()
    if not cc:
        raise SilUnavailable("No C compiler (cc/gcc/clang) found and no prebuilt SIL library for this firmware version")
    out_dir = _out_dir(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / C_FILE).write_text(c_text, encoding="utf-8")
        (Path(tmp) / H_FILE).write_text(h_text, encoding="utf-8")
        lib_tmp = Path(tmp) / name
        try:
            r = subprocess.run([cc, *CFLAGS, "-o", str(lib_tmp), str(Path(tmp) / C_FILE)], capture_output=True,
                               text=True, timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired as e:
            raise SilUnavailable(f"Compilation timed out after {timeout} s") from e
        if r.returncode != 0:
            raise SilUnavailable(f"Compilation failed: {(r.stderr or r.stdout).strip()[:1500]}")
        final = out_dir / name
        part = out_dir / f".{name}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        shutil.copyfile(lib_tmp, part)
        os.chmod(part, 0o755)
        os.replace(part, final)                     # atomic: concurrent builds of the same source are safe
    return {"path": str(final), "hash": digest, "cached": False, "warnings": r.stderr.strip()[:1500], "compiler": cc}


def status(cache_dir: Path | None = None) -> dict:
    """Is SIL usable here? Never raises."""
    c_text, h_text = sources()
    digest = source_hash(c_text, h_text)
    prebuilt = any((d / f"libaeb_{digest[:16]}.so").is_file() for d in _dirs(cache_dir))
    cc = compiler()
    return {"available": prebuilt or cc is not None, "prebuilt": prebuilt, "compiler": cc, "firmware_hash": digest,
            "firmware_version": _version(c_text, h_text)}


def _version(c_text: str, h_text: str) -> str:
    for line in h_text.splitlines():
        if line.startswith("#define AEB_SW_VERSION"):
            return line.split('"')[1]
    return "unknown"


class SilController:
    """ctypes wrapper with the same reset()/step() interface as AebModel."""
    name = "sil"

    def __init__(self, lib_path: str, cal: Calibration | None = None):
        """cal=None: the C code's own default calibration (aeb_default_cal), so the defaults compiled into the
        firmware are what gets tested; pass a Calibration to test a calibration change."""
        self.lib = ctypes.CDLL(lib_path)
        self.lib.aeb_step.argtypes = [ctypes.POINTER(CtxT), ctypes.POINTER(InT), ctypes.POINTER(OutT)]
        self.lib.aeb_step.restype = None
        self.lib.aeb_init.argtypes = [ctypes.POINTER(CtxT), ctypes.POINTER(CalT)]
        self.lib.aeb_init.restype = None
        self.lib.aeb_ctx_size.restype = ctypes.c_uint32
        self.lib.aeb_version.restype = ctypes.c_char_p
        if self.lib.aeb_ctx_size() != ctypes.sizeof(CtxT):
            raise SilUnavailable(f"Struct layout mismatch: C aeb_ctx_t is {self.lib.aeb_ctx_size()} bytes, "
                                 f"Python CtxT {ctypes.sizeof(CtxT)} bytes; update sil.py with aeb_ctrl.h")
        self.lib.aeb_default_cal.argtypes = [ctypes.POINTER(CalT)]
        self.lib.aeb_default_cal.restype = None
        self.version = self.lib.aeb_version().decode()
        self.cal_c = CalT(**dataclasses.asdict(cal)) if cal is not None else None
        self.ctx, self.inp, self.out = CtxT(), InT(), OutT()
        self.reset()

    def default_calibration(self) -> dict:
        c = CalT()
        self.lib.aeb_default_cal(ctypes.byref(c))
        return {name: getattr(c, name) for name, _ in CalT._fields_}

    def reset(self) -> None:
        self.lib.aeb_init(ctypes.byref(self.ctx), ctypes.byref(self.cal_c) if self.cal_c is not None else None)

    def step(self, ego_speed: float, obj_valid: bool, distance: float, closing: float, driver_brake: float) -> dict:
        i, o = self.inp, self.out
        i.ego_speed_mps, i.obj_valid, i.distance_m, i.closing_mps, i.driver_brake_mps2 = (
            ego_speed, 1 if obj_valid else 0, distance, closing, driver_brake)
        self.lib.aeb_step(ctypes.byref(self.ctx), ctypes.byref(i), ctypes.byref(o))
        return {"brake_req": o.brake_req_mps2, "aeb_req": o.aeb_req_mps2, "ttc": o.ttc_s, "a_req": o.a_req_mps2,
                "a_tgt": o.a_tgt_mps2, "fcw": bool(o.fcw), "state": int(o.state)}
