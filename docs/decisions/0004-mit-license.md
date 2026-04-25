# ADR 0004: MIT License

Date: 2026-04-25
Status: Accepted

## Context

License options for an open-source AI memory project:

- **MIT** — most permissive, no patent grant
- **Apache 2.0** — permissive with explicit patent grant
- **AGPL-3.0** — copyleft (basic-memory's choice)
- **BSL / Elastic License** — source-available with commercial restrictions

## Decision

MIT.

## Rationale

1. **Maximum adoption.** No friction for any downstream use, including commercial.
2. **Matches AI ecosystem norms.** Most LLM/agent infra is MIT or Apache 2.0.
3. **No patent surface to protect** (yet — we're not building patentable inventions).
4. **Compatibility.** MIT is compatible with everything; we can pull in MIT, Apache 2.0, BSD code.
5. **Future managed-tier compatibility.** If we offer hosted Memstem later, MIT doesn't restrict it.

## Consequences

We give up:

- Patent retaliation clause (Apache 2.0 has this)
- Copyleft enforcement (AGPL would force forks to share modifications)

We gain:

- Maximum permissive adoption
- Simpler legal review for enterprise users

If we ever ship a commercial managed offering, we may dual-license or move to BSL for new components.
