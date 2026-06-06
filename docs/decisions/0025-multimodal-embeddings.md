# ADR 0025: Multimodal embeddings — text + image/PDF in one vector space (Qwen3-VL)

Date: 2026-06-06
Status: Accepted

## Context

The shipped embedders (ADR 0009) are **text-only**, and the production fleet runs
`mxbai-embed-large-v1` (512-token, 1024-dim) on a T4. Two problems:

1. **512-token truncation.** Records are chunked at 2048 chars; dense chunks (code,
   JSON, tables) exceed 512 tokens and are silently truncated by the embedder's
   `auto_truncate`, degrading semantic recall. Validated live across the fleet
   2026-06-06 (every tenant has chunks over the limit).
2. **No image/document understanding.** MemStem is text-only end to end — no image or
   PDF handling in `adapters/`, `core/embeddings.py`, or the index. Screenshots,
   dashboards, quotes, and PDFs that carry real relevance are invisible to search.

Research (four independent passes) plus a **live validation on a g6/L4 24GB box** on
2026-06-06 converged on **`Qwen/Qwen3-VL-Embedding-8B`**:

- Apache-2.0, 32K context (kills the truncation problem), natively multimodal
  (text + images + screenshots + PDF-pages) into **one shared vector space**.
- Serves via **vLLM 0.22.1** (`vllm/vllm-openai@sha256:953d3a06…`) over the
  OpenAI-compatible `/v1/embeddings` endpoint — a drop-in for the existing
  `OpenAIEmbedder` provider.
- Verified: healthy text separation (margin 0.42), image input over HTTP via the
  `messages`/vision payload, screenshot→caption retrieval ranks correctly, 4096-dim
  output, fits the L4 24GB. Server-side Matryoshka (`dimensions` param) is rejected by
  vLLM; client-side truncation preserves ranking.

Full verdict + plan: RE-Shared Drive, *"MemStem Qwen3-VL Embedder — Verdict &
Implementation Plan"*.

## Decision (proposed)

### 1. Additive multimodal embedder interface
Extend the `Embedder` ABC (ADR 0009) **additively** — the text contract is unchanged:
- `supports_images: bool = False` class flag.
- `embed_image(image_data_url) -> vector` / `embed_images([...])`, default raising
  `EmbeddingError`; only multimodal backends override.
- `OpenAIEmbedder.embed_image` POSTs the OpenAI vision shape
  (`messages` → `image_url` data-URL) to `/embeddings`, returning a vector in the same
  space as text. Gated by `supports_images`.
- `EmbeddingConfig.supports_images` (default False). Qwen3-VL is configured as
  `provider: openai` + `supports_images: true` + `base_url: <vLLM>` — no new provider
  class, since vLLM is OpenAI-compatible.

### 2. Image + PDF ingestion
- Detect image attachments / image links in records; **render PDF pages to images**
  (pypdfium2) and embed page-images directly (no OCR — both external research passes
  favored native visual embedding).
- Store **both** extracted text chunks **and** page/image vectors (lexical + visual
  recall). Adapter discipline preserved (adapters emit normalized records; no index
  writes).

### 3. Storage & dims
- Image vectors share the existing `memories_vec` space (Qwen3-VL = one shared space).
- Moving off 1024-dim mxbai to 4096-dim Qwen3-VL is a **provider switch** per ADR 0009
  → full `reindex`/re-embed of the corpus. At our scale (~21k chunks) storing full
  4096-dim is ~340 MB — trivial; client-side truncation available if scale grows.

### 4. Ranking
- Cross-modal similarities sit on a lower magnitude than text↔text — rank/threshold
  **within-modality** or calibrate (interacts with ADR 0016 MMR + 0017 rerank).
- Optional **Qwen3-VL-Reranker-2B** as a multimodal reranker extending ADR 0017
  (separate VRAM — does not co-fit the 8B on 24GB).

### 5. Serving
- Pin vLLM to the tested digest; re-validate on upgrade (regression history: GH
  vLLM #33954 / Qwen3-VL-Embedding #59 on 0.14–0.15; 0.22.1 tests clean).

### Default unchanged
Per ADR 0001/0009 local-first: default stays text-only Ollama. Multimodal Qwen3-VL is
**opt-in per vault** via config. This ADR adds a capability; it does not change defaults.

## Resolved decisions (2026-06-06, Brad)

- **A. Image-as-record model → media-chunk of a parent record.** An image/screenshot is
  a chunk attached to the record it came from; a PDF becomes one record whose chunks are
  its page-images. Preserves provenance; reuses the existing chunk→vector machinery.
- **B. Cross-modal ranking → rank within-modality, normalize per-modality.** The reranker
  (when added) fuses across modalities. (Interacts with ADR 0016 / 0017.)
- **C. Reranker → later.** Qwen3-VL-Reranker-2B lands as its own PR after core multimodal
  ingestion.
- **D. PDF dependency → yes, optional + lazy.** `pypdfium2` (+ `pillow`) ship as a
  `multimodal` extra, imported only when embedding images/PDFs.

## Consequences

**Pros:** image + PDF search; eliminates the 512-token truncation; one shared space;
Apache-2.0 self-hosted; reuses the OpenAI-compatible embedder path.

**Cons:** dims change forces a full re-embed (ADR 0009 reindex); vLLM multimodal-embed
serving is recent (pin + monitor); cross-modal ranking adds complexity; PDF rendering
adds a dependency; a reranker adds VRAM.

## Alternatives considered

- **VLM OCR/caption → text → text-embedder.** Rejected as primary (both external
  research passes favored native visual embedding for text-heavy screenshots); keep as
  a fallback for pure-text-in-image if visual recall underperforms.
- **CLIP-family (Jina CLIP v2, SigLIP).** Rejected: weak long-text; Jina CLIP v2 is
  CC-BY-NC (non-commercial) — disqualified for the product.
- **Late-interaction (ColPali / ColNomic).** Best ViDoRe scores but multi-vector —
  breaks the single-vector `memories_vec` store. Optional per-tenant doc-heavy add-on
  only.
- **Stay text-only, just fix context window.** Solves truncation (e.g. Qwen3-Embedding
  text) but leaves images invisible — misses the stated goal.

## References

- ADR 0003 — SQLite + FTS5 + sqlite-vec
- ADR 0009 — pluggable embedders + embed queue (extended here)
- ADR 0016 — MMR diversification; ADR 0017 — cross-encoder rerank (cross-modal impact)
- RE-Shared Drive — *MemStem Qwen3-VL Embedder — Verdict & Implementation Plan* (2026-06-06)
- Live validation: g6.2xlarge / L4 24GB, vLLM 0.22.1, 2026-06-06
