# 0056 — Store each distinct vector once (vector dedupe)

- Status: proposed
- Date: 2026-11-18
- Supplements: 0036/0040 (vec compaction), 0045 (chunk reuse), 0048 (semantic deadline)

## Context

Every vector search is an exact brute-force KNN over the whole vec0 table,
so its cost is the table's size. On main-server (Ari vault, 2026-11-17
sizing) the table holds **224,472 vectors, of which only 144,405 are
distinct**: 80,067 (36%) are byte-for-byte copies of another row, and the
largest group has 1,517 copies. The July prototype measured the same share
(35%) on a 74K-vector vault, so this is structural, not a one-off.

The copies come from records whose chunks repeat exactly: headless
automation that sends the same prompt every run (1,370 sessions of one
nightly Codex job share chunks 0–3; 453 multi-model panel runs), and
session transcripts that begin with the same injected context. Identical
chunk text gives an identical vector under one embedding signature.

The cost shows up as cold-cache search. The vector chunks are 3.76 GB
(918K pages). Measured 2026-11-18 on a compacted copy, fadvise-evicted:
KNN k=50 **15.7 s cold, 0.9 s warm**. Cold reads hit the 15 s semantic
deadline (ADR 0048) and serve keyword-only results. VACUUM (2026-11-17)
made the pages contiguous but cannot shrink what is scanned.

## Decision

Keep one vec0 row per distinct vector; record every other chunk that has
the same vector as an **alias** of that row.

1. **Tables (schema v16, plain B-trees, idempotent):**
   - `vec_aliases(chunk_id PK, memory_id, chunk_index, canonical_chunk_id)`
     `WITHOUT ROWID`, indexed on `canonical_chunk_id`. A chunk is either a
     row in `memories_vec` (canonical) or a row here, never both.
   - `vec_canon(vec_hash PK, chunk_id UNIQUE)`: 16-byte sha256 prefix of the
     embedding blob → the canonical chunk holding it.

   `memories_vec` keeps its schema, so compaction (0036/0040), vec-accel and
   the health probes are unchanged.

2. **Writes.** Inserting a chunk vector looks up `vec_canon`; if the
   canonical row exists and its blob is **byte-equal** (hash collisions are
   therefore harmless), the chunk becomes an alias. Otherwise it is a new
   canonical row. Removing a canonical chunk that has aliases **promotes**
   one alias: a new vec0 row with the same blob, the other aliases
   re-pointed, `vec_canon` updated. Removing an alias deletes one B-tree row.
   Changing a chunk's vector is remove + insert, except a canonical chunk
   with no aliases whose new blob is new, which still updates in place
   (keeps 0045's slot reuse).

3. **Reads.** `query_vec` asks vec0 for the top `fetch_k` distinct vectors
   and expands each into its canonical chunk followed by its aliases
   (ordered by `chunk_id`), stopping at `fetch_k` chunk hits. Every distinct
   vector contributes at least one chunk, so this returns exactly the top
   `fetch_k` chunks by distance, as before. Only the order *among equal
   distances* changes, which was already arbitrary. The type filter runs
   after expansion, as before. Point lookups of a chunk's vector (MMR, 0045
   copy-reuse) resolve aliases to their canonical row.

4. **Existing indexes** are deduped by an explicit, resumable
   `memstem vec-dedupe` pass: keyset-paged over `memories_vec` on a reader,
   one short writer transaction per batch, each conversion re-verified
   inside its transaction (both rows still exist, blobs byte-equal). It
   frees vec0 slots, so it ends with a compaction (`--no-compact` to skip).
   `memstem vec-dedupe --undo` expands every alias back into a vec0 row; run
   it before downgrading to a release without this ADR.

5. **Compaction** also copies live rows missing from the build table
   (symmetric diff of the `_rowids` shadows). A promotion inserts a row
   without touching `embed_state`, which the existing delta step cannot see.

## Consequences

- Measured 2026-11-18 on a scratch copy of the main-server index (this
  branch's code, nothing live touched): 226,659 rows → **146,280 rows +
  80,379 aliases**; vector chunks 3.76 → **2.40 GB**; db file 5.0 → 3.65 GB
  after VACUUM. KNN k=50 **cold 15.7 → 10.0 s, warm 0.88 → 0.58 s**. For 20
  sample queries the top-50 distances were identical at every position
  (10 of 20 returned a different chunk among exact ties). The pass took
  674 s and the compaction 501 s, unpaused on an otherwise busy box.
- Dedupe alone brings cold search under the 15 s deadline but not near the
  1–3 s target; an in-memory float16 copy for exact KNN (prototype: 16 ms)
  is the follow-up that takes the scan off the page cache entirely.
- New duplicates are deduped on write, so the share stays at zero after the
  one-time pass.
- Search results are unchanged except for ties; no re-embedding, no
  embedding-API cost.
- Rollback: a release without this ADR still reads the index, but alias
  chunks are invisible to its KNN and its deletes orphan alias rows. Run
  `memstem vec-dedupe --undo` first (no re-embed needed).
- Not decided here: skipping embeddings for machine-generated repeat runs
  altogether (a content policy), and running the pass automatically as a
  hygiene stage for other tenants.
