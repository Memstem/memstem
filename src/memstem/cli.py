"""Command-line interface for Memstem."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="memstem",
    help="Unified memory and skill infrastructure for AI agents.",
    no_args_is_help=True,
)


@app.command()
def init(vault_path: str = typer.Argument(..., help="Path to create the vault at")) -> None:
    """Initialize a new Memstem vault."""
    raise NotImplementedError("Phase 1: vault init")


@app.command()
def daemon() -> None:
    """Run the Memstem daemon (watchers + MCP server + hygiene worker)."""
    raise NotImplementedError("Phase 1: daemon")


@app.command()
def search(query: str = typer.Argument(..., help="Search query")) -> None:
    """Search the vault from the CLI."""
    raise NotImplementedError("Phase 1: CLI search")


@app.command()
def reindex() -> None:
    """Rebuild the index from the canonical markdown vault."""
    raise NotImplementedError("Phase 1: reindex")


@app.command()
def mcp() -> None:
    """Run as an MCP server on stdio."""
    raise NotImplementedError("Phase 1: MCP server")


if __name__ == "__main__":
    app()
