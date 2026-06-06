"""Media helpers for multimodal ingestion (ADR 0025).

Detect local image references in a record body and render PDF pages to
images, so they can be embedded via :meth:`Embedder.embed_image` into the
same vector space as text. These are pure helpers — they read files and
return data, never touching the index or vault (adapter discipline; ADR 0002).

The PDF/image-encoding dependencies (``pypdfium2`` + ``pillow``) are optional
(the ``multimodal`` extra) and imported lazily; text-only installs are
unaffected.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from memstem.core.embeddings import image_bytes_to_data_url

# Markdown ``![alt](path)`` (path stops at whitespace or ``)``) and HTML <img>.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
_HTML_IMAGE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass(frozen=True)
class ImageRef:
    """A local image file referenced by a record body."""

    path: Path


def extract_image_refs(body: str, base_dir: Path) -> list[ImageRef]:
    """Return local image files referenced in ``body``.

    Recognises markdown ``![alt](path)`` and HTML ``<img src=...>``. Remote
    (``http(s)://``) and inline (``data:``) sources are skipped — only local,
    vault-resident images are embeddable. Paths resolve against ``base_dir``.
    De-duplicated, order-preserving.
    """
    refs: list[ImageRef] = []
    seen: set[Path] = set()
    for pattern in (_MD_IMAGE, _HTML_IMAGE):
        for match in pattern.finditer(body):
            raw = match.group(1).strip()
            if not raw or raw.startswith(("http://", "https://", "data:")):
                continue
            path = (base_dir / raw).resolve()
            if path.suffix.lower() not in IMAGE_EXTENSIONS or path in seen:
                continue
            seen.add(path)
            refs.append(ImageRef(path=path))
    return refs


def mime_for(path: Path) -> str:
    """Best-guess image MIME type from a file extension (defaults to PNG)."""
    return _MIME_BY_EXT.get(path.suffix.lower(), "image/png")


def image_file_to_data_url(path: Path) -> str:
    """Read a local image file and return it as a ``data:`` URL."""
    return image_bytes_to_data_url(path.read_bytes(), mime=mime_for(path))


def render_pdf_to_images(pdf_path: Path, dpi: int = 150) -> list[bytes]:
    """Render each page of a PDF to PNG bytes (one entry per page).

    Requires the optional ``multimodal`` extra (``pypdfium2`` + ``pillow``);
    raises :class:`RuntimeError` with an install hint when it's missing.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "PDF ingestion needs the 'multimodal' extra: pip install 'memstem[multimodal]'"
        ) from exc

    images: list[bytes] = []
    scale = dpi / 72.0
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for page in pdf:
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            images.append(buffer.getvalue())
    finally:
        pdf.close()
    return images
