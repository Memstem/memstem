# Secret handling in Memstem

This document describes how Memstem handles — and is designed to handle —
sensitive data: credentials, API keys, passwords, OAuth tokens, personally
identifiable information that flows through your assistant. It is intended
for customers and operators evaluating Memstem for use with confidential
workloads.

> **Status note (May 2026):** Memstem's secret-handling pipeline is being
> rolled out in phases. The architecture and the responsibility boundary
> described here are stable. Individual capabilities are marked with their
> current shipping state:
> **🟢 Shipped** | **🟡 In progress** | **⚪ Designed, not yet started**.
>
> When in doubt, the [in-scope / out-of-scope table](#what-is-and-is-not-in-scope)
> below is the authoritative description of what Memstem *commits* to
> doing — both now and after the in-progress work lands.

## TL;DR

- Memstem ships with **agent-side guidance** to put secrets in a vault, a
  **vault primitive** for storage, and **best-effort pattern matching on
  ingest** to redact known-format secrets before they enter the search
  index.
- The primary line of defense is your **assistant**: it is configured to
  use the vault automatically when handling secrets, so the secret never
  appears in chat transcripts or model context after the initial put.
- Memstem **does not guarantee detection of all secrets.** You remain
  responsible for not pasting raw secrets into chat unprompted. For
  maximum protection, store secrets in the vault directly before
  including related context in a session.

## What is and is not in scope

This is the authoritative responsibility boundary. It does not change
across releases; the implementation behind it ships in phases.

### In scope (Memstem provides — current + planned)

| Capability | Status | What it does |
|---|---|---|
| **Vault primitive** (`SecretBackend` interface) | ⚪ Designed | Stores secrets by name, returns them on demand to authorized callers. Multiple backends planned: `UltraVaultBackend` (default), `EnvBackend` (dev), `HashiCorpVaultBackend`, `AWSSecretsManagerBackend`, `NoopBackend`. |
| **Agent vault tools** | ⚪ Designed | `vault.put(name, value)` and `vault.get(name)` exposed to OpenClaw and Codex assistants, configured by default in the deployment template. |
| **System-prompt guidance** | 🟡 In progress | Default `AGENTS.md` / `CLAUDE.md` instruct the assistant to put credentials in the vault on first sight and refer to them by name thereafter. |
| **Ingest-time pattern scrub** | ⚪ Designed | When records enter the Memstem index, a regex pack matching known secret formats (AWS keys, GitHub tokens, OpenAI keys, JWT, OAuth bearer tokens, RSA private keys, etc.) runs. Hits are redacted to `{{vault:slug/key}}` placeholders and the underlying secret is moved to the configured backend. |
| **Audit log** | ⚪ Designed | Every secret detection writes a row to `_meta/index.db.secret_audit` with timestamp, customer slug, secret type, and source memory id. **Never** the secret value itself. |

### Out of scope (Memstem does not, and will not, provide)

| Limitation | Why |
|---|---|
| **Pre-flight scrubbing of outbound LLM API calls** | Memstem does not sit between your assistant and the LLM provider (OpenAI, Anthropic, etc.). If you paste a secret into chat, the LLM provider receives it before Memstem ever sees the message. Only the assistant's behavior controls that — not Memstem. |
| **Detection of secrets in arbitrary free-form text without pattern context** | Best-effort context-aware detection (e.g., *"my password is hunter2"* phrased in natural language) is offered as an opt-in premium tier that runs through a self-hosted model in your own VPC. Not enabled by default. Even when enabled, not guaranteed to find every instance. |
| **Real-time alerts when the assistant verbalizes a secret in its response** | Would require modifying the assistant runtime (Codex, Claude Code, etc.) to inspect every emitted token. Not implemented and not planned. |
| **Retroactive scrubbing of secrets already sent to third-party LLM providers** | Once a token has been transmitted to OpenAI or Anthropic, it has left the boundary Memstem can enforce. Memstem can scrub the record on its way into our index — it cannot reach into a vendor's logs. |

## What you should expect of yourself

The standard industry pattern for credential handling — followed by GitHub
secret scanning, AWS Secrets Manager, HashiCorp Vault, and others — is
that the **customer is responsible for putting secrets in the vault**, and
the **platform provides tooling plus best-effort detection as a backstop**.

Memstem follows that pattern. You should:

1. **Put secrets in the vault directly when you have a choice.** Don't
   paste an API key into chat and hope the assistant will figure it out —
   run `vault put openai-prod sk-...` (or the equivalent in your
   assistant's CLI) first, then refer to it by name in conversation.
2. **Trust the assistant to use the vault for secrets it encounters.**
   The default `AGENTS.md` instructs it to do exactly that. Don't bypass
   that instruction by asking the assistant to *"paste the full key back
   to me so I can copy it"* — that defeats the purpose.
3. **Treat the ingest-time scrub as a backstop, not a guarantee.** It
   catches the patterns it knows about. It will miss things that don't
   match a known pattern.

## How the layers fit together

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1 — Assistant (the only layer that prevents secrets   │
│  from reaching the LLM provider in the first place)          │
│                                                                │
│    AGENTS.md / system prompt: "Put secrets in the vault on   │
│    first encounter. Never echo secret values back in your    │
│    responses or generated code."                             │
│                                                                │
│    Tools: vault.put(name, value), vault.get(name)            │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼  (records flow into Memstem)
┌──────────────────────────────────────────────────────────────┐
│  Layer 2 — Ingest-time pattern scrub (defense in depth)      │
│                                                                │
│    Regex pack: AWS keys, GitHub tokens, OpenAI keys, JWT,    │
│    OAuth bearers, RSA private keys, …                        │
│                                                                │
│    On hit: write secret to vault, replace in body with       │
│    placeholder, log to audit.                                │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼  (records enter the search index)
┌──────────────────────────────────────────────────────────────┐
│  Layer 3 — Optional context-aware scan (premium, opt-in)     │
│                                                                │
│    Self-hosted LLM in your VPC scans hygiene-pipeline output │
│    for context-dependent leaks Layer 2 cannot catch with     │
│    regex. Same vault writeback flow.                         │
└──────────────────────────────────────────────────────────────┘
```

Layer 1 is the only layer that protects against secrets being transmitted
to your LLM provider. Layers 2 and 3 protect the Memstem index from
holding secrets in plaintext.

## Retrieval-time behavior

When a search hit returns a record containing `{{vault:slug/key}}`
placeholders:

- **Default:** placeholders are returned as-is. The caller sees the
  redacted text. This is the safe default.
- **Explicit resolve:** the caller may call
  `memstem.vault.resolve(memory_id, justification=...)` to fetch the
  resolved secret values. This requires a justification string and writes
  an audit row recording the access. Use this only when you have a
  concrete reason and accept the additional audit-trail surface.

Search ranking treats placeholder text as opaque — the BM25 and vector
scores reflect the redacted form, not the secret value.

## Vault backends

The default deployment ships with `UltraVaultBackend`, pointing at a
co-located vault container. To use your own:

```yaml
# _meta/config.yaml
secrets:
  backend: hashicorp        # or: aws-secrets-manager, env, noop, ultra-vault
  url: https://vault.example.com:8200
  namespace: my-tenant
  auth:
    method: token
    token_env: VAULT_TOKEN
```

See `docs/secret-backends.md` for the full backend reference.

## Reporting a detection miss

If you observe a credential pattern that Memstem failed to detect on
ingest, please open an issue with:

- The credential format (do not include real values)
- The provider it belongs to (AWS, GitHub, etc.)
- A redacted example showing the pattern shape

We will add the pattern to the default regex pack in the next release.

## What this document is not

This document describes what Memstem does and is committed to doing. It
is not:

- A compliance certification (Memstem is not SOC 2, HIPAA, PCI, or any
  other audited compliance scheme on its own — those depend on how you
  operate your overall environment).
- A guarantee of secret protection (see the in-scope / out-of-scope table
  above).
- Legal advice (consult counsel for jurisdiction-specific obligations
  around handling personal or sensitive data).
