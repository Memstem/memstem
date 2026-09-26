"""Tests for the optional accelerated sqlite-vec build (core/vec_accel.py)."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec
import yaml
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core import vec_accel
from memstem.core.index import Index

STOCK_SO = Path(sqlite_vec.loadable_path() + ".so")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    vec_accel.clear_cache()
    yield
    vec_accel.clear_cache()


@pytest.fixture
def fake_build(tmp_path: Path) -> Path:
    """A loadable sqlite-vec file standing in for an accelerated build."""
    dest = tmp_path / "vec0-fake.so"
    shutil.copyfile(STOCK_SO, dest)
    return dest


def _as_verified(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Make platform/CPU/probe checks pass; returns the probed paths."""
    probed: list[Path] = []

    def fake_probe(path: Path) -> str | None:
        probed.append(path)
        return None

    monkeypatch.setattr(vec_accel, "platform_supported", lambda: True)
    monkeypatch.setattr(vec_accel, "cpu_has_avx", lambda *a: True)
    monkeypatch.setattr(vec_accel, "probe", fake_probe)
    return probed


# --- resolve --------------------------------------------------------------


def test_resolve_unset_is_bundled() -> None:
    res = vec_accel.resolve(None)
    assert res.path is None
    assert "not set" in res.reason


def test_resolve_auto_without_build_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="memstem.core.vec_accel"):
        res = vec_accel.resolve("auto")
    assert res.path is None
    assert "not found" in res.reason
    assert not caplog.records


def test_resolve_explicit_missing_path_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="memstem.core.vec_accel"):
        res = vec_accel.resolve(str(tmp_path / "nope.so"))
    assert res.path is None
    assert any("not found" in r.getMessage() for r in caplog.records)


def test_resolve_auto_uses_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_verified(monkeypatch)
    target = vec_accel.default_path()
    target.parent.mkdir(parents=True)
    shutil.copyfile(STOCK_SO, target)
    res = vec_accel.resolve("auto")
    assert res.path == target
    assert target.name == f"vec0-{vec_accel.installed_version()}-avx.so"


def test_resolve_refuses_without_avx(
    monkeypatch: pytest.MonkeyPatch, fake_build: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _as_verified(monkeypatch)
    monkeypatch.setattr(vec_accel, "cpu_has_avx", lambda *a: False)
    with caplog.at_level(logging.WARNING, logger="memstem.core.vec_accel"):
        res = vec_accel.resolve(str(fake_build))
    assert res.path is None
    assert "AVX" in res.reason
    assert caplog.records


def test_resolve_refuses_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, fake_build: Path
) -> None:
    _as_verified(monkeypatch)
    monkeypatch.setattr(vec_accel, "platform_supported", lambda: False)
    res = vec_accel.resolve(str(fake_build))
    assert res.path is None
    assert "platform" in res.reason


def test_resolve_caches_probe_until_file_changes(
    monkeypatch: pytest.MonkeyPatch, fake_build: Path
) -> None:
    probed = _as_verified(monkeypatch)
    assert vec_accel.resolve(str(fake_build)).path == fake_build
    assert vec_accel.resolve(str(fake_build)).path == fake_build
    assert len(probed) == 1
    st = fake_build.stat()
    os.utime(fake_build, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    vec_accel.resolve(str(fake_build))
    assert len(probed) == 2


# --- probe ------------------------------------------------------------------


def test_probe_rejects_the_bundled_non_simd_build() -> None:
    # The PyPI wheel is exactly what this feature exists to replace: right
    # version, loads fine, but no AVX in its build flags.
    reason = vec_accel.probe(STOCK_SO)
    assert reason is not None
    assert "AVX" in reason


def test_probe_rejects_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vec_accel, "installed_version", lambda: "9.9.9")
    reason = vec_accel.probe(STOCK_SO)
    assert reason is not None and "version" in reason


def test_probe_rejects_missing_and_unloadable(tmp_path: Path) -> None:
    assert "does not exist" in (vec_accel.probe(tmp_path / "x.so") or "")
    junk = tmp_path / "junk.so"
    junk.write_bytes(b"not an ELF")
    assert "load failed" in (vec_accel.probe(junk) or "")


def test_cpu_has_avx_parses_flags(tmp_path: Path) -> None:
    info = tmp_path / "cpuinfo"
    info.write_text("processor\t: 0\nflags\t\t: fpu sse2 avx avx2\n")
    assert vec_accel.cpu_has_avx(info)
    info.write_text("processor\t: 0\nflags\t\t: fpu sse2 avx512f\n")
    assert not vec_accel.cpu_has_avx(info)
    assert not vec_accel.cpu_has_avx(tmp_path / "missing")


# --- load / Index -----------------------------------------------------------


def test_load_falls_back_to_bundled(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import sqlite3

    db = sqlite3.connect(":memory:")
    with caplog.at_level(logging.WARNING, logger="memstem.core.vec_accel"):
        assert vec_accel.load(db, tmp_path / "gone.so") is False
    assert db.execute("SELECT vec_version()").fetchone()[0].startswith("v")
    assert caplog.records


def test_index_loads_the_resolved_extension(tmp_path: Path, fake_build: Path) -> None:
    idx = Index(tmp_path / "index.db", dimensions=4, vec_extension=fake_build)
    idx.connect()
    try:
        info = idx.sqlite_vec_info
        assert info is not None
        assert info["extension"] == str(fake_build)
        assert info["version"] == f"v{vec_accel.installed_version()}"
        with idx.reader() as reader:
            assert reader is not None
            assert reader.execute("SELECT vec_version()").fetchone()[0] == info["version"]
    finally:
        idx.close()


def test_index_defaults_to_bundled(tmp_path: Path) -> None:
    idx = Index(tmp_path / "index.db", dimensions=4)
    idx.connect()
    try:
        assert idx.sqlite_vec_info is not None
        assert idx.sqlite_vec_info["extension"] == "bundled"
    finally:
        idx.close()


# --- build ------------------------------------------------------------------


def _source_tgz(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in {
            "sqlite-vec.c": b"/* source */\n",
            "sqlite-vec.h.tmpl": b'#define SQLITE_VEC_VERSION "v${VERSION}"\n',
        }.items():
            info = tarfile.TarInfo(f"sqlite-vec-{version}/{name}")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _amalgamation() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sqlite-amalgamation-3450300/sqlite3.h", "/* h */")
        zf.writestr("sqlite-amalgamation-3450300/sqlite3ext.h", "/* ext */")
    return buf.getvalue()


@pytest.fixture
def offline_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake downloads + compiler: the 'compiler' copies the bundled .so."""
    ver = vec_accel.installed_version()
    tgz, amal = _source_tgz(ver), _amalgamation()
    monkeypatch.setitem(vec_accel.SOURCE_SHA256, ver, hashlib.sha256(tgz).hexdigest())
    monkeypatch.setattr(vec_accel, "AMALGAMATION_SHA256", hashlib.sha256(amal).hexdigest())
    monkeypatch.setattr(vec_accel, "platform_supported", lambda: True)
    monkeypatch.setattr(vec_accel, "cpu_has_avx", lambda *a: True)
    monkeypatch.setattr(vec_accel, "probe", lambda path: None)
    monkeypatch.setattr(shutil, "which", lambda cc: f"/usr/bin/{cc}")
    seen: dict[str, Any] = {"urls": []}

    def fetch(url: str) -> bytes:
        seen["urls"].append(url)
        return tgz if url.endswith(".tar.gz") else amal

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        workdir = Path(cmd[-4]).parent  # [..., src.c, "-o", out, "-lm"]
        seen["header"] = (workdir / "sqlite-vec.h").read_text()
        shutil.copyfile(STOCK_SO, cmd[-2])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    seen["fetch"] = fetch
    return seen


def test_build_installs_verified_output(tmp_path: Path, offline_build: dict[str, Any]) -> None:
    dest = tmp_path / "out" / "vec0.so"
    got = vec_accel.build(dest, fetch=offline_build["fetch"])
    assert got == dest and dest.is_file()
    cmd = offline_build["cmd"]
    assert "-mavx" in cmd and "-DSQLITE_VEC_ENABLE_AVX" in cmd
    assert any(a.startswith("-Wl,--version-script=") for a in cmd)
    assert f'"v{vec_accel.installed_version()}"' in offline_build["header"]
    assert not list(dest.parent.glob(".*.tmp"))


def test_build_refuses_hash_mismatch(tmp_path: Path, offline_build: dict[str, Any]) -> None:
    def tampered(url: str) -> bytes:
        data: bytes = offline_build["fetch"](url)
        return data + b"x" if url.endswith(".tar.gz") else data

    with pytest.raises(vec_accel.VecAccelError, match="sha256"):
        vec_accel.build(tmp_path / "vec0.so", fetch=tampered)
    assert not (tmp_path / "vec0.so").exists()


def test_build_refuses_unpinned_version(
    tmp_path: Path, offline_build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(vec_accel.SOURCE_SHA256, vec_accel.installed_version())
    with pytest.raises(vec_accel.VecAccelError, match="allow-unpinned"):
        vec_accel.build(tmp_path / "vec0.so", fetch=offline_build["fetch"])
    assert vec_accel.build(
        tmp_path / "vec0.so", fetch=offline_build["fetch"], allow_unpinned=True
    ).is_file()


def test_build_refuses_failed_verification(
    tmp_path: Path, offline_build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vec_accel, "probe", lambda path: "not an AVX build")
    with pytest.raises(vec_accel.VecAccelError, match="verification"):
        vec_accel.build(tmp_path / "vec0.so", fetch=offline_build["fetch"])
    assert not (tmp_path / "vec0.so").exists()


def test_build_needs_a_compiler(
    tmp_path: Path, offline_build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cc: None)
    with pytest.raises(vec_accel.VecAccelError, match="compiler"):
        vec_accel.build(tmp_path / "vec0.so", fetch=offline_build["fetch"])


def test_build_must_match_installed_version(tmp_path: Path) -> None:
    with pytest.raises(vec_accel.VecAccelError, match="installed"):
        vec_accel.build(tmp_path / "vec0.so", version="0.0.1")


@pytest.mark.skipif(
    not os.environ.get("MEMSTEM_TEST_VEC_ACCEL_BUILD") or shutil.which("gcc") is None,
    reason="real download + gcc build; set MEMSTEM_TEST_VEC_ACCEL_BUILD=1",
)
def test_real_build_is_accelerated(tmp_path: Path) -> None:  # pragma: no cover - opt-in
    path = vec_accel.build(tmp_path / "vec0-avx.so")
    assert vec_accel.probe(path) is None
    assert vec_accel.resolve(str(path)).path == path


# --- CLI --------------------------------------------------------------------


def test_cli_status_reports_resolution(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "_meta").mkdir(parents=True)
    (vault / "_meta" / "config.yaml").write_text(
        yaml.safe_dump({"sqlite_vec_path": str(tmp_path / "missing.so")})
    )
    result = CliRunner().invoke(app, ["vec-accel", "status", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "sqlite_vec_path: '" in result.output
    assert "loads: bundled" in result.output


def test_cli_build_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Path:
        raise vec_accel.VecAccelError("no gcc here")

    monkeypatch.setattr(vec_accel, "build", boom)
    result = CliRunner().invoke(app, ["vec-accel", "build", "--dest", str(tmp_path / "x.so")])
    assert result.exit_code == 1
    assert "no gcc here" in result.output
