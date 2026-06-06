"""Tests for media helpers (ADR 0025 multimodal ingestion)."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

from memstem.core.media import (
    ImageRef,
    extract_image_refs,
    image_file_to_data_url,
    mime_for,
    render_pdf_to_images,
)

_HAS_PDFIUM = importlib.util.find_spec("pypdfium2") is not None

# A valid 1x1 transparent PNG.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class TestExtractImageRefs:
    def test_markdown_local_image(self, tmp_path: Path) -> None:
        (tmp_path / "shot.png").write_bytes(_PNG_1x1)
        refs = extract_image_refs("see ![cap](shot.png) here", tmp_path)
        assert refs == [ImageRef(path=(tmp_path / "shot.png").resolve())]

    def test_html_img_tag(self, tmp_path: Path) -> None:
        refs = extract_image_refs('<img src="a.jpg" alt="x">', tmp_path)
        assert [r.path.name for r in refs] == ["a.jpg"]

    def test_skips_remote_and_data_urls(self, tmp_path: Path) -> None:
        body = "![a](https://x.com/a.png) ![b](data:image/png;base64,AAA)"
        assert extract_image_refs(body, tmp_path) == []

    def test_skips_non_image_extensions(self, tmp_path: Path) -> None:
        assert extract_image_refs("![a](notes.txt)", tmp_path) == []

    def test_dedupes_and_preserves_order(self, tmp_path: Path) -> None:
        body = "![1](a.png) ![2](b.png) ![dup](a.png)"
        names = [r.path.name for r in extract_image_refs(body, tmp_path)]
        assert names == ["a.png", "b.png"]


class TestMimeAndDataUrl:
    def test_mime_for_known_and_default(self) -> None:
        assert mime_for(Path("x.jpg")) == "image/jpeg"
        assert mime_for(Path("x.PNG")) == "image/png"
        assert mime_for(Path("x.unknown")) == "image/png"

    def test_image_file_to_data_url_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "pic.png"
        path.write_bytes(_PNG_1x1)
        url = image_file_to_data_url(path)
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == _PNG_1x1


class TestRenderPdf:
    @pytest.mark.skipif(_HAS_PDFIUM, reason="pypdfium2 installed; missing-dep path not applicable")
    def test_missing_dependency_raises_helpful_error(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="multimodal"):
            render_pdf_to_images(tmp_path / "x.pdf")

    @pytest.mark.requires_pypdfium2
    def test_renders_each_page(self, tmp_path: Path) -> None:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        doc.new_page(144, 144)
        out = tmp_path / "doc.pdf"
        doc.save(str(out))
        doc.close()
        images = render_pdf_to_images(out, dpi=72)
        assert len(images) == 1
        assert images[0][:8] == b"\x89PNG\r\n\x1a\n"
