# Image sources

`hero.html` is the editable source for `../hero.png`. The original hero was
AI-image-generated (no editable source); this HTML/CSS rebuild replaced it so
the diagram can be kept accurate as the project evolves.

To re-render after editing:

```bash
cd docs/images/src && python3 -m http.server 8123 &
# headless Chromium (e.g. via Playwright), viewport exactly 1280x800:
#   page.setViewportSize({width: 1280, height: 800})
#   page.goto("http://localhost:8123/hero.html")
#   page.screenshot(path="../hero.png", scale="css")
```

Keep the diagram honest: only list adapters that actually ship (the dashed
card is for roadmap items), and keep the vault tree in sync with the real
layout (`memories/ skills/ sessions/ distillations/ daily/`).

`../hybrid-search.png` has no source file (AI-generated); its pipeline
(FTS5 BM25 + sqlite-vec cosine → RRF k=60) still matches `core/search.py` —
verify against the code before any regeneration.
