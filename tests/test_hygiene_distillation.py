"""Tests for `memstem.hygiene.distillation` (ADR 0008 PR-D first slice).

Cover: deterministic clustering by topic tag and by daily-log ISO
week, the size threshold, the "already distilled" filter, the
non-mutation contract, and the CLI subcommand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from memstem.cli import app
from memstem.core.frontmatter import validate
from memstem.core.storage import Memory, Vault
from memstem.hygiene.distillation import (
    DEFAULT_MIN_CLUSTER_SIZE,
    DistillationCandidate,
    find_distillation_candidates,
)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    for sub in ("memories", "skills", "sessions", "daily", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return Vault(root)


def _make_memory(
    *,
    body: str,
    vault: Vault,
    tags: list[str] | None = None,
    type_: str = "memory",
    title: str | None = None,
    created: datetime | None = None,
    links: list[str] | None = None,
) -> Memory:
    metadata: dict[str, object] = {
        "id": str(uuid4()),
        "type": type_,
        "created": (created or datetime(2026, 4, 25, 15, 0, 0, tzinfo=UTC)).isoformat(),
        "updated": (created or datetime(2026, 4, 25, 15, 0, 0, tzinfo=UTC)).isoformat(),
        "source": "human",
        "title": title or "untitled",
        "tags": tags or [],
    }
    if links is not None:
        metadata["links"] = links
    fm = validate(metadata)
    if type_ == "memory":
        path = Path(f"memories/{fm.id}.md")
    elif type_ == "daily":
        path = Path(f"daily/{fm.created.date().isoformat()}-{str(fm.id)[:6]}.md")
    elif type_ == "distillation":
        path = Path(f"memories/distillations/{fm.id}.md")
    else:
        path = Path(f"memories/{fm.id}.md")
    memory = Memory(frontmatter=fm, body=body, path=path)
    vault.write(memory)
    return memory


class TestEmptyVault:
    def test_empty_vault_produces_no_candidates(self, vault: Vault) -> None:
        assert find_distillation_candidates(vault) == []


class TestTopicClusters:
    def test_single_topic_above_threshold_clusters(self, vault: Vault) -> None:
        for i in range(DEFAULT_MIN_CLUSTER_SIZE):
            _make_memory(
                body=f"cf note {i}",
                vault=vault,
                tags=["topic:cloudflare"],
                title=f"cf-{i}",
            )
        candidates = find_distillation_candidates(vault)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.cluster_id == "topic:cloudflare"
        assert c.kind == "topic"
        assert c.size == DEFAULT_MIN_CLUSTER_SIZE

    def test_topic_below_threshold_skipped(self, vault: Vault) -> None:
        # Three memories on a topic — below the 5-member threshold.
        for i in range(3):
            _make_memory(body=f"x{i}", vault=vault, tags=["topic:auth"])
        assert find_distillation_candidates(vault) == []

    def test_below_explicit_min_skipped(self, vault: Vault) -> None:
        # Even with a custom higher threshold, we filter correctly.
        for i in range(5):
            _make_memory(body=f"y{i}", vault=vault, tags=["topic:scale"])
        candidates = find_distillation_candidates(vault, min_cluster_size=10)
        assert candidates == []

    def test_agent_tag_does_not_create_topic_cluster(self, vault: Vault) -> None:
        # `agent:*` tags share across every memory in an agent's
        # workspace. We deliberately don't cluster on them.
        for i in range(8):
            _make_memory(body=f"r{i}", vault=vault, tags=["agent:ari"])
        assert find_distillation_candidates(vault) == []

    def test_multiple_topics_each_eligible(self, vault: Vault) -> None:
        for i in range(5):
            _make_memory(body=f"a{i}", vault=vault, tags=["topic:cloudflare"])
        for i in range(6):
            _make_memory(body=f"b{i}", vault=vault, tags=["topic:auth"])
        candidates = find_distillation_candidates(vault)
        cluster_ids = {c.cluster_id for c in candidates}
        assert cluster_ids == {"topic:cloudflare", "topic:auth"}


class TestDailyWeekClusters:
    def test_daily_logs_in_same_week_cluster(self, vault: Vault) -> None:
        # Five days in 2026-W17 (Apr 20-26). Default workspace tag.
        base = datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC)
        for i in range(5):
            _make_memory(
                body=f"daily {i}",
                vault=vault,
                type_="daily",
                tags=["agent:ari"],
                created=base.replace(day=base.day + i),
            )
        candidates = find_distillation_candidates(vault)
        # Expect one cluster: daily:ari/2026-W17
        cluster_ids = {c.cluster_id for c in candidates}
        assert any(cid.startswith("daily:ari/2026-W") for cid in cluster_ids)

    def test_daily_logs_split_across_weeks_dont_combine(self, vault: Vault) -> None:
        # Three logs in week 17, three in week 18 — neither hits 5.
        for d in (20, 21, 22):
            _make_memory(
                body="x",
                vault=vault,
                type_="daily",
                tags=["agent:ari"],
                created=datetime(2026, 4, d, 9, 0, 0, tzinfo=UTC),
            )
        for d in (28, 29, 30):
            _make_memory(
                body="y",
                vault=vault,
                type_="daily",
                tags=["agent:ari"],
                created=datetime(2026, 4, d, 9, 0, 0, tzinfo=UTC),
            )
        # No cluster of >=5 in any single week.
        candidates = find_distillation_candidates(vault)
        assert all(not c.cluster_id.startswith("daily:") for c in candidates)

    def test_daily_logs_separated_by_agent(self, vault: Vault) -> None:
        # Five for agent ari, five for agent sarah — both clusters
        # should appear separately.
        base = datetime(2026, 4, 20, 9, 0, 0, tzinfo=UTC)
        for i in range(5):
            _make_memory(
                body=f"a{i}",
                vault=vault,
                type_="daily",
                tags=["agent:ari"],
                created=base.replace(day=base.day + i),
            )
        for i in range(5):
            _make_memory(
                body=f"s{i}",
                vault=vault,
                type_="daily",
                tags=["agent:sarah"],
                created=base.replace(day=base.day + i),
            )
        candidates = find_distillation_candidates(vault)
        # Two clusters, both daily-week kind.
        daily = [c for c in candidates if c.kind == "daily-week"]
        agents = {c.cluster_id.split("/")[0] for c in daily}
        assert agents == {"daily:ari", "daily:sarah"}


class TestSkipExistingDistillations:
    def test_cluster_filtered_when_all_members_distilled(self, vault: Vault) -> None:
        # Build a topic cluster, then add a distillation that lists all members.
        members = [
            _make_memory(
                body=f"cf{i}",
                vault=vault,
                tags=["topic:cloudflare"],
            )
            for i in range(5)
        ]
        # The distillation links references members by id (the
        # `links` field uses path-like strings; we just put the bare
        # ids since the parser strips the `.md` extension and last
        # path segment.)
        _make_memory(
            body="rollup",
            vault=vault,
            type_="distillation",
            tags=["agent:ari"],
            links=[str(m.id) for m in members],
        )
        # Cluster is fully covered; should be filtered out.
        assert find_distillation_candidates(vault) == []

    def test_cluster_not_filtered_when_only_some_members_distilled(self, vault: Vault) -> None:
        # 5 members, only 3 covered by an existing distillation.
        members = [
            _make_memory(body=f"cf{i}", vault=vault, tags=["topic:cloudflare"]) for i in range(5)
        ]
        _make_memory(
            body="partial rollup",
            vault=vault,
            type_="distillation",
            tags=["agent:ari"],
            links=[str(members[0].id), str(members[1].id), str(members[2].id)],
        )
        candidates = find_distillation_candidates(vault)
        assert len(candidates) == 1
        assert candidates[0].cluster_id == "topic:cloudflare"

    def test_skip_filter_can_be_disabled(self, vault: Vault) -> None:
        members = [
            _make_memory(body=f"cf{i}", vault=vault, tags=["topic:cloudflare"]) for i in range(5)
        ]
        _make_memory(
            body="rollup",
            vault=vault,
            type_="distillation",
            tags=["agent:ari"],
            links=[str(m.id) for m in members],
        )
        candidates = find_distillation_candidates(vault, skip_already_distilled=False)
        assert len(candidates) == 1


class TestNoMutation:
    def test_walking_for_candidates_does_not_modify_vault(self, vault: Vault) -> None:
        # Walk before, walk after — every memory has the same body and
        # frontmatter. The candidate generator must be read-only.
        for i in range(5):
            _make_memory(body=f"cf{i}", vault=vault, tags=["topic:cloudflare"])
        before = sorted((m.frontmatter.id, m.body, tuple(m.frontmatter.tags)) for m in vault.walk())
        find_distillation_candidates(vault)
        after = sorted((m.frontmatter.id, m.body, tuple(m.frontmatter.tags)) for m in vault.walk())
        assert before == after


class TestCandidateShape:
    def test_member_lists_align(self, vault: Vault) -> None:
        # The three parallel lists (member_ids, member_paths,
        # member_titles) must have the same length and order.
        for i in range(5):
            _make_memory(
                body=f"x{i}",
                vault=vault,
                tags=["topic:test"],
                title=f"title-{i}",
            )
        candidates = find_distillation_candidates(vault)
        c = candidates[0]
        assert len(c.member_ids) == len(c.member_paths) == len(c.member_titles)
        assert c.size == 5

    def test_size_property_matches_member_count(self) -> None:
        c = DistillationCandidate(
            cluster_id="topic:x",
            kind="topic",
            rationale="x",
            member_ids=["a", "b", "c"],
            member_paths=["p1", "p2", "p3"],
            member_titles=["t1", "t2", "t3"],
        )
        assert c.size == 3


class TestCandidateOrder:
    def test_larger_clusters_listed_first(self, vault: Vault) -> None:
        # Three clusters of decreasing size — output should be
        # decreasing.
        for i in range(7):
            _make_memory(body=f"a{i}", vault=vault, tags=["topic:big"])
        for i in range(6):
            _make_memory(body=f"b{i}", vault=vault, tags=["topic:medium"])
        for i in range(5):
            _make_memory(body=f"c{i}", vault=vault, tags=["topic:small"])
        candidates = find_distillation_candidates(vault)
        sizes = [c.size for c in candidates]
        assert sizes == sorted(sizes, reverse=True)


class TestCli:
    def _vault_with_meta(self, tmp_path: Path) -> Path:
        root = tmp_path / "vault"
        for sub in ("memories", "skills", "sessions", "daily", "_meta"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "_meta" / "config.yaml").write_text(f"vault_path: {root}\n", encoding="utf-8")
        return root

    def test_no_candidates_message(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "distill", "--vault", str(root)])
        assert result.exit_code == 0
        assert "no distillation candidates" in result.stdout

    def test_lists_candidates(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        for i in range(5):
            _make_memory(
                body=f"cf{i}",
                vault=v,
                tags=["topic:cloudflare"],
                title=f"cf-{i}",
            )
        runner = CliRunner()
        result = runner.invoke(app, ["hygiene", "distill", "--vault", str(root)])
        assert result.exit_code == 0
        assert "1 candidate" in result.stdout
        assert "topic:cloudflare" in result.stdout
        assert "cf-0" in result.stdout

    def test_min_cluster_size_flag(self, tmp_path: Path) -> None:
        root = self._vault_with_meta(tmp_path)
        v = Vault(root)
        # 6 members — passes default 5, fails --min-cluster-size 10.
        for i in range(6):
            _make_memory(body=f"x{i}", vault=v, tags=["topic:tight"])
        runner = CliRunner()
        result_default = runner.invoke(app, ["hygiene", "distill", "--vault", str(root)])
        assert "1 candidate" in result_default.stdout
        result_strict = runner.invoke(
            app,
            ["hygiene", "distill", "--vault", str(root), "--min-cluster-size", "10"],
        )
        assert "no distillation candidates" in result_strict.stdout
