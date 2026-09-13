from __future__ import annotations

from pathlib import Path

from memstem.adapters.openclaw import OpenClawAdapter
from memstem.adapters.plugin_skills import iter_plugin_skills
from memstem.config import OpenClawLayout, OpenClawWorkspace
from memstem.core.index import Index
from memstem.core.pipeline import Pipeline
from memstem.core.search import Search
from memstem.core.storage import Vault

NAMES = [
    "acp-router",
    "browser-automation",
    "obsidian-vault-maintainer",
    "voice-call",
    "wiki-maintainer",
]


def skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\n---\n# {name}\n\nUse {name} to perform this specialized task carefully and verify the resulting work."
    )
    return path


async def test_regular_and_plugin_skills_searchable_without_duplicates(
    tmp_path: Path, tmp_vault: Path
) -> None:
    packages = tmp_path / "installed"
    links = tmp_path / "plugin-skills"
    links.mkdir()
    regular = skill(tmp_path / "skills", "regular-ari-skill")
    for name in NAMES:
        path = skill(packages, name)
        (links / name).symlink_to(path.parent, target_is_directory=True)
    (links / "regular-alias").symlink_to(regular.parent, target_is_directory=True)
    ws = OpenClawWorkspace(
        path=tmp_path,
        tag="ari",
        layout=OpenClawLayout(
            plugin_skill_roots=[Path("plugin-skills")],
            plugin_skill_allowed_roots=[packages, tmp_path / "skills"],
        ),
    )
    adapter = OpenClawAdapter([ws])
    records = [r async for r in adapter.reconcile([])]
    assert len(records) == 6
    assert len({r.ref for r in records}) == 6
    index = Index(tmp_vault / "_meta/index.db", dimensions=4)
    index.connect()
    vault = Vault(tmp_vault)
    pipeline = Pipeline(vault, index)
    try:
        for record in records:
            pipeline.process(record)
        # Poll overlapping aliases twice; only six searchable canonical skills.
        for _ in range(2):
            for record in [r async for r in adapter._poll_external()]:
                pipeline.process(record)
        assert (
            index.db.execute("SELECT count(*) FROM memories WHERE type='skill'").fetchone()[0] == 6
        )
        for name in [*NAMES, "regular-ari-skill"]:
            results = Search(vault, index).search(name, types=["skill"], limit=10)
            assert sum(r.memory.frontmatter.title == name for r in results) == 1
        changed = skill(packages, NAMES[0])
        changed.write_text(changed.read_text() + "\nA newly installed plugin update is captured.")
        update = [r async for r in adapter._poll_external()]
        assert len(update) == 1 and "newly installed" in update[0].body
    finally:
        index.close()


def test_symlink_escape_cycles_and_explicit_approval(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    external = tmp_path / "outside"
    path = skill(external, "unapproved")
    (root / "escape").symlink_to(path.parent, target_is_directory=True)
    (root / "cycle").symlink_to(root, target_is_directory=True)
    ws = OpenClawWorkspace(
        path=tmp_path, tag="ari", layout=OpenClawLayout(plugin_skill_roots=[root])
    )
    assert list(iter_plugin_skills(ws)) == []
    ws.layout.plugin_skill_allowed_roots = [external]
    assert list(iter_plugin_skills(ws)) == [path]
    # A nested symlink to a file outside the approved destination stays denied.
    rogue = skill(tmp_path / "secret", "rogue")
    (external / "unapproved" / "nested").symlink_to(rogue.parent, target_is_directory=True)
    assert list(iter_plugin_skills(ws)) == [path]


async def test_frontmatter_only_skills_have_distinct_names(tmp_path: Path) -> None:
    for name in ("wiki-maintainer", "obsidian-vault-maintainer"):
        path = skill(tmp_path / "plugins", name)
        path.write_text(
            f"---\nname: {name}\n---\nDetailed instructions for {name} without an H1 heading."
        )
    ws = OpenClawWorkspace(
        path=tmp_path, tag="ari", layout=OpenClawLayout(plugin_skill_roots=[Path("plugins")])
    )
    records = [r async for r in OpenClawAdapter([ws]).reconcile([])]
    assert {r.title for r in records} == {"wiki-maintainer", "obsidian-vault-maintainer"}
