"""The standalone evaluation report.

One HTML file, no server, no network, no build step - open it from disk and
every chart is there. That constraint is the reason the charts are hand-written
inline SVG rather than a plotting library: a report that needs a CDN to render
is a report that stops rendering the moment it is emailed to somebody, and this
file is the Stage 3 artifact, meant to be looked at by people who will never
run the code.

Two charts carry the argument.

The **reliability diagram** is the one almost nobody ships. It plots predicted
confidence against observed frequency, before and after calibration, and it is
the difference between "the model says 0.95" and "the model says 0.95 and is
right 95% of the time". Everything downstream - the thresholds, the grey band,
the auto-accept rule - is only meaningful if that curve sits on the diagonal.

The **robustness curve** plots F1 against the corruption dial for each
strategy. It is the answer to "how much worse does this get when the data is
bad", which is the question that actually matters for exclusion files, and it
is where the learned weights visibly separate from the hand-tuned ones.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from concordance.eval.harness import OUTCOMES, EvalReport

# A small, deliberately flat palette: one accent for "after", one muted for
# "before", one for each baseline strategy. Chosen to stay legible on both a
# light and a dark background, since the report is read in whatever the reader
# happens to have.
MAX_CORRUPTION = 0.9

PALETTE = {
    "deterministic": "#8a7fbd",
    "fuzzy": "#c98b5e",
    "probabilistic": "#3f8f6f",
    "probabilistic_llm": "#2f6f9f",
    "before": "#b0623f",
    "after": "#2f7f68",
    "grid": "#c9c6c0",
}

CSS = """
:root {
  --bg: #faf9f7; --fg: #22201d; --muted: #6b665f; --line: #ddd9d3;
  --panel: #ffffff; --accent: #2f7f68; --warn: #b0623f;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16151a; --fg: #e8e5e0; --muted: #9b958c; --line: #2f2d33;
    --panel: #1e1d23;
  }
}
:root[data-theme="dark"] {
  --bg: #16151a; --fg: #e8e5e0; --muted: #9b958c; --line: #2f2d33;
  --panel: #1e1d23;
}
body { background: var(--bg); color: var(--fg); font: 15px/1.55 ui-sans-serif, -apple-system,
  "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding-block: 40px; padding-left: 20px;
  padding-right: 20px; max-width: 1100px; margin: 0 auto; }
h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.02em; }
h2 { font-size: 19px; margin: 44px 0 12px; letter-spacing: -0.01em; }
h3 { font-size: 15px; margin: 24px 0 8px; color: var(--muted); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em; }
p.sub { color: var(--muted); margin: 0 0 8px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0 8px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; min-width: 150px; flex: 1 1 150px; }
.card .k { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.06em; }
.card .v { font-size: 25px; font-variant-numeric: tabular-nums; margin-top: 2px; }
.card .n { color: var(--muted); font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums;
  font-size: 14px; }
.scroll { overflow-x: auto; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.05em; }
tbody tr:hover { background: color-mix(in srgb, var(--fg) 4%, transparent); }
.charts { display: flex; flex-wrap: wrap; gap: 24px; }
.chart { flex: 1 1 420px; min-width: 0; }
svg { max-width: 100%; height: auto; display: block; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--muted); font-size: 13px;
  margin-top: 6px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; background: currentColor; margin-right: 6px; }
.note { background: var(--panel); border-left: 3px solid var(--warn); border-radius: 0 6px 6px 0;
  padding: 10px 14px; margin: 8px 0; color: var(--muted); font-size: 14px; }
.good { color: var(--accent); } .bad { color: var(--warn); }
footer { margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 13px; }
code { font: 13px/1.4 ui-monospace, "SF Mono", "Cascadia Mono", Menlo, monospace;
  background: color-mix(in srgb, var(--fg) 7%, transparent); padding: 1px 5px;
  border-radius: 4px; }
"""


def esc(value: Any) -> str:
    return html.escape(str(value))


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _num(value: float | None, places: int = 4) -> str:
    return "-" if value is None else f"{value:.{places}f}"


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


def reliability_svg(
    before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]], size: int = 380
) -> str:
    """Predicted confidence against observed frequency, before and after.

    The diagonal is perfection. A curve above it is a model that is less
    confident than it should be; below it, the dangerous direction, is a model
    that is more confident than it has earned.
    """
    pad = 42
    inner = size - pad - 14

    def point(x: float, y: float) -> tuple[float, float]:
        return pad + x * inner, size - pad - y * inner

    parts = [
        f'<svg viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="Reliability diagram: predicted confidence against observed frequency">'
    ]
    for i in range(11):
        t = i / 10
        gx, gy = point(t, 0)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{size - pad:.1f}" x2="{gx:.1f}" y2="{size - pad - inner:.1f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="0.5" opacity="0.4"/>'
        )
        _, hy = point(0, t)
        parts.append(
            f'<line x1="{pad}" y1="{hy:.1f}" x2="{pad + inner}" y2="{hy:.1f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="0.5" opacity="0.4"/>'
        )
    x0, y0 = point(0, 0)
    x1, y1 = point(1, 1)
    parts.append(
        f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
        f'stroke="currentColor" stroke-width="1" stroke-dasharray="4 4" opacity="0.45"/>'
    )

    for bins, colour in ((before, PALETTE["before"]), (after, PALETTE["after"])):
        populated = [b for b in bins if b.get("count")]
        if not populated:
            continue
        coords = [
            point(float(b["mean_predicted"]), float(b["observed_frequency"])) for b in populated
        ]
        path = " ".join(
            ("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2" '
            f'stroke-linejoin="round"/>'
        )
        total = sum(int(b["count"]) for b in populated) or 1
        for (x, y), b in zip(coords, populated, strict=True):
            # Radius carries the bin population, so a bin holding four records
            # cannot be mistaken for one holding fourteen hundred.
            r = 2.5 + 5.5 * (int(b["count"]) / total) ** 0.5
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="{colour}" '
                f'fill-opacity="0.85"><title>predicted {b["mean_predicted"]:.3f} · '
                f"observed {b['observed_frequency']:.3f} · n={b['count']}</title></circle>"
            )

    parts.append(
        f'<line x1="{pad}" y1="{size - pad}" x2="{pad + inner}" y2="{size - pad}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.5"/>'
        f'<line x1="{pad}" y1="{size - pad}" x2="{pad}" y2="{size - pad - inner}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.5"/>'
    )
    for i in (0, 5, 10):
        t = i / 10
        gx, _ = point(t, 0)
        parts.append(
            f'<text x="{gx:.1f}" y="{size - pad + 16}" text-anchor="middle" font-size="11" '
            f'fill="currentColor" opacity="0.6">{t:.1f}</text>'
        )
        _, gy = point(0, t)
        parts.append(
            f'<text x="{pad - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="currentColor" opacity="0.6">{t:.1f}</text>'
        )
    parts.append(
        f'<text x="{pad + inner / 2:.1f}" y="{size - 4}" text-anchor="middle" font-size="12" '
        f'fill="currentColor" opacity="0.75">predicted confidence</text>'
        f'<text x="12" y="{size - pad - inner / 2:.1f}" text-anchor="middle" font-size="12" '
        f'fill="currentColor" opacity="0.75" transform="rotate(-90 12 '
        f'{size - pad - inner / 2:.1f})">observed frequency</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def robustness_svg(
    series: dict[str, list[tuple[float, float]]],
    width: int = 560,
    height: int = 360,
    y_label: str = "F1",
) -> str:
    """F1 against the corruption dial, one line per strategy."""
    pad_l, pad_b, pad_t, pad_r = 46, 42, 12, 12
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_b - pad_t

    def point(x: float, y: float) -> tuple[float, float]:
        return pad_l + x * inner_w, pad_t + inner_h - y * inner_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Robustness curve: {esc(y_label)} against corruption level">'
    ]
    for i in range(6):
        t = i / 5
        _, gy = point(0, t)
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + inner_w}" y2="{gy:.1f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="0.5" opacity="0.4"/>'
            f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="currentColor" opacity="0.6">{t:.1f}</text>'
        )
    for i in range(10):
        t = i / 9
        gx, _ = point(t, 0)
        parts.append(
            f'<text x="{gx:.1f}" y="{height - pad_b + 16}" text-anchor="middle" font-size="11" '
            f'fill="currentColor" opacity="0.6">{i / 10:.1f}</text>'
        )

    for name, points in series.items():
        if not points:
            continue
        colour = PALETTE.get(name, "#888")
        ordered = sorted(points)
        # The dial runs 0.0 to 0.9, so it is rescaled onto the full axis.
        coords = [point(min(x / MAX_CORRUPTION, 1.0), y) for x, y in ordered]
        path = " ".join(
            ("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for (x, y), (cx, cy) in zip(coords, ordered, strict=True):
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colour}">'
                f"<title>{esc(name)} · corruption {cx:.1f} · {esc(y_label)} {cy:.4f}</title>"
                f"</circle>"
            )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + inner_h}" x2="{pad_l + inner_w}" '
        f'y2="{pad_t + inner_h}" stroke="currentColor" stroke-width="1" opacity="0.5"/>'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + inner_h}" '
        f'stroke="currentColor" stroke-width="1" opacity="0.5"/>'
        f'<text x="{pad_l + inner_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
        f'font-size="12" fill="currentColor" opacity="0.75">corruption level</text>'
        f'<text x="12" y="{pad_t + inner_h / 2:.1f}" text-anchor="middle" font-size="12" '
        f'fill="currentColor" opacity="0.75" transform="rotate(-90 12 '
        f'{pad_t + inner_h / 2:.1f})">{esc(y_label)}</text>'
        "</svg>"
    )
    return "".join(parts)


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _tally_rows(source: dict[str, Any]) -> list[list[str]]:
    rows = []
    for name, tally in sorted(source.items()):
        correct = tally["true_positives"] + tally["true_negatives"] + tally["ambiguous_correct"]
        rows.append(
            [
                f"<code>{esc(name)}</code>",
                str(tally["n"]),
                _num(tally["precision"]),
                _num(tally["recall"]),
                _num(tally["f1"]),
                str(tally["false_positives"]),
                str(tally["false_negatives"]),
                f"{correct}/{tally['n']}",
            ]
        )
    return rows


def _confusion_table(confusion: dict[str, dict[str, int]]) -> str:
    headers = ["expected \\ decided", *[str(o) for o in OUTCOMES]]
    rows = []
    for expected in OUTCOMES:
        row = confusion.get(str(expected), {})
        cells = [f"<code>{esc(expected)}</code>"]
        for decided in OUTCOMES:
            value = row.get(str(decided), 0)
            mark = "good" if expected is decided else ""
            cells.append(f'<span class="{mark}">{value}</span>')
        rows.append(cells)
    return _table(headers, rows)


def _weights_table(params: dict[str, Any]) -> str:
    """The fitted `log2(m/u)` tables - the model, in the form a human can argue with."""
    weights = params.get("weights_log2", {})
    rows = []
    for field_name in params.get("fields", []):
        row = weights.get(field_name, [])
        cells = [f"<code>{esc(field_name)}</code>"]
        for value in row:
            colour = "good" if value > 0 else "bad"
            cells.append(f'<span class="{colour}">{value:+.2f}</span>')
        cells.extend([""] * (7 - len(cells)))
        rows.append(cells)
    return _table(
        ["field", "level 0", "1", "2", "3", "4", "5"],
        rows,
    )


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


def render(
    report: EvalReport,
    config: dict[str, Any] | None = None,
    sweep: Sequence[dict[str, Any]] | None = None,
) -> str:
    """The whole report as one self-contained HTML document."""
    payload = report.as_dict()
    detail = payload["detail"]
    overall = detail["overall"]
    dataset = detail["dataset"]

    cards = [
        ("precision", _num(payload["precision"]), f"{overall['false_positives']} false positives"),
        ("recall", _num(payload["recall"]), f"{overall['false_negatives']} false negatives"),
        ("F1", _num(payload["f1"]), f"{overall['n']} records"),
        (
            "ECE",
            _num(payload["ece"]),
            f"Brier {_num(payload['brier'])}",
        ),
        (
            "blocking recall",
            _num(detail["blocking_recall"]),
            "ceiling on system recall",
        ),
        (
            "grey band",
            _pct(detail["grey_band_fraction"]),
            "routed to review",
        ),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{v}</div>'
        f'<div class="n">{esc(n)}</div></div>'
        for k, v, n in cards
    )

    # Reliability: prefer the fitted before/after pair from the config, which is
    # the honest comparison. Fall back to this run's own bins alone.
    before_bins: list[dict[str, Any]] = []
    after_bins: list[dict[str, Any]] = list(payload["reliability_bins"])
    threshold_rows: list[list[str]] = []
    weight_sections = ""
    if config:
        calibration = config.get("calibration", {})
        individual = calibration.get("individual", {})
        before_bins = list(individual.get("before", {}).get("bins", []))
        after_bins = list(individual.get("after", {}).get("bins", [])) or after_bins
        for kind, thresholds in sorted(config.get("thresholds", {}).items()):
            threshold_rows.append(
                [
                    f"<code>{esc(kind)}</code>",
                    _num(thresholds["t_auto_accept"]),
                    _num(thresholds["t_auto_reject"]),
                    _pct(thresholds["grey_band_fraction"]),
                    _num(thresholds["achieved_precision"]),
                    _num(thresholds["achieved_recall"]),
                    str(thresholds["n_holdout"]),
                    "yes" if thresholds.get("precision_target_met", True) else "<b>no</b>",
                ]
            )
        weight_sections = "".join(
            f"<h3>{esc(kind)} model &mdash; log2(m/u)</h3>{_weights_table(params)}"
            for kind, params in sorted(config.get("params", {}).items())
        )

    series: dict[str, list[tuple[float, float]]] = {}
    for row in sweep or []:
        level = row.get("corruption_level")
        if level is None:
            continue
        series.setdefault(str(row["strategy"]), []).append((float(level), float(row["f1"])))
    robustness = (
        f'<h2>Robustness</h2><p class="sub">F1 against the corruption dial, one line per '
        f"strategy. The gap between the learned weights and the hand-tuned ones is the "
        f"point of the chart.</p>"
        f'<div class="chart">{robustness_svg(series)}</div>'
        + '<div class="legend">'
        + "".join(
            f'<span style="color:{PALETTE.get(name, "#888")}">{esc(name)}</span>'
            for name in sorted(series)
        )
        + "</div>"
        if series
        else '<h2>Robustness</h2><div class="note">No sweep results found. Run '
        "<code>concordance report sweep</code> and regenerate this report to fill in the "
        "robustness curve.</div>"
    )

    notes = "".join(f'<div class="note">{esc(n)}</div>' for n in detail["notes"])
    adjudicator = detail.get("adjudicator") or {}
    if adjudicator.get("adjudicator") == "null":
        notes += (
            '<div class="note">The <code>probabilistic_llm</code> strategy ran with the null '
            "adjudicator: every grey-band record was left <code>AMBIGUOUS</code> and no model "
            "was called. Its numbers are therefore identical to <code>probabilistic</code> by "
            "construction, which is what makes the LLM stage measurable when it arrives at "
            "Stage 4.</div>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Concordance Evaluation</title>
<style>{CSS}</style>
</head>
<body>
<h1>Concordance &mdash; matching evaluation</h1>
<p class="sub">
  strategy <code>{esc(payload["strategy"])}</code> ·
  corruption <code>{esc(payload["corruption_level"])}</code> ·
  config <code>{esc(detail["config_id"] or "-")}</code> ·
  seed <code>{esc(detail["seed"])}</code>
</p>
<p class="sub">
  {esc(dataset.get("records", 0))} sanction records against
  {esc(dataset.get("providers", 0))} providers ·
  mean {esc(dataset.get("mean_candidates", 0))} candidates per record ·
  provider snapshot <code>{esc(str(dataset.get("provider_snapshot_hash", ""))[:12])}</code>
</p>
<div class="cards">{card_html}</div>
{notes}

<h2>Calibration</h2>
<p class="sub">
  A confidence number is only a business input if it is a probability. This is the
  measurement, on the holdout split the calibrator never saw.
</p>
<div class="charts">
  <div class="chart">
    {reliability_svg(before_bins, after_bins)}
    <div class="legend">
      <span style="color:{PALETTE["before"]}">before calibration</span>
      <span style="color:{PALETTE["after"]}">after isotonic</span>
    </div>
  </div>
  <div class="chart">
    <h3>thresholds</h3>
    {
        _table(
            [
                "model",
                "accept ≥",
                "reject &lt;",
                "grey band",
                "precision",
                "recall",
                "holdout n",
                "target met",
            ],
            threshold_rows,
        )
        if threshold_rows
        else '<div class="note">No fitted config supplied.</div>'
    }
    <h3>calibration metrics, this run</h3>
    {
        _table(
            ["model", "n", "ECE", "MCE", "Brier"],
            [
                [
                    f"<code>{esc(k)}</code>",
                    str(v["n"]),
                    _num(v["ece"]),
                    _num(v["mce"]),
                    _num(v["brier"]),
                ]
                for k, v in sorted(detail["calibration_by_model"].items())
            ],
        )
    }
  </div>
</div>

{robustness}

<h2>By model</h2>
<p class="sub">
  Individuals and organizations are fitted and scored by two independent models, so they
  are reported independently. One combined number would hide which of the two moved.
</p>
{
        _table(
            ["model", "n", "precision", "recall", "F1", "FP", "FN", "correct"],
            _tally_rows(detail["by_model"]),
        )
    }

<h2>By scenario</h2>
<p class="sub">
  Every record was generated <em>for</em> a scenario. An aggregate F1 says accuracy moved;
  this table says which capability moved it.
</p>
{
        _table(
            ["scenario", "n", "precision", "recall", "F1", "FP", "FN", "correct"],
            _tally_rows(detail["by_scenario"]),
        )
    }

<h2>Confusion</h2>
<p class="sub">
  Rows are the expected outcome, columns the decision. <code>AMBIGUOUS</code> is a correct
  answer in its own right, not a failure to decide.
</p>
{_confusion_table(detail["confusion"])}

<h2>Routing</h2>
<div class="charts">
  <div class="chart">
    <h3>route</h3>
    {
        _table(
            ["route", "records"],
            [[f"<code>{esc(k)}</code>", str(v)] for k, v in detail["routes"].items()],
        )
    }
  </div>
  <div class="chart">
    <h3>reason</h3>
    {
        _table(
            ["reason", "records"],
            [[f"<code>{esc(k)}</code>", str(v)] for k, v in detail["reasons"].items()],
        )
    }
  </div>
</div>

{
        f"<h2>Fitted weights</h2><p class='sub'>Log-likelihood ratio in bits per field and "
        f"agreement level, learned by EM rather than chosen. A positive number is evidence for "
        f"the pair; a negative one is evidence against. Levels run from 0 (missing, or the "
        f"weakest level the field has) upward.</p>{weight_sections}"
        if weight_sections
        else ""
    }

<h2>Latency</h2>
{
        _table(
            ["stage", "ms per record"],
            [
                [f"<code>{esc(k)}</code>", f"{v:.3f}"]
                for k, v in sorted(detail["latency_ms"].items())
            ],
        )
    }

<footer>
  Generated by <code>concordance report eval</code>. Self-contained: no network, no server,
  no build step. Charts are inline SVG.
</footer>
</body>
</html>
"""


def write_html(
    report: EvalReport,
    path: Path,
    config: dict[str, Any] | None = None,
    sweep: Sequence[dict[str, Any]] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(report, config, sweep), encoding="utf-8")
    return path


def load_sweep(path: Path) -> list[dict[str, Any]]:
    """Sweep rows from disk, or an empty list when no sweep has been run."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cells", payload) if isinstance(payload, dict) else payload
    return [r for r in rows if isinstance(r, dict) and "strategy" in r]
