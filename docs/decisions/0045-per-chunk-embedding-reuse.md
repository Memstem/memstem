# 0045 — Per-chunk embedding reuse and in-place vector updates

- Status: accepted
- Date: 2026-09-26
- Supplements: 0036/0039/0040/0041 (vec compaction), 0024 (embed_state)

## Context

A record is re-embedded whenever its body hash changes, and
`Index.upsert_vectors` replaced the record's vectors wholesale: delete every
row for the memory, insert one row per chunk. Two costs follow.

**Dead slots.** vec0 never reuses a deleted slot until its whole 1024-slot
chunk is empty, so every re-embed leaves one dead slot per old chunk. Live
agent sessions are rewritten on every settle and are re-embedded whole each
time. On brads-server on 2026-09-26, 468 re-embeds between 09:00Z and 19:00Z
wrote ~25.8K chunks (22.5K from sessions; one 1,910-chunk memory file alone),
and occupancy fell 0.83 → 0.755, about 2.6K dead slots an hour. Every KNN
scan reads every slot, dead or alive, until the nightly compaction gate
(ADR 0041) rebuilds the table. snape-server showed the same thing on
2026-09-11: 1,783 dead slots from one conversation in three hours (noted in
ADR 0041's consequences, never built).

**Wasted embedding calls.** A session that grew by one message had all its
chunks sent to the embedding API again, although `chunk_text` packs
paragraphs greedily from the start, so an append-only body keeps its
leading chunks byte-identical.

**A full-table scan per re-embed.** The wholesale delete was
`DELETE FROM memories_vec WHERE memory_id = ?`. `memory_id` is a vec0
metadata column, so this is a scan of the whole table (1.4 s on the
280K-slot live index, measured 2026-09-26), run under the writer lock on
every re-embed and every record delete.

## Decision

1. **Chunk hashes.** New table `vec_chunk_hashes(memory_id, chunk_index,
   chunk_hash)` (schema v15, `ON DELETE CASCADE` from `memories`) records the
   sha256 of each embedded chunk: the chunk text, or `img:` + data URL for an
   image media chunk (ADR 0025).
2. **Reuse, content-addressed, same signature only.** Before embedding, the
   worker hashes the new chunks. If the record's `embed_state.embed_signature`
   equals the worker's current signature, a new chunk whose hash matches an
   existing chunk of the same memory reuses that vector: untouched if it is
   at the same index, copied (point read by `chunk_id`) if it moved. Only the
   misses go to the embedder. A different signature, or no hashes (every
   record embedded before this change), embeds everything.
3. **In-place writes.** `Index.apply_vectors` updates existing rows with
   `UPDATE memories_vec SET embedding = ? WHERE chunk_id = ?` (vec0 0.1.9
   rewrites the slot in place and allocates nothing — verified), inserts
   rows past the old count, and deletes rows past the new count. The
   `chunk_id = "<memory_id>:<index>"` scheme is unchanged, so search, MMR's
   first-chunk lookup and compaction are unaffected.
4. **No more metadata scans.** A memory's vector rows are found through the
   `memories_vec_rowids` shadow table (a B-tree with a unique index on the
   `chunk_id` string) with a `[memory_id + ":", memory_id + ";")` range, then
   deleted or updated by primary key. This replaces every
   `DELETE ... WHERE memory_id = ?` in `Index`.
5. **Safety over reuse.** The hash table is advisory. A hash whose vector
   row no longer exists is a miss. If the rows change between planning and
   writing (another writer, a compaction swap), `apply_vectors` raises
   `StaleVectorPlanError`; the worker releases its claim and replans next tick
   without spending a retry. `upsert_vectors` (full rewrite, kept for callers
   that have no hashes) clears the record's hashes, so it can never leave a
   hash that describes a different vector.

## Consequences

- Rewriting a record no longer creates dead slots unless it shrinks (the
  tail rows are deleted). Growth only appends. Dead-slot growth should
  track shrinking bodies and deletions rather than every re-embed.
- An append to a session embeds only the chunks that changed: the last one
  and any new ones. An edit near the start of a body shifts the paragraph
  packing and still re-embeds most chunks. That is correct, just not cheaper.
- Compaction (ADR 0040) copies rows by `chunk_id`; hashes stay valid across
  a swap. Its delta pass keys on `embed_state.embedded_at`, which is still
  written after every embed, so in-place updates made during a build are
  carried over.
- A dimension change (`memstem reindex`) clears the hashes along with
  `embed_state`.
- One extra small table: roughly 100 bytes per chunk (~25 MB at 280K chunks).
