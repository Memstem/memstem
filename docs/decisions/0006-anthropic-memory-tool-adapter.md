# ADR 0006: Implement BetaAbstractMemoryTool

Date: 2026-04-25
Status: Accepted (planned for Phase 2)

## Context

Anthropic's official memory tool (`memory_20250818` and successors) is client-managed: the model emits tool calls (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) and the application's `BetaAbstractMemoryTool` implementation routes them to a backend.

The default backend in Anthropic's SDK is a local filesystem under `/memories`. Anyone can implement the abstract class to back it with anything.

## Decision

Implement `BetaAbstractMemoryTool` in Memstem so Claude Code's official memory tool routes natively into the Memstem vault.

## Rationale

1. **Killer integration story.** No other memory project can claim "Anthropic's official memory tool reads/writes our store directly."
2. **Forward compatibility.** As Anthropic evolves the memory tool API, we update one adapter — not every consumer.
3. **Replaces filesystem brittleness with API stability.** The memory tool API is more stable than session JSONL formats.
4. **Skill stack alignment.** Skills, memories, and Claude's working memory all live in one place.

## Consequences

**Pros:** flagship integration; future-proof; lets Claude Code use its native memory ergonomics.

**Cons:**

- Requires running the Memstem MCP server alongside Claude Code (already required for `memstem_search`)
- API surface tracks Anthropic's; may need updates as Anthropic versions tools

**Mitigation:** keep the implementation thin; wrap Memstem core operations.
