"""Optional SIMD-accelerated sqlite-vec build.

The ``sqlite-vec`` wheels on PyPI are compiled without SIMD on Linux —
upstream's Makefile only adds ``-mavx -DSQLITE_VEC_ENABLE_AVX`` on
Darwin x86_64 — so every KNN query computes its L2 distances with the
scalar loop. On brads-server (280K slots x 4096 dims) an AVX build of the
same tag cut the vector scan from 2.45 s to 1.65 s with an identical
top-50.

This module lets a host opt into such a build without changing the
Python dependency:

* ``memstem vec-accel build`` (:func:`build`) downloads the source tag
  matching the installed ``sqlite_vec`` package, compiles it with AVX,
  verifies it (:func:`probe`) and installs it atomically at
  :func:`default_path`.
* ``sqlite_vec_path`` in ``config.yaml`` (``auto`` or an explicit path)
  is turned into a loadable path by :func:`resolve`, which refuses the
  build unless the platform, CPU flags, version and a numeric sanity
  check all agree. Any refusal logs a warning and falls back to the
  bundled extension — acceleration is never a reason for the index not
  to open.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import shutil
import sqlite3
import string
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

logger = logging.getLogger(__name__)

#: SQLite would derive the init symbol from the file name
#: (``vec0-0.1.9-avx.so`` -> ``sqlite3_vec_init`` only by accident), so
#: the entry point is always passed explicitly.
ENTRYPOINT = "sqlite3_vec_init"

SOURCE_URL = "https://github.com/asg017/sqlite-vec/archive/refs/tags/v{version}.tar.gz"
#: Headers only (``sqlite3.h``/``sqlite3ext.h``); same amalgamation
#: upstream's ``scripts/vendor.sh`` uses. The loadable extension calls
#: SQLite through the runtime's API table, so the header version need
#: not match the Python runtime's SQLite.
AMALGAMATION_URL = "https://www.sqlite.org/2024/sqlite-amalgamation-3450300.zip"
AMALGAMATION_SHA256 = "ea170e73e447703e8359308ca2e4366a3ae0c4304a8665896f068c736781c651"
#: sha256 of the GitHub source tarball per sqlite-vec version. A version
#: not listed here needs ``allow_unpinned`` to build.
SOURCE_SHA256: dict[str, str] = {
    "0.1.9": "9823e737d9934dcbe85dff75d3fca81018a9beee803d70fa77b16faab5d61dc9",
}

CFLAGS = ("-O3", "-fPIC", "-shared", "-mavx", "-DSQLITE_VEC_ENABLE_AVX", "-Wl,-Bsymbolic")
#: sqlite-vec exports its internals (``vec0Filter_knn_chunks_iter``, ...)
#: and SQLite dlopens extensions RTLD_GLOBAL, so in a process that also
#: loaded the bundled build, whichever came first would serve the other's
#: hot paths — measured: the AVX build ran at stock speed. Export only the
#: entry points so the accelerated build always runs its own code.
VERSION_SCRIPT = """{
  global: sqlite3_vec_init; sqlite3_vec_numpy_init; sqlite3_vec_static_blobs_init;
  local: *;
};
"""

Fetch = Callable[[str], bytes]


class VecAccelError(RuntimeError):
    """A build or probe step failed; the message says which."""


@dataclass(frozen=True)
class Resolution:
    """Outcome of :func:`resolve`: what to load, and why."""

    requested: str | None
    path: Path | None  # None -> bundled (stock) extension
    reason: str


def installed_version() -> str:
    """The ``sqlite_vec`` package version, without a ``v`` prefix."""
    return str(sqlite_vec.__version__).lstrip("v")


def default_path(version: str | None = None) -> Path:
    """Where ``build`` installs and ``auto`` looks for the AVX build."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    ver = version or installed_version()
    return Path(base) / "memstem" / "sqlite-vec" / f"vec0-{ver}-avx.so"


def platform_supported() -> bool:
    return sys.platform.startswith("linux") and platform.machine().lower() in {"x86_64", "amd64"}


def cpu_has_avx(cpuinfo: Path = Path("/proc/cpuinfo")) -> bool:
    try:
        text = cpuinfo.read_text(errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("flags"):
            return "avx" in line.split(":", 1)[-1].split()
    return False


def _load_extension(db: sqlite3.Connection, path: Path) -> None:
    # ``Connection.load_extension(entrypoint=)`` is 3.12+; the SQL
    # function takes the entry point on every supported Python.
    db.enable_load_extension(True)
    try:
        db.execute("SELECT load_extension(?, ?)", (str(path), ENTRYPOINT))
    finally:
        db.enable_load_extension(False)


def load(db: sqlite3.Connection, path: Path | None) -> bool:
    """Load sqlite-vec into ``db``; the accelerated build when ``path`` is set.

    Returns True when the accelerated build was loaded. A failure to load
    it (file replaced or removed since :func:`resolve`) is logged and the
    bundled extension is loaded instead.
    """
    if path is not None:
        try:
            _load_extension(db, path)
            return True
        except sqlite3.Error as exc:
            logger.warning("vec-accel: could not load %s (%s); using bundled sqlite-vec", path, exc)
    db.enable_load_extension(True)
    try:
        sqlite_vec.load(db)
    finally:
        db.enable_load_extension(False)
    return False


def _sanity_vectors() -> list[tuple[bytes, bytes]]:
    # 64 dims exercise the AVX path (dims % 16 == 0); 37 the scalar tail.
    pairs = []
    for dims in (64, 37):
        a = [((i * 7919) % 97) / 97.0 - 0.5 for i in range(dims)]
        b = [((i * 104729) % 89) / 89.0 - 0.5 for i in range(dims)]
        pairs.append((struct.pack(f"{dims}f", *a), struct.pack(f"{dims}f", *b)))
    return pairs


def _distances(db: sqlite3.Connection) -> list[float]:
    out: list[float] = []
    for a, b in _sanity_vectors():
        row = db.execute(
            "SELECT vec_distance_l2(?, ?), vec_distance_cosine(?, ?)", (a, b, a, b)
        ).fetchone()
        out.extend(float(x) for x in row)
    return out


def probe(path: Path) -> str | None:
    """Verify an extension build; returns None if usable, else the reason.

    Loads it into a throwaway ``:memory:`` connection and checks that it
    reports the installed package's version and an AVX build, that its
    distances match the bundled build's, and that a vec0 KNN query
    returns the expected order.
    """
    if not path.is_file():
        return f"{path} does not exist"
    db = sqlite3.connect(":memory:")
    stock = sqlite3.connect(":memory:")
    try:
        try:
            _load_extension(db, path)
        except sqlite3.Error as exc:
            return f"load failed: {exc}"
        version, debug = db.execute("SELECT vec_version(), vec_debug()").fetchone()
        if str(version).lstrip("v") != installed_version():
            return f"version {version} != installed sqlite-vec v{installed_version()}"
        flags = next(
            (
                ln.split(":", 1)[1].split()
                for ln in str(debug).splitlines()
                if ln.startswith("Build flags")
            ),
            [],
        )
        if "avx" not in flags:
            return "not an AVX build (vec_debug build flags lack 'avx')"
        load(stock, None)
        got, want = _distances(db), _distances(stock)
        if any(abs(g - w) > 1e-4 * max(1.0, abs(w)) for g, w in zip(got, want, strict=True)):
            return f"distance mismatch vs bundled build: {got} != {want}"
        db.execute("CREATE VIRTUAL TABLE t USING vec0(embedding float[16])")
        for rowid in range(1, 5):
            vec = struct.pack("16f", *([float(rowid)] * 16))
            db.execute("INSERT INTO t(rowid, embedding) VALUES (?, ?)", (rowid, vec))
        query = struct.pack("16f", *([3.1] * 16))
        order = [
            r[0]
            for r in db.execute(
                "SELECT rowid FROM t WHERE embedding MATCH ? AND k = 4 ORDER BY distance", (query,)
            )
        ]
        if order != [3, 4, 2, 1]:
            return f"vec0 KNN returned {order}, expected [3, 4, 2, 1]"
    finally:
        db.close()
        stock.close()
    return None


_cache: dict[tuple[str | None, str, int, int], Resolution] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve(setting: str | None) -> Resolution:
    """Turn the ``sqlite_vec_path`` setting into what to load.

    ``None`` (default) -> bundled. ``"auto"`` -> :func:`default_path` if
    it exists, else bundled without a warning (the host simply has no
    build). Anything else is a path that must exist. Every accelerated
    candidate must pass the platform, CPU and :func:`probe` checks;
    failures log a warning and resolve to bundled. Cached per process
    (keyed on the file's identity, so a rebuilt file is re-probed).
    """
    if setting is None or not str(setting).strip():
        return Resolution(None, None, "sqlite_vec_path not set; bundled sqlite-vec")
    setting = str(setting).strip()
    auto = setting.lower() == "auto"
    candidate = default_path() if auto else Path(setting).expanduser()
    try:
        st = candidate.stat()
        key = (setting, str(candidate), st.st_mtime_ns, st.st_size)
    except OSError:
        reason = f"{candidate} not found; bundled sqlite-vec"
        if auto:
            logger.debug("vec-accel: %s", reason)
        else:
            logger.warning("vec-accel: %s", reason)
        return Resolution(setting, None, reason)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    if not platform_supported():
        why = f"platform {sys.platform}/{platform.machine()} unsupported (Linux x86_64 only)"
    elif not cpu_has_avx():
        why = "CPU lacks AVX"
    else:
        why = probe(candidate) or ""
    if why:
        logger.warning("vec-accel: not using %s: %s; bundled sqlite-vec", candidate, why)
        result = Resolution(setting, None, f"{why}; bundled sqlite-vec")
    else:
        result = Resolution(setting, candidate, "accelerated (avx) build verified")
    with _cache_lock:
        _cache[key] = result
    return result


def _fetch_url(url: str) -> bytes:
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as resp:
        data: bytes = resp.read()
    return data


def _check_sha(name: str, data: bytes, expected: str | None) -> None:
    if expected is None:
        return
    got = hashlib.sha256(data).hexdigest()
    if got != expected:
        raise VecAccelError(f"{name}: sha256 {got} != pinned {expected}")


def _render_header(template: str, version: str) -> str:
    major, minor, patch = [*version.split("-", 1)[0].split("."), "0", "0", "0"][:3]
    return string.Template(template).safe_substitute(
        VERSION=version,
        DATE="",
        SOURCE="memstem vec-accel build",
        VERSION_MAJOR=major,
        VERSION_MINOR=minor,
        VERSION_PATCH=patch,
    )


def build(
    dest: Path | None = None,
    *,
    version: str | None = None,
    allow_unpinned: bool = False,
    fetch: Fetch = _fetch_url,
    compiler: str | None = None,
) -> Path:
    """Download, compile, verify and atomically install an AVX build.

    Returns the installed path. Raises :class:`VecAccelError` on any
    failure; ``dest`` is only replaced by a build that passed
    :func:`probe`.
    """
    ver = (version or installed_version()).lstrip("v")
    if ver != installed_version():
        raise VecAccelError(
            f"requested v{ver} but the installed sqlite-vec package is v{installed_version()}; "
            "the build must match it"
        )
    target = dest or default_path(ver)
    if not platform_supported():
        raise VecAccelError(f"unsupported platform {sys.platform}/{platform.machine()}")
    if not cpu_has_avx():
        raise VecAccelError("this CPU does not report AVX")
    cc = compiler or os.environ.get("CC") or "gcc"
    cc_path = shutil.which(cc)
    if cc_path is None:
        raise VecAccelError(f"C compiler {cc!r} not found (install gcc / build-essential)")
    pinned = SOURCE_SHA256.get(ver)
    if pinned is None and not allow_unpinned:
        raise VecAccelError(
            f"no pinned source hash for v{ver}; pass --allow-unpinned to build anyway"
        )

    src_tgz = fetch(SOURCE_URL.format(version=ver))
    _check_sha("source tarball", src_tgz, None if allow_unpinned else pinned)
    amal = fetch(AMALGAMATION_URL)
    _check_sha("sqlite amalgamation", amal, None if allow_unpinned else AMALGAMATION_SHA256)

    with tempfile.TemporaryDirectory(prefix="memstem-vec-accel-") as tmp:
        work = Path(tmp)
        vendor = work / "vendor"
        vendor.mkdir()
        # Read only the members we need — no extractall of a remote archive.
        with tarfile.open(fileobj=io.BytesIO(src_tgz), mode="r:gz") as tf:
            files: dict[str, bytes] = {}
            for member in tf.getmembers():
                name = member.name.rsplit("/", 1)[-1]
                if member.isfile() and name in {"sqlite-vec.c", "sqlite-vec.h.tmpl"}:
                    fh = tf.extractfile(member)
                    if fh is not None:
                        files[name] = fh.read()
        if set(files) != {"sqlite-vec.c", "sqlite-vec.h.tmpl"}:
            raise VecAccelError(f"source tarball missing files (found {sorted(files)})")
        (work / "sqlite-vec.c").write_bytes(files["sqlite-vec.c"])
        (work / "sqlite-vec.h").write_text(_render_header(files["sqlite-vec.h.tmpl"].decode(), ver))
        with zipfile.ZipFile(io.BytesIO(amal)) as zf:
            for info in zf.infolist():
                name = info.filename.rsplit("/", 1)[-1]
                if name in {"sqlite3.h", "sqlite3ext.h"}:
                    (vendor / name).write_bytes(zf.read(info))
        if not (vendor / "sqlite3ext.h").is_file():
            raise VecAccelError("amalgamation zip missing sqlite3ext.h")

        exports = work / "exports.map"
        exports.write_text(VERSION_SCRIPT)
        out = work / "vec0.so"
        cmd = [
            cc_path,
            *CFLAGS,
            f"-Wl,--version-script={exports}",
            f"-I{vendor}",
            f"-I{work}",
            str(work / "sqlite-vec.c"),
            "-o",
            str(out),
            "-lm",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise VecAccelError(f"compile failed ({proc.returncode}): {proc.stderr[-2000:]}")
        why = probe(out)
        if why:
            raise VecAccelError(f"built extension failed verification: {why}")

        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copyfile(out, staged)
        os.chmod(staged, 0o755)
        # os.replace gives running daemons a new inode; their mapped copy
        # of the old file stays valid until they restart.
        os.replace(staged, target)
    clear_cache()
    return target
