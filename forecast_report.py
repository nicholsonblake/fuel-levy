"""
TGP Forecast Dashboard — CEO-ready HTML report
Generates a visual dashboard showing current diesel TGP state,
model decomposition, scenario forecasts, and directional outlook.

Reads from forecast/latest.json and data/diesel_tgp_history.csv.
Outputs to reports/forecast.html (deployed via GitHub Pages).

Usage:
    python forecast_report.py
"""

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

FORECAST_DIR: Path = Path(__file__).parent / "forecast"
DATA_DIR: Path = Path(__file__).parent / "data"
REPORT_DIR: Path = Path(__file__).parent / "reports"
PREDICTION_LOG: Path = FORECAST_DIR / "prediction_log.csv"


def load_latest() -> dict:
    path = FORECAST_DIR / "latest.json"
    with open(path) as f:
        return json.load(f)


def load_tgp_history(days: int = 120) -> pd.DataFrame:
    path = DATA_DIR / "diesel_tgp_history.csv"
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    cutoff = df.index.max() - pd.Timedelta(days=days)
    return df[df.index >= cutoff]


def load_prediction_log() -> pd.DataFrame:
    if not PREDICTION_LOG.exists():
        return pd.DataFrame()
    return pd.read_csv(PREDICTION_LOG, parse_dates=["run_date", "data_date"])


def svg_trend_chart(
    history: pd.DataFrame,
    predicted_value: float,
    width: int = 700,
    height: int = 260,
) -> str:
    """Generate an SVG line chart of recent TGP with model prediction line."""
    values = history["diesel_tgp"].dropna().values
    dates = history.index
    n = len(values)
    if n < 2:
        return "<p>Insufficient data for chart</p>"

    y_min = min(values.min(), predicted_value) * 0.92
    y_max = max(values.max(), predicted_value) * 1.05
    y_range = y_max - y_min if y_max > y_min else 1

    pad_left = 55
    pad_right = 20
    pad_top = 20
    pad_bottom = 40
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom

    def x_pos(i: int) -> float:
        return pad_left + (i / max(n - 1, 1)) * chart_w

    def y_pos(v: float) -> float:
        return pad_top + chart_h - ((v - y_min) / y_range) * chart_h

    # Build polyline points
    points = " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(values))

    # Area fill under the line
    area_points = (
        f"{x_pos(0):.1f},{y_pos(y_min):.1f} "
        + points
        + f" {x_pos(n - 1):.1f},{y_pos(y_min):.1f}"
    )

    # Grid lines and labels
    grid_lines = ""
    n_gridlines = 5
    for i in range(n_gridlines + 1):
        val = y_min + (y_range * i / n_gridlines)
        y = y_pos(val)
        grid_lines += (
            f'<line x1="{pad_left}" y1="{y:.0f}" x2="{width - pad_right}" '
            f'y2="{y:.0f}" stroke="#E8E6E1" stroke-width="1"/>\n'
            f'<text x="{pad_left - 8}" y="{y:.0f}" text-anchor="end" '
            f'font-size="11" fill="#999" dominant-baseline="middle">{val:.0f}</text>\n'
        )

    # Date labels (show ~5 evenly spaced)
    date_labels = ""
    label_interval = max(n // 5, 1)
    for i in range(0, n, label_interval):
        x = x_pos(i)
        d = dates[i]
        date_labels += (
            f'<text x="{x:.0f}" y="{height - 8}" text-anchor="middle" '
            f'font-size="10" fill="#999">{d.strftime("%d %b")}</text>\n'
        )
    # Always show last date
    date_labels += (
        f'<text x="{x_pos(n - 1):.0f}" y="{height - 8}" text-anchor="end" '
        f'font-size="10" fill="#999">{dates[-1].strftime("%d %b")}</text>\n'
    )

    # Model equilibrium line
    pred_y = y_pos(predicted_value)
    equilibrium_line = (
        f'<line x1="{pad_left}" y1="{pred_y:.0f}" x2="{width - pad_right}" '
        f'y2="{pred_y:.0f}" stroke="#3B82F6" stroke-width="1.5" stroke-dasharray="6,4"/>\n'
        f'<text x="{width - pad_right + 2}" y="{pred_y:.0f}" font-size="10" '
        f'fill="#3B82F6" dominant-baseline="middle">Model: {predicted_value:.0f}</text>\n'
    )

    # Latest value dot
    last_x = x_pos(n - 1)
    last_y = y_pos(values[-1])

    # Hover targets for tooltip
    hover_targets = ""
    for i, v in enumerate(values):
        cx = x_pos(i)
        cy = y_pos(v)
        d = dates[i].strftime("%d %b %Y")
        hover_targets += (
            f'<rect x="{cx - chart_w / n / 2:.0f}" y="{pad_top}" '
            f'width="{chart_w / n:.0f}" height="{chart_h}" fill="transparent" '
            f'class="hover-target" data-x="{cx:.1f}" data-y="{cy:.1f}" '
            f'data-date="{d}" data-val="{v:.1f}"/>\n'
        )

    svg = f"""<svg viewBox="0 0 {width} {height}" class="trendChart"
         style="width:100%;max-width:{width}px;height:auto">
      {grid_lines}
      {equilibrium_line}
      <polygon points="{area_points}" fill="url(#areaGrad)" opacity="0.3"/>
      <polyline points="{points}" fill="none" stroke="#C17F4E" stroke-width="2.5"
                stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="5" fill="#C17F4E" stroke="#fff" stroke-width="2"/>
      <text x="{last_x - 8:.1f}" y="{last_y - 12:.1f}" font-size="13" font-weight="700"
            fill="#C17F4E" text-anchor="end">{values[-1]:.1f}</text>
      <line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top + chart_h}"
            stroke="#CCC" stroke-width="1"/>
      <line x1="{pad_left}" y1="{pad_top + chart_h}" x2="{width - pad_right}"
            y2="{pad_top + chart_h}" stroke="#CCC" stroke-width="1"/>
      {hover_targets}
      <circle class="hoverDot" cx="0" cy="0" r="4" fill="#1C1F26" stroke="#fff"
              stroke-width="2" style="display:none"/>
      <line class="hoverLine" x1="0" y1="{pad_top}" x2="0" y2="{pad_top + chart_h}"
            stroke="#1C1F26" stroke-width="1" stroke-dasharray="3,3" style="display:none"/>
      <defs>
        <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#C17F4E" stop-opacity="0.4"/>
          <stop offset="100%" stop-color="#C17F4E" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
    </svg>"""
    return svg


def svg_decomposition_bar(decomposition: dict, width: int = 500, height: int = 60) -> str:
    """Stacked horizontal bar showing TGP component breakdown."""
    components = [
        ("Crude (AUD)", decomposition.get("crude_oil_aud", 0), "#C17F4E"),
        ("Refining", decomposition.get("refining_margin", 0), "#3B82F6"),
        ("Excise", decomposition.get("excise", 0), "#6B7280"),
        ("Base", decomposition.get("intercept", 0), "#D1D5DB"),
    ]
    residual = decomposition.get("residual", 0)
    if residual > 0:
        components.append(("Overshoot", residual, "#EF4444"))

    total = sum(c[1] for c in components)
    if total <= 0:
        return ""

    bar_y = 10
    bar_h = 30
    label_y = bar_y + bar_h + 16

    segments = ""
    labels = ""
    x_offset = 0
    for label, value, color in components:
        seg_w = (value / total) * width
        if seg_w < 1:
            continue
        segments += (
            f'<rect x="{x_offset:.1f}" y="{bar_y}" width="{seg_w:.1f}" '
            f'height="{bar_h}" fill="{color}" rx="2"/>\n'
        )
        if seg_w > 40:
            segments += (
                f'<text x="{x_offset + seg_w / 2:.1f}" y="{bar_y + bar_h / 2 + 1}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="11" font-weight="600" fill="#fff">{value:.0f}</text>\n'
            )
        # Legend below
        labels += (
            f'<rect x="{x_offset:.1f}" y="{label_y}" width="10" height="10" '
            f'fill="{color}" rx="2"/>\n'
            f'<text x="{x_offset + 14:.1f}" y="{label_y + 9}" font-size="10" '
            f'fill="#666">{label}</text>\n'
        )
        x_offset += seg_w

    return f"""<svg viewBox="0 0 {width} {height + 10}" style="width:100%;max-width:{width}px;height:auto">
      {segments}
      {labels}
    </svg>"""


def svg_asymmetry_chart(asymmetry: dict, width: int = 400, height: int = 180) -> str:
    """Bar chart comparing cumulative rise vs fall pass-through by week."""
    cum_up = asymmetry.get("cum_up", [])
    cum_down = asymmetry.get("cum_down", [])
    if not cum_up or not cum_down:
        return "<p>No asymmetry data</p>"

    n_weeks = len(cum_up)
    max_val = max(max(abs(v) for v in cum_up), max(abs(v) for v in cum_down), 0.1)

    pad_left = 40
    pad_right = 20
    pad_top = 25
    pad_bottom = 30
    chart_w = width - pad_left - pad_right
    chart_h = height - pad_top - pad_bottom
    bar_w = chart_w / n_weeks * 0.35

    bars = ""
    for i in range(n_weeks):
        x_center = pad_left + (i + 0.5) / n_weeks * chart_w

        # Rise bar (left of center)
        up_h = (abs(cum_up[i]) / max_val) * chart_h * 0.9
        bars += (
            f'<rect x="{x_center - bar_w - 1:.1f}" y="{pad_top + chart_h - up_h:.1f}" '
            f'width="{bar_w:.1f}" height="{up_h:.1f}" fill="#EF4444" rx="2" opacity="0.85"/>\n'
        )

        # Fall bar (right of center)
        down_h = (abs(cum_down[i]) / max_val) * chart_h * 0.9
        bars += (
            f'<rect x="{x_center + 1:.1f}" y="{pad_top + chart_h - down_h:.1f}" '
            f'width="{bar_w:.1f}" height="{down_h:.1f}" fill="#3B82F6" rx="2" opacity="0.85"/>\n'
        )

        # Week label
        bars += (
            f'<text x="{x_center:.0f}" y="{height - 8}" text-anchor="middle" '
            f'font-size="10" fill="#999">W{i}</text>\n'
        )

    # Legend
    legend = (
        f'<rect x="{pad_left}" y="5" width="10" height="10" fill="#EF4444" rx="2"/>'
        f'<text x="{pad_left + 14}" y="14" font-size="10" fill="#666">Price rises</text>'
        f'<rect x="{pad_left + 100}" y="5" width="10" height="10" fill="#3B82F6" rx="2"/>'
        f'<text x="{pad_left + 114}" y="14" font-size="10" fill="#666">Price falls</text>'
    )

    return f"""<svg viewBox="0 0 {width} {height}" style="width:100%;max-width:{width}px;height:auto">
      {bars}
      {legend}
      <line x1="{pad_left}" y1="{pad_top + chart_h}" x2="{width - pad_right}"
            y2="{pad_top + chart_h}" stroke="#CCC" stroke-width="1"/>
    </svg>"""


def scenario_heatmap_html(forecasts_path: Path, current_conditions: dict) -> str:
    """Build an HTML heatmap table from scenarios CSV."""
    df = pd.read_csv(forecasts_path)

    # Pick the crack spread closest to current conditions
    current_crack = current_conditions.get("diesel_crack_aud_cpl", 25)
    # Map to bucket
    if current_crack < 12:
        crack = 15
    elif current_crack > 28:
        crack = 35
    else:
        crack = 25

    subset = df[df["crack_spread_usd"] == crack]
    if subset.empty:
        crack = df["crack_spread_usd"].mode().iloc[0]
        subset = df[df["crack_spread_usd"] == crack]

    pivot = subset.pivot_table(
        index="wti_usd", columns="audusd", values="predicted_diesel_tgp"
    )

    current_wti = current_conditions.get("wti_usd", 0)
    current_fx = current_conditions.get("audusd", 0)

    # Color scale: green (low) -> amber -> red (high)
    val_min = pivot.values.min()
    val_max = pivot.values.max()
    val_range = val_max - val_min if val_max > val_min else 1

    def cell_color(val: float) -> str:
        ratio = (val - val_min) / val_range
        if ratio < 0.33:
            r, g, b = 76, 175, 80  # green
        elif ratio < 0.66:
            r, g, b = 255, 183, 77  # amber
        else:
            r, g, b = 229, 115, 115  # red
        return f"rgb({r},{g},{b})"

    rows_html = ""
    for wti in sorted(pivot.index):
        cells = ""
        is_current_wti = abs(wti - current_wti) <= 6
        for fx in sorted(pivot.columns):
            val = pivot.loc[wti, fx]
            color = cell_color(val)
            is_current_fx = abs(fx - current_fx) < 0.025
            highlight = (
                ' style="outline:3px solid #1C1F26;outline-offset:-3px;font-weight:700"'
                if is_current_wti and is_current_fx else ""
            )
            cells += (
                f'<td style="background:{color};color:#fff;text-align:center;'
                f'padding:10px 8px;font-size:14px;font-weight:500"'
                f'{highlight}>{val:.0f}</td>\n'
            )
        row_style = ' style="font-weight:700"' if is_current_wti else ""
        wti_label = f"${wti}" + (" *" if is_current_wti else "")
        rows_html += f"<tr><td style='padding:8px 12px;font-weight:600;white-space:nowrap'{row_style}>{wti_label}</td>{cells}</tr>\n"

    header_cells = ""
    for fx in sorted(pivot.columns):
        is_cur = abs(fx - current_fx) < 0.025
        style = "font-weight:700;" if is_cur else ""
        label = f"{fx:.2f}" + (" *" if is_cur else "")
        header_cells += f'<th style="padding:8px;text-align:center;{style}">{label}</th>'

    return f"""
    <table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden;
                  box-shadow:0 1px 3px rgba(0,0,0,.08)">
      <thead>
        <tr style="background:#1C1F26;color:#F5F5F3">
          <th style="padding:8px 12px;text-align:left">WTI / AUD</th>
          {header_cells}
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <p style="font-size:11px;color:#999;margin-top:8px">
      * = closest to current conditions. Crack spread held at ${crack}/bbl.
      Values are predicted diesel TGP in cpl (inc GST).
    </p>
    """


def direction_indicator(residual: float, actual: float) -> tuple[str, str, str]:
    """Return (arrow, label, color) based on where TGP is vs equilibrium."""
    pct = residual / actual * 100 if actual > 0 else 0
    if residual > 20:
        return "&#9660;", f"OVERSHOOTING by {residual:.0f} cpl ({pct:.0f}%) -- expect decline", "#EF4444"
    elif residual < -20:
        return "&#9650;", f"UNDERSHOOTING by {abs(residual):.0f} cpl -- expect further rises", "#F59E0B"
    else:
        return "&#9654;", f"Near equilibrium (within {abs(residual):.0f} cpl)", "#22C55E"


def generate_html(data: dict) -> str:
    conditions = data["conditions"]
    decomposition = data["decomposition"]
    asymmetry = data.get("asymmetry", {})
    model = data.get("model", {})

    run_date = data.get("run_date", date.today().isoformat())
    actual_tgp = conditions["diesel_tgp_actual"]
    predicted_tgp = decomposition["predicted_total"]
    residual = decomposition["residual"]

    # Load history for chart
    try:
        history = load_tgp_history(days=120)
    except Exception:
        history = pd.DataFrame()

    # Direction indicator
    arrow, direction_text, direction_color = direction_indicator(residual, actual_tgp)

    # Week-on-week change
    if len(history) > 7:
        week_ago = history["diesel_tgp"].iloc[-8] if len(history) > 7 else actual_tgp
        wow_change = actual_tgp - week_ago
        wow_pct = (wow_change / week_ago * 100) if week_ago > 0 else 0
        wow_text = f'{"+" if wow_change >= 0 else ""}{wow_change:.1f} cpl ({wow_pct:+.1f}%) vs last week'
    else:
        wow_text = ""

    # Build SVG charts
    trend_svg = svg_trend_chart(history, predicted_tgp) if not history.empty else ""
    decomp_svg = svg_decomposition_bar(decomposition)
    asym_svg = svg_asymmetry_chart(asymmetry) if asymmetry.get("cum_up") else ""

    # Scenario heatmap
    scenarios_path = FORECAST_DIR / "scenarios.csv"
    scenario_html = ""
    if scenarios_path.exists():
        scenario_html = scenario_heatmap_html(scenarios_path, conditions)

    # Model quality badge
    r_sq = model.get("r_squared", 0)
    quality_color = "#22C55E" if r_sq > 0.95 else "#F59E0B" if r_sq > 0.85 else "#EF4444"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diesel TGP Forecast | Booth Transport</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #F5F5F3;
    color: #1C1F26;
    line-height: 1.5;
  }}
  .header {{
    background: #1C1F26;
    color: #F5F5F3;
    padding: 20px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .header h1 {{ font-size: 20px; font-weight: 600; }}
  .header .meta {{ font-size: 12px; color: #999; }}
  .container {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}

  .hero-row {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .hero-card {{
    background: #fff;
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    text-align: center;
  }}
  .hero-label {{ font-size: 12px; color: #999; text-transform: uppercase; letter-spacing: 1px; }}
  .hero-number {{ font-size: 42px; font-weight: 700; margin: 4px 0; }}
  .hero-sub {{ font-size: 13px; color: #666; }}

  .card {{
    background: #fff;
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    margin-bottom: 20px;
  }}
  .card h2 {{
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 2px solid #E0DDD8;
  }}
  .card h3 {{
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 12px;
    color: #555;
  }}

  .direction-banner {{
    background: #fff;
    border-left: 5px solid {direction_color};
    border-radius: 8px;
    padding: 16px 24px;
    margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .direction-arrow {{
    font-size: 36px;
    color: {direction_color};
    line-height: 1;
  }}
  .direction-text {{ font-size: 16px; font-weight: 600; }}
  .direction-detail {{ font-size: 13px; color: #666; margin-top: 4px; }}

  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 700px) {{
    .hero-row {{ grid-template-columns: 1fr; }}
    .two-col {{ grid-template-columns: 1fr; }}
  }}

  .stat-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #F0EDEA; }}
  .stat-label {{ color: #666; font-size: 14px; }}
  .stat-value {{ font-weight: 600; font-size: 14px; }}

  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
  }}

  .chart-tooltip {{
    display: none;
    position: absolute;
    background: #1C1F26;
    color: #fff;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 12px;
    pointer-events: none;
    z-index: 10;
    white-space: nowrap;
  }}

  .asym-stat {{ text-align: center; padding: 16px; }}
  .asym-number {{ font-size: 28px; font-weight: 700; }}
  .asym-label {{ font-size: 12px; color: #999; text-transform: uppercase; }}

  footer {{ text-align: center; font-size: 11px; color: #999; padding: 24px 0; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Booth Transport</h1>
    <div class="meta">Diesel TGP Forecast Dashboard</div>
  </div>
  <div style="text-align:right">
    <div class="meta">Updated: {run_date}</div>
    <div class="meta">Model R-sq: <span class="badge" style="background:{quality_color}">{r_sq:.1%}</span></div>
  </div>
</div>

<div class="container">

  <!-- Hero numbers -->
  <div class="hero-row">
    <div class="hero-card">
      <div class="hero-label">Current Diesel TGP</div>
      <div class="hero-number" style="color:#C17F4E">{actual_tgp:.1f}</div>
      <div class="hero-sub">cpl (inc GST) | {wow_text}</div>
    </div>
    <div class="hero-card">
      <div class="hero-label">Model Equilibrium</div>
      <div class="hero-number" style="color:#3B82F6">{predicted_tgp:.0f}</div>
      <div class="hero-sub">cpl predicted at current inputs</div>
    </div>
    <div class="hero-card">
      <div class="hero-label">Gap</div>
      <div class="hero-number" style="color:{direction_color}">{"+" if residual >= 0 else ""}{residual:.0f}</div>
      <div class="hero-sub">cpl {"above" if residual >= 0 else "below"} equilibrium</div>
    </div>
  </div>

  <!-- Direction banner -->
  <div class="direction-banner">
    <div class="direction-arrow">{arrow}</div>
    <div>
      <div class="direction-text">{direction_text}</div>
      <div class="direction-detail">
        Rises pass through in ~{asymmetry.get("weeks_90pct_rise", "?")} week(s).
        Falls take ~{asymmetry.get("weeks_90pct_fall", "?")} weeks.
        {"TGP will drift down as the lag effect unwinds." if residual > 20 else
         "TGP has further to climb before reaching equilibrium." if residual < -20 else
         "Watch crude and AUD/USD for the next directional signal."}
      </div>
    </div>
  </div>

  <!-- Trend chart -->
  <div class="card" style="position:relative">
    <h2>Diesel TGP -- Last 120 Days</h2>
    <div class="chart-tooltip"></div>
    {trend_svg}
    <p style="font-size:11px;color:#999;margin-top:8px">
      Solid line = actual TGP. Dashed blue = model equilibrium at today's inputs.
    </p>
  </div>

  <!-- Decomposition + Current inputs -->
  <div class="two-col">
    <div class="card">
      <h2>What is Driving the Price</h2>
      {decomp_svg}
      <div style="margin-top:16px">
        <div class="stat-row">
          <span class="stat-label">Crude oil (AUD)</span>
          <span class="stat-value">{decomposition.get("crude_oil_aud", 0):.0f} cpl</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Refining margin</span>
          <span class="stat-value">{decomposition.get("refining_margin", 0):.0f} cpl</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Excise</span>
          <span class="stat-value">{decomposition.get("excise", 0):.0f} cpl</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Base costs</span>
          <span class="stat-value">{decomposition.get("intercept", 0):.0f} cpl</span>
        </div>
        <div class="stat-row" style="border-bottom:2px solid #1C1F26">
          <span class="stat-label" style="font-weight:600">Overshoot / lag</span>
          <span class="stat-value" style="color:#EF4444">{residual:+.0f} cpl</span>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Market Inputs</h2>
      <div class="stat-row">
        <span class="stat-label">WTI Crude</span>
        <span class="stat-value">${conditions["wti_usd"]:.2f} USD/bbl</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">AUD/USD</span>
        <span class="stat-value">{conditions["audusd"]:.4f}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">WTI in AUD</span>
        <span class="stat-value">{conditions["wti_aud_cpl"]:.1f} cpl</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Diesel crack spread</span>
        <span class="stat-value">{conditions["diesel_crack_aud_cpl"]:.1f} cpl (AUD)</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Fuel excise</span>
        <span class="stat-value">{conditions["excise"]:.1f} cpl</span>
      </div>
      <div style="margin-top:16px">
        <h3>Pass-Through Asymmetry</h3>
        <div style="display:flex;gap:16px">
          <div class="asym-stat" style="flex:1;background:#FEF2F2;border-radius:8px">
            <div class="asym-number" style="color:#EF4444">{asymmetry.get("weeks_90pct_rise", "?")}</div>
            <div class="asym-label">Weeks for rises</div>
          </div>
          <div class="asym-stat" style="flex:1;background:#EFF6FF;border-radius:8px">
            <div class="asym-number" style="color:#3B82F6">{asymmetry.get("weeks_90pct_fall", "?")}</div>
            <div class="asym-label">Weeks for falls</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Asymmetry chart -->
  <div class="card">
    <h2>Cumulative Pass-Through by Week</h2>
    <p style="font-size:13px;color:#666;margin-bottom:12px">
      How much of a 1 cpl crude oil change has flowed through to TGP by each week.
      Rises (red) pass through faster than falls (blue).
    </p>
    {asym_svg}
  </div>

  <!-- Scenario table -->
  <div class="card">
    <h2>Scenario Matrix -- Where Could TGP Go?</h2>
    <p style="font-size:13px;color:#666;margin-bottom:12px">
      Predicted diesel TGP (cpl) under different crude oil and exchange rate scenarios.
      Current conditions marked with *.
    </p>
    {scenario_html}
  </div>

  <footer>
    Data: AIP Terminal Gate Prices, yfinance (WTI, AUD/USD, Heating Oil futures).
    Model: OLS multi-factor regression (R-sq {r_sq:.1%}).
    Updated daily at ~11am AEST.
  </footer>

</div>

<script>
document.querySelectorAll('.trendChart').forEach(function(svg) {{
  var card = svg.closest('.card');
  if (!card) return;
  var tooltip = card.querySelector('.chart-tooltip');
  var dot = svg.querySelector('.hoverDot');
  var vline = svg.querySelector('.hoverLine');
  if (!tooltip || !dot || !vline) return;
  svg.querySelectorAll('.hover-target').forEach(function(rect) {{
    rect.onmouseenter = function() {{
      var x = this.getAttribute('data-x');
      var y = this.getAttribute('data-y');
      var dt = this.getAttribute('data-date');
      var val = this.getAttribute('data-val');
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', y);
      dot.style.display = '';
      vline.setAttribute('x1', x);
      vline.setAttribute('x2', x);
      vline.style.display = '';
      tooltip.innerHTML = '<strong>' + dt + '</strong><br>TGP: ' + val + ' cpl';
      tooltip.style.display = 'block';
      var svgRect = svg.getBoundingClientRect();
      var cardRect = card.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var pxX = (x / vb.width) * svgRect.width;
      var pxY = (y / vb.height) * svgRect.height;
      var tipLeft = pxX + (svgRect.left - cardRect.left) + 12;
      var tipTop = pxY + (svgRect.top - cardRect.top) - 20;
      if (tipLeft + 160 > cardRect.width) tipLeft = pxX + (svgRect.left - cardRect.left) - 160;
      tooltip.style.left = tipLeft + 'px';
      tooltip.style.top = tipTop + 'px';
    }};
    rect.onmouseleave = function() {{
      dot.style.display = 'none';
      vline.style.display = 'none';
      tooltip.style.display = 'none';
    }};
  }});
}});
</script>

</body>
</html>"""
    return html


def main() -> None:
    data = load_latest()
    html = generate_html(data)

    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / "forecast.html"
    path.write_text(html, encoding="utf-8")
    print(f"Forecast dashboard saved to {path}")


if __name__ == "__main__":
    main()
