# 0049 — OpenClaw scheduled-job sessions expire (closing the ADR 0011 cron gap)

Status: **Proposed**
Date: 2026-10-03
Supersedes: none
Related: 0011 (noise filter / `valid_to`), 0026 (source-deletion tombstone — sessions out of scope), 0029 (session de-index by age)

## Context

ADR 0011 set the policy that "heartbeats and cron output expire after 4 weeks". The rules it
shipped match *empty* heartbeat polls (`HEARTBEAT_OK`, `[heartbeat]`, `[OpenClaw heartbeat poll]`)
and cron-runner artifacts (`Running cron job:`, `__openclaw_*_dream__`). They do not match the
form OpenClaw scheduled jobs actually take: an isolated agent session whose first user turn is
`[cron:<job uuid> <job name>] <job prompt>`. Those sessions are ingested as ordinary
`type=session` records and never expire.

Measured on the Ari vault (2026-10-03): **7,970 of 13,476 session records (59%; 38% of all
records)** are scheduled-job runs — mostly Ari's heartbeats, then the fleet task monitor, report
jobs and health checks — accruing ~1,400–2,100 a month since April. They are 20.7 MB of 338 MB of
body text and 18,964 of 218,810 vector chunks (~9%). Their outcomes already live where people
look for them (emailed reports, the agent's daily log); the raw transcripts mostly crowd
operational queries.

ADR 0026 deliberately does not tombstone sessions when their source disappears, so deleting
sources is not a cleanup path for these.

## Decision

1. **Rule.** A record with `source == "openclaw"`, type `session`, whose body *starts* with
   `**User:** [cron:<uuid>` followed by whitespace or `]`, is classified
   `TAG_TRANSIENT` with kind `openclaw_scheduled_session`. Anchoring at the start of the body
   means a chat that merely mentions a cron job never matches; the UUID requirement rules out
   free-form `[cron:…]` text.
2. **Expiry is anchored to when the job ran.** `valid_to = created + N days` (new
   `NoiseDecision.expires_at`), not `now + N`. Re-ingests produce the same expiry, and a retro
   replay retires the backlog older than N days at once instead of granting it another N days.
   If `created` is missing, fall back to `now + N`.
3. **Opt-in per installation.** `adapters.openclaw.scheduled_session_ttl_days: int | null`
   (default `null` = rule off). The fleet changes nothing until each operator turns it on.
   Recommended value: `28`, matching ADR 0011.
4. **Backlog.** `memstem hygiene cleanup-retro --no-dedup --noise --noise-kind
   openclaw_scheduled_session` (dry-run by default, `--apply` to write) replays the same rule
   over existing records. `--noise-kind` is new and restricts the replay to named rule kinds so
   this cleanup does not also apply unrelated noise rules.

## Consequences

- Expired records stay in the vault and the index (`valid_to` is a frontmatter field). Default
  search hides them; `include_expired` still finds them; clearing `valid_to` restores them.
- Expiry alone does not remove vector chunks (search filters `valid_to` after the KNN scan, as
  ADR 0029 notes), so the ~9% scan saving needs a separate vector-strip step:
  `memstem hygiene strip-expired-vectors` (follow-up PR) drops their vec0 chunks but keeps the
  markdown, index row, FTS and `embed_state`, and the pipeline skips enqueueing already-expired
  records, so they are not re-embedded. Freed slots are reclaimed by `vec_compact`.
- Only the OpenClaw adapter's isolated cron sessions are covered. Scheduled-job transcripts that
  reach MemStem through another adapter (e.g. an OpenClaw Codex harness writing into
  `~/.codex`) keep their source's normal treatment.
