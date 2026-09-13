"""Block 4B Item 3 (Master Plan v3.6 section 39/40): PDF export via the
SAME HTML/CSS template the web page and print view already render --
"nothing designed twice." weasyprint has no JS engine at all (consistent
with section 39's own "no JavaScript charting" -- the SVG charts already
being pure markup is what makes this work with zero changes to them).

Fonts are NOT embedded here: the app's self-hosted @font-face rules
reference /static/fonts/*.woff2 by absolute path, and resolving those
would need this function to know the live server's own base URL (a
route-time concern, not this module's). A missing font resolves to
weasyprint's own generic fallback rather than failing the render --
disclosed here, not silently "fixed" by inventing a fake base_url that
would just as silently resolve to nothing.
"""
from __future__ import annotations

from pathlib import Path

STATIC_CSS_PATH = Path(__file__).resolve().parent / "static" / "app.css"


def inline_app_css() -> str:
    """The built Tailwind stylesheet's own text, read fresh every call
    (this is an occasional export action, not a hot path -- see
    app.pipeline.metrics.load_metrics_yaml for the same rather-re-read-
    than-risk-a-stale-cache reasoning)."""
    return STATIC_CSS_PATH.read_text()


def render_html_to_pdf(html: str) -> bytes:
    """html must already be a complete, standalone document (its own
    <style> with the inlined stylesheet, not a fragment) -- callers
    build that via a dedicated *_pdf.html template, never this page's
    own base.html-wrapped screen version."""
    from weasyprint import HTML

    return HTML(string=html).write_pdf()
