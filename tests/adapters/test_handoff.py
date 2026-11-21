"""Handoff notes: discovery, one record per note across worktrees, in-place updates."""

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from memstem.adapters.base import Adapter, MemoryRecord
from memstem.adapters.handoff import HandoffAdapter
from memstem.cli import _build_handoff_adapter, _sweep_deleted_sources
from memstem.config import Config, HandoffRoot
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.storage import Vault

NOON = 1_794_571_200  # 2026-11-13 12:00:00 UTC


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Notes without a date in their name are dated in local time; pin it."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def write(path: Path, text: str, *, mtime: int = NOON) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def worktree(path: Path, main: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {main}/.git/worktrees/{path.name}\n")
    return path


def adapter(*roots: Path | HandoffRoot, **kwargs: object) -> HandoffAdapter:
    return HandoffAdapter(
        roots=[r if isinstance(r, HandoffRoot) else HandoffRoot(path=r) for r in roots],
        exclude_patterns=["*template*"],
        **kwargs,  # type: ignore[arg-type]
    )


def records(a: HandoffAdapter) -> dict[str, MemoryRecord]:
    async def run() -> list[MemoryRecord]:
        return [r async for r in a.reconcile([])]

    return {r.ref: r for r in asyncio.run(run())}


NOTE = "# New tab and windows\n\n## Decided\nKeep the tab bar.\n\n## Open\nWindow restore.\n"


def test_discovery_matches_names_and_prunes(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    kept = [
        write(root / "alpha" / "HANDOFF.md", NOTE),
        write(root / "alpha" / "docs" / "pre-handoff-gate3.md", NOTE + "gate"),
        write(root / "beta" / "Handoff-2026-08-11.MD", NOTE + "beta"),
    ]
    write(root / "alpha" / "HANDOFF-TEMPLATE.md", NOTE + "template")
    write(root / "alpha" / "notes.md", NOTE + "not a handoff")
    write(root / "alpha" / "HANDOFF.txt", NOTE + "not markdown")
    write(root / "alpha" / "empty-handoff.md", "\n\n")
    write(root / "alpha" / ".claude" / "worktrees" / "x" / "HANDOFF.md", NOTE + "hidden")
    write(root / "alpha" / "node_modules" / "pkg" / "HANDOFF.md", NOTE + "dependency")
    write(root / "alpha" / "backups" / "old" / "HANDOFF.md", NOTE + "backup")
    write(root / "alpha" / "a" / "b" / "c" / "HANDOFF.md", NOTE + "too deep")
    write(root / "alpha" / "big-handoff.md", "x" * 5000)

    found = records(adapter(root, exclude_dirs=["backups"], max_depth=3, max_file_bytes=4096))

    assert set(found) == {str(p) for p in kept}


def test_record_carries_project_and_date(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    path = write(
        root / "acme-ops" / "docs" / "HANDOFF-2026-11-13-new-tab-and-windows.md",
        "---\ntags: [ops]\n---\n" + NOTE,
        mtime=NOON + 3 * 86400,
    )

    record = records(adapter(root))[str(path)]

    assert record.source == "handoff"
    assert record.title == "acme-ops handoff 2026-11-13: New tab and windows"
    assert record.tags == ["ops", "handoff", "project:acme-ops"]
    assert record.metadata["type"] == "memory"
    assert record.metadata["created"] == "2026-11-13T00:00:00+00:00"
    assert record.metadata["updated"] == "2026-11-16T12:00:00+00:00"
    head, _, rest = record.body.partition("\n\n")
    assert head == f"Handoff note for project acme-ops, dated 2026-11-13. Source: {path}"
    assert rest == NOTE.strip()


def test_undated_name_uses_modification_date_and_falls_back_to_stem(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    write(root / "HANDOFF.md", "No heading here, only prose.")
    write(root / "westwind" / "handoff" / "Notes_Handoff_20260819.md", "Body without heading.")

    titles = sorted(r.title or "" for r in records(adapter(root)).values())

    assert titles == [
        "projects handoff 2026-11-13: HANDOFF",
        "westwind handoff 2026-08-19: Notes_Handoff_20260819",
    ]


def test_worktree_copies_are_one_note_and_newest_wins(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    main = repo(root / "app")
    write(main / "docs" / "HANDOFF.md", NOTE + "main copy", mtime=NOON)
    for n in range(3):
        write(worktree(root / f"app-wt-{n}", main) / "docs" / "HANDOFF.md", NOTE + "main copy")
    newest = worktree(root / "app-wt-fix", main)
    write(newest / "docs" / "HANDOFF.md", NOTE + "edited in the worktree", mtime=NOON + 60)
    # A note that only ever existed in a worktree still belongs to the main tree.
    write(newest / "HANDOFF-2026-11-12-fix.md", NOTE + "worktree only")

    found = records(adapter(root))

    assert set(found) == {
        str(main / "docs" / "HANDOFF.md"),
        str(main / "HANDOFF-2026-11-12-fix.md"),
    }
    record = found[str(main / "docs" / "HANDOFF.md")]
    assert record.body.endswith("edited in the worktree")
    assert record.title is not None and record.title.startswith("app handoff ")
    assert "project:app" in record.tags


def git(cwd: Path, *args: str, when: int | None = None) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_checkout_time_does_not_beat_a_later_commit(tmp_path: Path) -> None:
    """A fresh worktree of an old branch has the newest file time and the oldest text."""
    root = tmp_path / "projects"
    main = root / "app"
    main.mkdir(parents=True)
    git(main, "init", "-q", "-b", "main")
    write(main / "HANDOFF.md", NOTE + "Decided in September.")
    git(main, "add", "HANDOFF.md")
    git(main, "commit", "-q", "-m", "september", when=NOON - 60 * 86400)
    git(main, "branch", "old")
    write(main / "HANDOFF.md", NOTE + "Decided in November.", mtime=NOON)
    git(main, "commit", "-q", "-am", "november", when=NOON)
    stale = root / "app-wt-old"
    git(main, "worktree", "add", "-q", str(stale), "old")  # checked out just now
    assert (stale / "HANDOFF.md").stat().st_mtime > (main / "HANDOFF.md").stat().st_mtime

    a = adapter(root)
    (record,) = records(a).values()

    assert record.ref == str(main / "HANDOFF.md")
    assert record.body.endswith("Decided in November.")
    assert record.title == "app handoff 2026-11-13: New tab and windows"
    assert record.metadata["updated"] == "2026-11-13T12:00:00+00:00"

    # An uncommitted edit is dated by its file time, and wins.
    write(stale / "HANDOFF.md", NOTE + "Edited today, not committed.", mtime=NOON + 86400)
    (record,) = records(a).values()
    assert record.body.endswith("Edited today, not committed.")
    assert record.title == "app handoff 2026-11-14: New tab and windows"


def test_tie_prefers_the_identity_path(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    main = repo(root / "app")
    write(worktree(root / "aaa-wt", main) / "HANDOFF.md", NOTE + "worktree")
    write(main / "HANDOFF.md", NOTE + "main")

    a = adapter(root)

    assert a.scan()[str(main / "HANDOFF.md")].path == main / "HANDOFF.md"


def test_repository_above_the_root_only_names_root_level_notes(tmp_path: Path) -> None:
    home = repo(tmp_path / "home")  # e.g. a dotfiles repository
    root = home / "projects"
    loose = write(root / "HANDOFF.md", NOTE + "loose")
    nested = write(root / "client" / "reports" / "handoff-before-phase4.md", NOTE + "nested")

    found = records(adapter(root))

    assert set(found) == {str(loose), str(nested)}
    assert "project:home" in found[str(loose)].tags
    assert "project:client" in found[str(nested)].tags


def test_newest_only_root_is_one_rolling_note(tmp_path: Path) -> None:
    main = repo(tmp_path / "projects" / "shop")
    tree = worktree(tmp_path / "projects" / "shop-preview", main)
    private = tree / ".private"
    write(private / "queue-1-219" / "HANDOFF.md", NOTE + "older", mtime=NOON)
    write(private / "queue-1-220" / "HANDOFF.md", NOTE + "newer", mtime=NOON + 86400)
    write(private / "queue-1-220" / "brief.md", "not a handoff")

    a = adapter(tmp_path / "projects", HandoffRoot(path=private, newest_only=True))
    found = records(a)

    assert set(found) == {str(private / "HANDOFF.md")}
    record = found[str(private / "HANDOFF.md")]
    assert record.body.endswith("newer")
    assert record.title == "shop handoff 2026-11-14: New tab and windows"
    assert a.source_exists(str(private / "HANDOFF.md"))
    assert not (private / "HANDOFF.md").exists()


def test_edit_updates_the_record_in_place(tmp_path: Path) -> None:
    """The step this adapter exists for: an edited note replaces itself, no duplicate."""
    root = tmp_path / "projects"
    main = repo(root / "app")
    write(main / "HANDOFF.md", NOTE + "Decision: ship the tab bar on Friday.")
    tree = worktree(root / "app-wt", main)
    a = adapter(root)
    vault = Vault(tmp_path / "vault")
    index = Index(tmp_path / "index.db", dimensions=768)
    index.connect()
    try:
        pipeline = Pipeline(vault, index)
        (first,) = [pipeline.process(r) for r in records(a).values()]
        assert first is not None

        write(
            tree / "HANDOFF.md",
            NOTE + "Decision: ship the tab bar on Monday instead.",
            mtime=NOON + 3600,
        )
        (second,) = [pipeline.process(r) for r in records(a).values()]

        assert second is not None and second.id == first.id and second.path == first.path
        stored = vault.read(first.path)
        assert "Monday instead" in stored.body and "on Friday" not in stored.body
        assert stored.frontmatter.source == "handoff"
        assert str(first.path).startswith("memories/handoff/")
        assert len(list((tmp_path / "vault" / "memories" / "handoff").glob("*.md"))) == 1
        rows = index.db.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE source = 'handoff'"
        ).fetchone()
        assert rows["n"] == 1
        assert [h.memory_id for h in index.query_fts("Monday", limit=5)] == [str(first.id)]
        assert index.query_fts("Friday", limit=5) == []
    finally:
        index.close()


def test_sweep_keeps_a_note_while_any_copy_exists(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    main = repo(root / "app")
    tree = worktree(root / "app-wt", main)
    only_copy = write(tree / "HANDOFF.md", NOTE + "lives only in the worktree")
    a = adapter(root)
    adapters: dict[str, Adapter] = {a.name: a}
    vault = Vault(tmp_path / "vault")
    index = Index(tmp_path / "index.db", dimensions=768)
    index.connect()
    try:
        pipeline = Pipeline(vault, index)
        (memory,) = [pipeline.process(r) for r in records(a).values()]
        assert memory is not None
        assert not (main / "HANDOFF.md").exists()

        assert _sweep_deleted_sources(vault, index, adapters) == 0
        assert vault.read(memory.path).frontmatter.deleted_at is None

        only_copy.unlink()
        records(a)  # the periodic reconcile rescans before the sweep runs
        assert _sweep_deleted_sources(vault, index, adapters) == 1
        assert vault.read(memory.path).frontmatter.deleted_at is not None
    finally:
        index.close()


def test_source_exists_scans_when_nothing_was_scanned_yet(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    path = write(root / "alpha" / "HANDOFF.md", NOTE)

    a = adapter(root)

    assert a.source_exists(str(path))
    assert not a.source_exists(str(root / "alpha" / "GONE-HANDOFF.md"))
    assert a.source_roots() == [root]


async def test_watch_emits_only_what_changed(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    one = write(root / "alpha" / "HANDOFF.md", NOTE + "one")
    write(root / "beta" / "HANDOFF.md", NOTE + "two")
    a = adapter(root, poll_seconds=0.01)
    assert a.watcher_alive() is None
    stream = a.watch([])
    try:
        first = {(await anext(stream)).ref, (await anext(stream)).ref}
        assert first == {str(one), str(root / "beta" / "HANDOFF.md")}
        assert a.watcher_alive() is True
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(stream), timeout=0.2)
    finally:
        await stream.aclose()

    # A fresh watch on the same adapter: nothing changed, then one note is edited.
    stream = a.watch([])
    try:
        write(one, NOTE + "one, edited", mtime=NOON + 5)
        changed = await asyncio.wait_for(anext(stream), timeout=2)
        assert changed.ref == str(one) and changed.body.endswith("one, edited")
    finally:
        await stream.aclose()
    assert a.watcher_alive() is None


def test_unreadable_note_is_skipped_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    bad = root / "alpha" / "HANDOFF.md"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe not utf-8 \xff")
    good = write(root / "beta" / "HANDOFF.md", NOTE)

    assert set(records(adapter(root))) == {str(good)}


def test_missing_root_is_harmless(tmp_path: Path) -> None:
    assert records(adapter(tmp_path / "nowhere")) == {}


def test_opt_in_config(tmp_path: Path) -> None:
    cfg = Config(vault_path=tmp_path / "vault")
    assert _build_handoff_adapter(cfg) is None

    cfg = Config.model_validate(
        {
            "vault_path": str(tmp_path / "vault"),
            "adapters": yaml.safe_load(
                f"""
                handoff:
                  enabled: true
                  roots:
                    - {tmp_path}/projects
                    - path: {tmp_path}/projects/shop/.private
                      newest_only: true
                  exclude_dirs: [backups]
                  poll_seconds: 60
                """
            ),
        }
    )
    built = _build_handoff_adapter(cfg)
    assert built is not None
    assert [(r.path, r.newest_only) for r in built.roots] == [
        (tmp_path / "projects", False),
        (tmp_path / "projects" / "shop" / ".private", True),
    ]
    assert built.poll_seconds == 60 and "backups" in built.pruned
    assert built.patterns == ["*handoff*.md"] and built.exclude_patterns == ["*template*"]

    cfg.adapters.handoff.roots = []
    assert _build_handoff_adapter(cfg) is None
