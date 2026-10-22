#!/usr/bin/env python3
"""CLI entrypoint for the recall-quality eval harness.

Runs every query in a YAML query set against a Memstem vault and
prints MRR + Recall@K + per-class breakdown. Optional JSON dump for
diffing across runs (used by CI to gate recall-quality PRs).

Usage::

    scripts/run_eval.py                            # vault from default config
    scripts/run_eval.py --vault ~/memstem-vault \\
                        --queries eval/queries.yaml
    scripts/run_eval.py --json-out /tmp/eval.json  # save full report

    # ADR 0054: the vault's live ranking, recency on, Jev serving with the
    # prefer-recent prompt (Jev runs log to a throwaway ledger, not the vault's).
    scripts/run_eval.py --production-ranking --recency --jev prefer-recent

The eval never writes to ``query_log`` (passes ``log_client=None``) so
running it doesn't pollute live retrieval signals.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer
import yaml

from memstem.config import Config
from memstem.core.embeddings import Embedder, EmbeddingError, embed_for
from memstem.core.index import Index
from memstem.core.jev_shadow import build_jev_shadow
from memstem.core.recency import policy_from_config
from memstem.core.search import Search
from memstem.core.storage import Vault
from memstem.eval import format_report, load_queries, report_to_json, run_eval

logger = logging.getLogger(__name__)


def _load_config(vault_path: Path) -> Config:
    """Load `_meta/config.yaml` from the vault, with a sane default fallback.

    Mirrors the helper in :mod:`memstem.cli` so the eval CLI behaves
    identically to ``memstem search`` when both run against the same
    vault.
    """
    cfg_path = vault_path / "_meta" / "config.yaml"
    if not cfg_path.is_file():
        return Config(vault_path=vault_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return Config(vault_path=vault_path)
    raw.setdefault("vault_path", str(vault_path))
    return Config.model_validate(raw)


def _maybe_embedder(config: Config) -> Embedder | None:
    """Build the configured embedder; return None on failure (logged)."""
    try:
        return embed_for(config.embedding)
    except EmbeddingError as exc:
        logger.warning("embedder unavailable: %s", exc)
        return None
    except Exception as exc:  # connection refused, DNS, ...
        logger.warning("embedder unavailable: %s", exc)
        return None


def main(
    vault: Annotated[
        Path | None,
        typer.Option(
            "--vault",
            help="Vault root. Defaults to ~/memstem-vault.",
        ),
    ] = None,
    queries_path: Annotated[
        Path,
        typer.Option(
            "--queries",
            help="YAML query set to run. Defaults to eval/queries.yaml.",
        ),
    ] = Path("eval/queries.yaml"),
    json_out: Annotated[
        Path | None,
        typer.Option(
            "--json-out",
            help="Write the full report (per-query + aggregates) as JSON to this path.",
        ),
    ] = None,
    no_embedder: Annotated[
        bool,
        typer.Option(
            "--no-embedder",
            help="Run BM25-only (skip vec retrieval). Useful when Ollama is unreachable.",
        ),
    ] = False,
    production_ranking: Annotated[
        bool,
        typer.Option(
            "--production-ranking",
            help="Use the vault's search config (importance, type bias, MMR) instead of defaults.",
        ),
    ] = False,
    recency: Annotated[
        bool | None,
        typer.Option(
            "--recency/--no-recency",
            help="Force query-time recency on or off (ADR 0054). Default: the vault's config.",
        ),
    ] = None,
    recency_weight: Annotated[
        float | None, typer.Option("--recency-weight", help="Override recency weight.")
    ] = None,
    recency_half_life: Annotated[
        float | None, typer.Option("--recency-half-life", help="Override half-life in days.")
    ] = None,
    jev: Annotated[
        str,
        typer.Option(
            "--jev",
            help="off | base | prefer-recent. Serve Jev's order with that prompt; "
            "runs are logged to a temporary ledger, never the vault's.",
        ),
    ] = "off",
) -> None:
    """Run the recall-quality eval and print metrics."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if not queries_path.exists():
        typer.echo(f"ERROR: queries file not found: {queries_path}", err=True)
        raise typer.Exit(2)
    queries = load_queries(queries_path)
    if not queries:
        typer.echo("ERROR: query set is empty", err=True)
        raise typer.Exit(2)

    vault_path = vault or Path.home() / "memstem-vault"
    if not vault_path.exists():
        typer.echo(f"ERROR: vault not found: {vault_path}", err=True)
        raise typer.Exit(2)

    config = _load_config(vault_path)
    vault_obj = Vault(config.vault_path)
    db_path = config.index_path or config.vault_path / "_meta" / "index.db"
    index = Index(db_path, dimensions=config.embedding.dimensions)
    index.connect_readonly()
    embedder = None if no_embedder else _maybe_embedder(config)

    sc = config.search
    kwargs: dict[str, object] = {}
    if production_ranking:
        kwargs.update(
            rrf_k=sc.rrf_k,
            bm25_weight=sc.bm25_weight,
            vector_weight=sc.vector_weight,
            importance_weight=sc.importance_weight,
            type_bias=sc.type_bias,
            mmr_lambda=sc.mmr_lambda,
        )
    rc = sc.recency.model_copy()
    if recency is not None:
        rc.enabled = recency
    if recency_weight is not None:
        rc.weight = recency_weight
    if recency_half_life is not None:
        rc.half_life_days = recency_half_life
    kwargs["recency"] = policy_from_config(rc)

    shadow = None
    if jev not in ("off", "base", "prefer-recent"):
        typer.echo(f"ERROR: --jev must be off, base or prefer-recent, got {jev!r}", err=True)
        raise typer.Exit(2)
    if jev != "off":
        jc = sc.jev_shadow.model_copy(
            update={"enabled": True, "serve": True, "prefer_recent": jev == "prefer-recent"}
        )
        shadow = build_jev_shadow(jc, Path(tempfile.mkdtemp(prefix="memstem-eval-jev-")))
        if shadow is None:
            typer.echo("ERROR: Jev unavailable (no OpenRouter key?)", err=True)
            raise typer.Exit(2)
        kwargs["shadow_client"] = "eval"
    search = Search(vault=vault_obj, index=index, embedder=embedder, shadow=shadow)

    report = run_eval(search, queries, kwargs)
    typer.echo(format_report(report))

    if json_out is not None:
        json_out.write_text(json.dumps(report_to_json(report), indent=2, default=str))
        typer.echo(f"\nJSON report written to {json_out}")

    # Exit non-zero only when nothing matched at all — partial-fail
    # (some queries miss) is the normal "we have a recall problem to
    # fix" state and exits 0 so CI can capture the metrics rather than
    # block on them.
    if report.total > 0 and report.found == 0:
        sys.exit(1)


if __name__ == "__main__":
    typer.run(main)
