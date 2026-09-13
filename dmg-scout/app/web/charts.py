"""Block 4B Item 1 (Master Plan v3.6 section 39): server-rendered SVG
charts, no JavaScript charting library. Pure string-building -- output is
plain, static SVG markup, so it renders identically in a browser, in the
browser's print dialog, and inside a weasyprint-rendered PDF (weasyprint
has no JS engine at all, which is exactly why this approach, not a JS
chart, is what "shared by the app and the PDFs" requires).

Both functions take already-computed values, never touch a database or a
model -- callers (app/web/main.py's Reports/Weekly Brief routes) do the
metric_snapshot reads and pass plain (label, value) data in.
"""
from __future__ import annotations

from html import escape


def sparkline_svg(values: list[float | None], width: int = 64, height: int = 20) -> str:
    """A minimal line sparkline with a dot on the last real point. None
    values are dropped, never interpolated across a real gap in history.
    Fewer than 2 real points (no real trend to draw -- see this
    function's own callers for "state plainly when history is shorter
    than twelve weeks") renders a dashed flat line instead of guessing a
    shape, and says so in its own aria-label."""
    points = [v for v in values if v is not None]
    if len(points) < 2:
        y = height / 2
        return (
            f'<svg class="chart-svg kpi-tile-spark" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="not enough history yet for a trend">'
            f'<line x1="2" y1="{y:.1f}" x2="{width - 2}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-width="1" stroke-dasharray="2,2" opacity="0.4"/></svg>'
        )
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    n = len(points)
    xs = [2 + i * (width - 4) / (n - 1) for i in range(n)]
    ys = [height - 2 - (v - lo) / span * (height - 4) for v in points]
    path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    return (
        f'<svg class="chart-svg kpi-tile-spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="trend over {n} periods">'
        f'<path d="{path_d}"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="1.8"/>'
        f'</svg>'
    )


def bar_chart_svg(items: list[tuple[str, float]], *, width: int = 480, height: int = 160,
                  action_labels: set[str] | None = None) -> str:
    """A horizontal bar chart from (label, value) pairs, in the exact
    order the caller gives them (never re-sorted here -- ranking is a
    display decision the caller already made). action_labels marks which
    bars render in the one "action needed" accent instead of the neutral
    one. Empty input renders nothing -- callers pair this with
    empty_state() for the sentence, not a blank chart."""
    if not items:
        return ""
    action_labels = action_labels or set()
    max_v = max((v for _, v in items), default=0) or 1.0
    row_h = height / len(items)
    bar_h = row_h * 0.55
    label_w = width * 0.32
    bars_w = width - label_w - 48
    parts = [f'<svg class="chart-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">']
    for i, (label, value) in enumerate(items):
        y = i * row_h + (row_h - bar_h) / 2
        bar_w = (value / max_v) * bars_w if max_v else 0.0
        cls = "bar action" if label in action_labels else "bar"
        safe_label = escape(str(label))
        parts.append(f'<text x="{label_w - 6:.1f}" y="{y + bar_h * 0.72:.1f}" text-anchor="end">{safe_label}</text>')
        parts.append(f'<rect class="{cls}" x="{label_w:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}"/>')
        parts.append(f'<text x="{label_w + bar_w + 6:.1f}" y="{y + bar_h * 0.72:.1f}">{value:g}</text>')
    parts.append("</svg>")
    return "".join(parts)
