# OpenClaw SQLite ingestion: verification, cutover and rollback

Native SQLite and plugin skill ingestion are opt-in. Installing this build alone
does not change the existing OpenClaw ingestion roots. Keep the compatibility
bridge running until parity has been checked and the operator approves cutover.

## Configuration

Merge these fields into the existing workspace's `layout`. Preserve its regular
skills, memory, extra-file, session-directory and other adapter settings.
Paths can be absolute or relative to the workspace.

```yaml
adapters:
  openclaw:
    agent_workspaces:
      - path: /path/to/openclaw-state
        tag: ari
        layout:
          skills_dirs: [skills]
          # Preserve existing session_dirs, including the bridge and archives.
          trajectory_sqlite_roots: [.]  # default: []
          trajectory_poll_seconds: 30
          plugin_skill_roots: [plugin-skills]  # default: []
          plugin_skill_allowed_roots:
            - /path/to/approved/openclaw/runtime-installations
            - /path/to/approved/openclaw/plugin-installations
```

A state root discovers `agents/*/agent/openclaw-agent.sqlite`, including agents
with retained sessions that are no longer configured in the runtime. Discovery
rejects database symlinks escaping the state root. Each database is opened with
`mode=ro` and `query_only`, with WAL visibility and a short read transaction.
The reader never changes OpenClaw's schema, retention settings or journal mode.

Plugin roots discover installed `SKILL.md` files through approved symlinks.
Allowed roots authorize destinations; they do not independently discover files.
Keep these roots as narrow as practical. A package version change inside an
approved installation root works without copying skills. A destination outside
those roots is rejected with a warning until explicitly approved. Cycles are
skipped; canonical file paths deduplicate aliases. Missing H1 titles fall back
to the frontmatter name or skill directory name. Codex's vendor `.system`
exclusion is unchanged.

## Verify before production cutover

Use a feature worktree and the project's Python environment. An editable
production install imports from its checkout, so do not edit that checkout.

```bash
ruff check .
ruff format --check .
mypy src/ tests/ scripts/
pytest --cov-report=xml
python scripts/verify_source_deletion.py
python scripts/verify_openclaw_sqlite.py \
  --state-root /path/to/openclaw-state --tag ari \
  --bridge-root /path/to/openclaw-state/memstem-feed \
  --plugin-root plugin-skills \
  --allow-plugin-root /path/to/approved/openclaw/runtime-installations \
  --allow-plugin-root /path/to/approved/openclaw/plugin-installations \
  --report /private/path/parity.json
```

The practical verifier reads the real databases, bridge and installed skills,
then exercises the pipeline and search in a throwaway vault. It checks retained
bridge text, canonical ID reuse, a stale bridge replay after native ingestion,
and repeated skill ingestion/search. It never writes the source files or live
vault. Check any session without bridge coverage separately. Native output can
contain additional text from intermediate snapshots; inspect differences instead
of treating equal byte counts as the only definition of parity.

Also verify recent `session` records from `openclaw`, `claude-code` and `codex`,
exact and semantic searches with `embedder_degraded: false`, and all configured
plugin skill names. Run the normal and independent CLI paths:

```bash
memstem search 'your exact memory title' --verbose
memstem search 'your conceptual query' --no-daemon --verbose
memstem doctor
memstem doctor embedder
```

Normal search should report `daemon-probe ... found=True` and `daemon-search`.
`--no-daemon` should report `direct-search` and never make a daemon request.
Run the site's local health probe as well. The new daemon answers
`/health?detail=false` without database diagnostics; the default `/health` still
reports full health. Older daemons remain supported through a bounded five-second
probe timeout and the same resolved-vault identity check.

## Approved cutover

1. Obtain approval to merge/deploy and to change the production configuration.
2. Back up the commented configuration and the canonical vault Markdown. Use a
   SQLite online backup for the live index; copying only `index.db` can omit WAL
   commits. Retain bridge JSONL and OpenClaw import archives. Capture current
   version, daemon identity, source counts and representative session IDs.
3. Merge the reviewed PR, install the approved build, add only the desired native
   and plugin fields, and restart the intended MemStem instance once. Leave the
   compatibility bridge enabled and preserve its existing `session_dirs`.
4. Repeat the practical audit and health/search checks. Confirm a fresh real
   conversation and plugin update are visible through the native poll. Include a
   database rotation/retention test on a staging copy. Watch the embed queue drain.
5. After an agreed observation period and separate approval to retire the bridge,
   disable its timer and remove only its feed directories from configuration.
   Keep the feed and archive files as recovery evidence; disabling a timer does
   not authorize deleting history. Update the canonical service ledger.

## History and operational limits

OpenClaw session UUIDs keep the existing `sessions/<id>.md` path and canonical ID
across SQLite, bridge and archive refs. The pipeline merges overlapping turns and
retains unmatched old turns. Shorter windows, reused sequence numbers and stale
bridge writes therefore cannot shrink previously captured conversation history.
Full reconcile can recover identity from Markdown even without `record_map`.
Other-source path collisions fail rather than overwrite records.

The incremental unit is a changed database/WAL, detected by inode, size and
nanosecond modification/change timestamps. Retained sessions are replayed rather
than trusting a high sequence cursor that a restore can invalidate. Full
reconcile replays even unchanged databases. Reads/parsing run off the event loop.
The per-session `max_trajectory_bytes` cap still applies; an oversized session,
unavailable source or retention gap is reported. A missing source does not
remove captured sessions. Configure `session_dirs` for archive JSONL that has
never been ingested; already-captured archives require no reimport.

Events pruned before any successful reader captured them cannot be recovered.
Polling must be faster than the effective upstream retention window. Turn merging
preserves content, but cannot infer the exact order of disjoint windows or tell
whether identical text in a reset is a new repetition. Retention also means an
upstream edit/redaction does not remove previously captured text; deliberate
vault deletion remains an operator action. Tool payloads and images remain
subject to the existing conversation parser's exclusions.

Query-log insertion briefly attempts a fresh immediate write transaction. If
concurrent ingestion owns the index writer, exact telemetry rows are atomically
queued under `_meta/query-log-pending/`, with private file permissions. The next
successful search/get log write replays up to 100 batches. Searches remain
independent of daemon availability. This queue is capped at 1,000 files / 16 MiB
and 512 KiB per batch. Disk errors, a full queue or malformed pending files still
warn. Replay is at-least-once around crashes between commit and file removal;
only noncanonical retrieval counts can be repeated. Logging disabled in the
configuration produces no pending files.

## Rollback

The supported immediate rollback is a **configuration rollback on this build**:
remove `trajectory_sqlite_roots`, restore the prior bridge session directories,
and restart the intended instance after approval. If the bridge timer was
retired, re-enable it after approval. Remove the plugin fields if those need to
be rolled back. Keep the canonical session Markdown and accumulated history;
there is no index schema migration to undo.

A package downgrade to 0.23.0 also removes the monotonic session merge. Its bridge
reader can overwrite native-enriched history with a shorter file. Before such a
downgrade, stop ingestion during an approved maintenance window, preserve the
complete current canonical vault separately, and verify the older input files
contain every retained turn. If that cannot be established, keep this build with
native ingestion disabled. Do not restore only a pre-cutover vault backup and
silently discard the conversations captured since it was taken.

Older packages ignore pending telemetry files. Retain them for replay by a fixed
build; they are not conversation history. Do not delete bridge feeds, import
archives or pending files as an incidental cleanup during rollback.
