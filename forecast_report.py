"""
TGP Forecast Dashboard
Generates a visual HTML dashboard for diesel TGP forecasting.
Reads from forecast/latest.json and data/diesel_tgp_history.csv.
Outputs to reports/forecast.html (deployed via GitHub Pages).
"""

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

FORECAST_DIR = Path(__file__).parent / "forecast"
DATA_DIR = Path(__file__).parent / "data"
REPORT_DIR = Path(__file__).parent / "reports"


def load_latest():
    with open(FORECAST_DIR / "latest.json") as f:
        return json.load(f)


def load_tgp_history(days=120):
    path = DATA_DIR / "diesel_tgp_history.csv"
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    cutoff = df.index.max() - pd.Timedelta(days=days)
    return df[df.index >= cutoff]


def svg_sparkline(history, predicted, w=800, h=300):
    vals = history["diesel_tgp"].dropna().values
    dates = history.index
    n = len(vals)
    if n < 2:
        return ""

    y_min = min(vals.min(), predicted) * 0.90
    y_max = max(vals.max(), predicted) * 1.04
    y_range = y_max - y_min or 1

    pl, pr, pt, pb = 56, 24, 24, 44
    cw = w - pl - pr
    ch = h - pt - pb

    def xp(i):
        return pl + (i / max(n - 1, 1)) * cw

    def yp(v):
        return pt + ch - ((v - y_min) / y_range) * ch

    # Smooth bezier curve through points
    points = [(xp(i), yp(v)) for i, v in enumerate(vals)]

    # Build path with smooth curves
    path_d = f"M {points[0][0]:.1f},{points[0][1]:.1f}"
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        cpx = (x0 + x1) / 2
        path_d += f" C {cpx:.1f},{y0:.1f} {cpx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"

    # Area under curve
    area_d = path_d + f" L {points[-1][0]:.1f},{pt + ch:.1f} L {points[0][0]:.1f},{pt + ch:.1f} Z"

    # Gridlines
    grid = ""
    n_grid = 5
    for i in range(n_grid + 1):
        v = y_min + y_range * i / n_grid
        y = yp(v)
        grid += f'<line x1="{pl}" y1="{y:.0f}" x2="{w - pr}" y2="{y:.0f}" stroke="#f0f0f0" stroke-width="1"/>\n'
        grid += f'<text x="{pl - 10}" y="{y:.0f}" text-anchor="end" dominant-baseline="middle" fill="#b0b0b0" font-size="11" font-family="Inter,system-ui,sans-serif">{v:.0f}</text>\n'

    # Date labels
    dlabels = ""
    step = max(n // 6, 1)
    for i in range(0, n, step):
        x = xp(i)
        dlabels += f'<text x="{x:.0f}" y="{h - 8}" text-anchor="middle" fill="#b0b0b0" font-size="10" font-family="Inter,system-ui,sans-serif">{dates[i].strftime("%d %b")}</text>\n'
    dlabels += f'<text x="{xp(n - 1):.0f}" y="{h - 8}" text-anchor="end" fill="#b0b0b0" font-size="10" font-family="Inter,system-ui,sans-serif">{dates[-1].strftime("%d %b")}</text>\n'

    # Predicted equilibrium line
    py = yp(predicted)
    eq_line = f"""
    <line x1="{pl}" y1="{py:.0f}" x2="{w - pr}" y2="{py:.0f}"
          stroke="#6366f1" stroke-width="1.5" stroke-dasharray="8,6" opacity="0.6"/>
    <rect x="{w - pr + 4}" y="{py - 10:.0f}" width="52" height="20" rx="4" fill="#6366f1" opacity="0.1"/>
    <text x="{w - pr + 8}" y="{py + 3:.0f}" fill="#6366f1" font-size="11"
          font-weight="600" font-family="Inter,system-ui,sans-serif">{predicted:.0f} eq</text>
    """

    # Last point highlight
    lx, ly = points[-1]
    last_val = vals[-1]
    highlight = f"""
    <circle cx="{lx:.1f}" cy="{ly:.1f}" r="6" fill="#fff" stroke="#f97316" stroke-width="3"/>
    <text x="{lx - 10:.1f}" y="{ly - 14:.1f}" text-anchor="end" fill="#f97316"
          font-size="14" font-weight="700" font-family="Inter,system-ui,sans-serif">{last_val:.1f}</text>
    """

    # Hover targets
    hovers = ""
    for i, v in enumerate(vals):
        cx, cy = xp(i), yp(v)
        d = dates[i].strftime("%d %b %Y")
        hovers += f'<rect x="{cx - cw / n / 2:.0f}" y="{pt}" width="{max(cw / n, 4):.0f}" height="{ch}" fill="transparent" class="ht" data-x="{cx:.1f}" data-y="{cy:.1f}" data-d="{d}" data-v="{v:.1f}"/>\n'

    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;display:block">
    <defs>
      <linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#f97316" stop-opacity="0.15"/>
        <stop offset="100%" stop-color="#f97316" stop-opacity="0.01"/>
      </linearGradient>
    </defs>
    {grid}
    <path d="{area_d}" fill="url(#ag)"/>
    <path d="{path_d}" fill="none" stroke="#f97316" stroke-width="2.5"
          stroke-linejoin="round" stroke-linecap="round"/>
    {eq_line}
    {highlight}
    {dlabels}
    {hovers}
    <circle class="hd" cx="0" cy="0" r="4" fill="#1e293b" stroke="#fff" stroke-width="2" style="display:none"/>
    <line class="hl" x1="0" y1="{pt}" x2="0" y2="{pt + ch}" stroke="#1e293b" stroke-width="1" stroke-dasharray="3,3" style="display:none"/>
    </svg>"""


def decomp_bars(decomposition, total_actual):
    components = [
        ("Crude + shipping", decomposition.get("crude_oil_aud", 0), "#f97316"),
        ("Refining margin", decomposition.get("refining_margin", 0), "#6366f1"),
        ("Excise", decomposition.get("excise", 0), "#64748b"),
        ("Base", decomposition.get("intercept", 0), "#cbd5e1"),
    ]
    residual = decomposition.get("residual", 0)
    if residual > 0:
        components.append(("Lag overshoot", residual, "#ef4444"))

    total = sum(c[1] for c in components)
    if total <= 0:
        return ""

    rows = ""
    for label, value, color in components:
        pct = value / total * 100
        pct_of_actual = value / total_actual * 100 if total_actual > 0 else 0
        rows += f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
          <div style="width:120px;font-size:13px;color:#64748b;flex-shrink:0">{label}</div>
          <div style="flex:1;background:#f1f5f9;border-radius:6px;height:28px;overflow:hidden;position:relative">
            <div style="width:{pct:.1f}%;height:100%;background:{color};border-radius:6px;
                        display:flex;align-items:center;padding-left:8px;min-width:40px;
                        transition:width 0.6s ease">
              <span style="font-size:12px;font-weight:600;color:#fff;white-space:nowrap">{value:.0f}</span>
            </div>
          </div>
          <div style="width:48px;text-align:right;font-size:12px;color:#94a3b8;flex-shrink:0">{pct_of_actual:.0f}%</div>
        </div>"""

    return f'<div style="margin-top:8px">{rows}</div>'


def scenario_table(forecasts_path, conditions):
    df = pd.read_csv(forecasts_path)

    current_crack = conditions.get("diesel_crack_aud_cpl", 25)
    crack = 35 if current_crack > 28 else (15 if current_crack < 12 else 25)

    subset = df[df["crack_spread_usd"] == crack]
    if subset.empty:
        crack = df["crack_spread_usd"].mode().iloc[0]
        subset = df[df["crack_spread_usd"] == crack]

    pivot = subset.pivot_table(index="wti_usd", columns="audusd", values="predicted_diesel_tgp")

    cur_wti = conditions.get("wti_usd", 0)
    cur_fx = conditions.get("audusd", 0)

    vmin = pivot.values.min()
    vmax = pivot.values.max()
    vr = vmax - vmin or 1

    def bg(val):
        ratio = (val - vmin) / vr
        # Green to amber to red
        if ratio < 0.5:
            r = int(34 + ratio * 2 * (234 - 34))
            g = int(197 + ratio * 2 * (179 - 197))
            b = int(94 + ratio * 2 * (8 - 94))
        else:
            r2 = (ratio - 0.5) * 2
            r = int(234 + r2 * (239 - 234))
            g = int(179 - r2 * (179 - 68))
            b = int(8 + r2 * (68 - 8))
        return f"rgb({r},{g},{b})"

    hdr = '<th style="padding:12px 8px;text-align:left;font-weight:600;font-size:12px;color:#64748b;border-bottom:2px solid #e2e8f0">WTI USD</th>'
    for fx in sorted(pivot.columns):
        cur = abs(fx - cur_fx) < 0.025
        style = "font-weight:700;color:#1e293b" if cur else "color:#64748b"
        label = f"{fx:.2f}" + (" &#9668;" if cur else "")
        hdr += f'<th style="padding:12px 8px;text-align:center;font-size:12px;{style};border-bottom:2px solid #e2e8f0">{label}</th>'

    body = ""
    for wti in sorted(pivot.index):
        is_cur_wti = abs(wti - cur_wti) <= 6
        cells = ""
        for fx in sorted(pivot.columns):
            val = pivot.loc[wti, fx]
            is_cur_fx = abs(fx - cur_fx) < 0.025
            is_current = is_cur_wti and is_cur_fx
            border = "outline:2px solid #1e293b;outline-offset:-2px;border-radius:4px;" if is_current else ""
            fw = "font-weight:700;" if is_current else "font-weight:500;"
            cells += f'<td style="padding:10px 8px;text-align:center;background:{bg(val)};color:#fff;font-size:13px;{fw}{border}">{val:.0f}</td>\n'

        rw = "font-weight:700;" if is_cur_wti else ""
        marker = " &#9668;" if is_cur_wti else ""
        body += f'<tr><td style="padding:10px 12px;font-size:13px;{rw}color:#1e293b;border-bottom:1px solid #f1f5f9">${wti}{marker}</td>{cells}</tr>\n'

    return f"""
    <div style="overflow-x:auto;border-radius:12px;border:1px solid #e2e8f0">
      <table style="width:100%;border-collapse:collapse;min-width:500px">
        <thead><tr style="background:#f8fafc">{hdr}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>
    <p style="font-size:11px;color:#94a3b8;margin-top:10px">
      Predicted diesel TGP (cpl inc GST). Current position marked. Crack spread held at ${crack}/bbl USD.
    </p>"""


def asym_bars(asymmetry, w=600, h=200):
    cum_up = asymmetry.get("cum_up", [])
    cum_down = asymmetry.get("cum_down", [])
    if not cum_up:
        return ""

    n = len(cum_up)
    mx = max(max(abs(v) for v in cum_up), max(abs(v) for v in cum_down), 0.1)

    pl, pr, pt, pb = 36, 20, 30, 36
    cw = w - pl - pr
    ch = h - pt - pb
    bw = cw / n * 0.32
    gap = 2

    bars = ""
    for i in range(n):
        xc = pl + (i + 0.5) / n * cw

        uh = abs(cum_up[i]) / mx * ch * 0.85
        bars += f'<rect x="{xc - bw - gap / 2:.1f}" y="{pt + ch - uh:.1f}" width="{bw:.1f}" height="{uh:.1f}" fill="#f97316" rx="3" opacity="0.85"/>\n'

        dh = abs(cum_down[i]) / mx * ch * 0.85
        bars += f'<rect x="{xc + gap / 2:.1f}" y="{pt + ch - dh:.1f}" width="{bw:.1f}" height="{dh:.1f}" fill="#6366f1" rx="3" opacity="0.85"/>\n'

        bars += f'<text x="{xc:.0f}" y="{h - 10}" text-anchor="middle" fill="#94a3b8" font-size="11" font-family="Inter,system-ui,sans-serif">W{i}</text>\n'

    legend = f"""
    <circle cx="{pl}" cy="14" r="5" fill="#f97316"/>
    <text x="{pl + 10}" y="18" fill="#64748b" font-size="11" font-family="Inter,system-ui,sans-serif">Rises</text>
    <circle cx="{pl + 70}" cy="14" r="5" fill="#6366f1"/>
    <text x="{pl + 80}" y="18" fill="#64748b" font-size="11" font-family="Inter,system-ui,sans-serif">Falls</text>
    """

    return f"""<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:{w}px;height:auto;display:block">
    {bars}
    {legend}
    <line x1="{pl}" y1="{pt + ch}" x2="{w - pr}" y2="{pt + ch}" stroke="#e2e8f0" stroke-width="1"/>
    </svg>"""


def generate_html(data):
    cond = data["conditions"]
    decomp = data["decomposition"]
    asym = data.get("asymmetry", {})
    model = data.get("model", {})
    run_date = data.get("run_date", date.today().isoformat())

    actual = cond["diesel_tgp_actual"]
    predicted = decomp["predicted_total"]
    residual = decomp["residual"]
    r_sq = model.get("r_squared", 0)

    try:
        history = load_tgp_history(120)
    except Exception:
        history = pd.DataFrame()

    # Week-on-week
    if len(history) > 7:
        wa = history["diesel_tgp"].iloc[-8]
        wow = actual - wa
        wow_pct = wow / wa * 100 if wa else 0
        wow_html = f'<span style="color:{"#ef4444" if wow > 0 else "#22c55e"};font-weight:600">{"+" if wow >= 0 else ""}{wow:.1f} cpl</span> <span style="color:#94a3b8">({wow_pct:+.1f}%) vs 7 days ago</span>'
    else:
        wow_html = ""

    # Direction
    if residual > 20:
        dir_icon = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 4l8 8h-5v8h-6v-8H4l8-8z" fill="#ef4444" transform="rotate(180 12 12)"/></svg>'
        dir_label = "Expect decline"
        dir_detail = f"TGP is {residual:.0f} cpl above equilibrium. As the lag unwinds, TGP should drift down over {asym.get('weeks_90pct_fall', 5)} weeks."
        dir_color = "#fef2f2"
        dir_border = "#fecaca"
    elif residual < -20:
        dir_icon = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 4l8 8h-5v8h-6v-8H4l8-8z" fill="#f59e0b"/></svg>'
        dir_label = "Expect further rises"
        dir_detail = f"TGP is {abs(residual):.0f} cpl below equilibrium. Prices have not yet caught up to current market inputs."
        dir_color = "#fffbeb"
        dir_border = "#fde68a"
    else:
        dir_icon = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M4 12h16m-4-4l4 4-4 4" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        dir_label = "Near equilibrium"
        dir_detail = f"TGP is within {abs(residual):.0f} cpl of model prediction. Direction depends on crude and AUD/USD moves."
        dir_color = "#f0fdf4"
        dir_border = "#bbf7d0"

    chart_svg = svg_sparkline(history, predicted) if not history.empty else ""
    decomp_html = decomp_bars(decomp, actual)
    asym_svg = asym_bars(asym) if asym.get("cum_up") else ""
    sc_path = FORECAST_DIR / "scenarios.csv"
    sc_html = scenario_table(sc_path, cond) if sc_path.exists() else ""

    wti_aud_cpl = cond.get("wti_aud_cpl", 0)
    coeff = model.get("coefficients", {}).get("WTI_AUD_CPL_lag1", 1.19)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diesel TGP Forecast</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{
    font-family:'Inter',system-ui,-apple-system,sans-serif;
    background:#f8fafc;
    color:#1e293b;
    line-height:1.6;
    -webkit-font-smoothing:antialiased;
  }}
  .top-bar{{
    background:#fff;
    border-bottom:1px solid #e2e8f0;
    padding:16px 32px;
    display:flex;
    justify-content:space-between;
    align-items:center;
  }}
  .logo{{font-size:18px;font-weight:700;color:#1e293b;letter-spacing:-0.3px}}
  .logo span{{color:#f97316}}
  .top-meta{{font-size:12px;color:#94a3b8}}
  .container{{max-width:1040px;margin:0 auto;padding:28px 20px 60px}}

  .metric-grid{{
    display:grid;
    grid-template-columns:1fr 1fr 1fr;
    gap:16px;
    margin-bottom:20px;
  }}
  .metric{{
    background:#fff;
    border-radius:16px;
    padding:28px 24px;
    border:1px solid #e2e8f0;
  }}
  .metric-label{{
    font-size:11px;
    font-weight:600;
    text-transform:uppercase;
    letter-spacing:0.8px;
    color:#94a3b8;
    margin-bottom:4px;
  }}
  .metric-value{{
    font-size:44px;
    font-weight:800;
    letter-spacing:-1.5px;
    line-height:1.1;
  }}
  .metric-sub{{font-size:12px;color:#94a3b8;margin-top:6px}}

  .signal-bar{{
    background:{dir_color};
    border:1px solid {dir_border};
    border-radius:14px;
    padding:18px 24px;
    margin-bottom:20px;
    display:flex;
    align-items:center;
    gap:16px;
  }}
  .signal-icon{{flex-shrink:0}}
  .signal-label{{font-size:16px;font-weight:700;color:#1e293b}}
  .signal-detail{{font-size:13px;color:#64748b;margin-top:2px}}

  .card{{
    background:#fff;
    border-radius:16px;
    border:1px solid #e2e8f0;
    padding:28px;
    margin-bottom:20px;
  }}
  .card-title{{
    font-size:15px;
    font-weight:700;
    color:#1e293b;
    margin-bottom:4px;
  }}
  .card-desc{{
    font-size:12px;
    color:#94a3b8;
    margin-bottom:20px;
  }}

  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}

  .input-row{{
    display:flex;
    justify-content:space-between;
    padding:10px 0;
    border-bottom:1px solid #f1f5f9;
    font-size:13px;
  }}
  .input-row:last-child{{border-bottom:none}}
  .input-label{{color:#64748b}}
  .input-value{{font-weight:600;color:#1e293b}}

  .asym-grid{{display:flex;gap:12px;margin-bottom:20px}}
  .asym-pill{{
    flex:1;
    text-align:center;
    padding:20px 16px;
    border-radius:14px;
  }}
  .asym-num{{font-size:36px;font-weight:800;letter-spacing:-1px;line-height:1}}
  .asym-unit{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px}}

  .tip{{
    display:none;
    position:absolute;
    background:#1e293b;
    color:#fff;
    padding:8px 12px;
    border-radius:8px;
    font-size:12px;
    pointer-events:none;
    z-index:10;
    white-space:nowrap;
    box-shadow:0 4px 12px rgba(0,0,0,0.15);
  }}

  .badge{{
    display:inline-flex;
    align-items:center;
    gap:4px;
    padding:3px 10px;
    border-radius:20px;
    font-size:11px;
    font-weight:600;
  }}

  footer{{
    text-align:center;
    font-size:11px;
    color:#94a3b8;
    padding:32px 0;
    border-top:1px solid #f1f5f9;
    margin-top:20px;
  }}

  @media(max-width:720px){{
    .metric-grid,.two-col{{grid-template-columns:1fr}}
    .asym-grid{{flex-direction:column}}
  }}
</style>
</head>
<body>

<div class="top-bar">
  <div>
    <div class="logo">Booth<span>.</span> Fuel Intelligence</div>
  </div>
  <div style="text-align:right">
    <div class="top-meta">{run_date} &middot; Model R&sup2;
      <span class="badge" style="background:{"#f0fdf4;color:#16a34a" if r_sq > 0.95 else "#fffbeb;color:#d97706" if r_sq > 0.85 else "#fef2f2;color:#dc2626"}">{r_sq:.1%}</span>
    </div>
    <div class="top-meta">Auto-updated weekdays at 11am AEST</div>
  </div>
</div>

<div class="container">

  <!-- Key Metrics -->
  <div class="metric-grid">
    <div class="metric">
      <div class="metric-label">Diesel TGP Today</div>
      <div class="metric-value" style="color:#f97316">{actual:.1f}</div>
      <div class="metric-sub">{wow_html}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Model Equilibrium</div>
      <div class="metric-value" style="color:#6366f1">{predicted:.0f}</div>
      <div class="metric-sub">Where TGP <em>should</em> be at current inputs</div>
    </div>
    <div class="metric">
      <div class="metric-label">Overshoot / Gap</div>
      <div class="metric-value" style="color:{"#ef4444" if residual > 0 else "#22c55e"}">{"+" if residual >= 0 else ""}{residual:.0f}</div>
      <div class="metric-sub">cpl {"above" if residual >= 0 else "below"} equilibrium</div>
    </div>
  </div>

  <!-- Direction Signal -->
  <div class="signal-bar">
    <div class="signal-icon">{dir_icon}</div>
    <div>
      <div class="signal-label">{dir_label}</div>
      <div class="signal-detail">{dir_detail}</div>
    </div>
  </div>

  <!-- Trend Chart -->
  <div class="card" style="position:relative">
    <div class="card-title">Diesel TGP &mdash; Last 120 Days</div>
    <div class="card-desc">Orange line = actual TGP. Dashed purple = model equilibrium at current inputs.</div>
    <div class="tip" id="chartTip"></div>
    {chart_svg}
  </div>

  <!-- Decomposition + Inputs -->
  <div class="two-col">
    <div class="card">
      <div class="card-title">What is Driving the Price</div>
      <div class="card-desc">Breakdown of today's {actual:.0f} cpl diesel TGP into model components</div>
      {decomp_html}
    </div>

    <div class="card">
      <div class="card-title">Market Inputs</div>
      <div class="card-desc">Latest values feeding the forecast model</div>
      <div class="input-row">
        <span class="input-label">WTI Crude Oil</span>
        <span class="input-value">${cond["wti_usd"]:.2f} USD/bbl</span>
      </div>
      <div class="input-row">
        <span class="input-label">AUD / USD</span>
        <span class="input-value">{cond["audusd"]:.4f}</span>
      </div>
      <div class="input-row">
        <span class="input-label">WTI in AUD</span>
        <span class="input-value">{wti_aud_cpl:.1f} cpl</span>
      </div>
      <div class="input-row">
        <span class="input-label">Diesel crack spread</span>
        <span class="input-value">{cond["diesel_crack_aud_cpl"]:.1f} cpl (AUD)</span>
      </div>
      <div class="input-row">
        <span class="input-label">Fuel excise</span>
        <span class="input-value">{cond["excise"]:.1f} cpl</span>
      </div>

      <div style="margin-top:24px">
        <div class="card-title" style="font-size:13px">Pass-Through Speed</div>
        <div class="card-desc">How fast crude oil changes flow through to TGP</div>
        <div class="asym-grid">
          <div class="asym-pill" style="background:#fff7ed">
            <div class="asym-num" style="color:#f97316">{asym.get("weeks_90pct_rise", "?")}</div>
            <div class="asym-unit" style="color:#f97316">Week to rise</div>
          </div>
          <div class="asym-pill" style="background:#eef2ff">
            <div class="asym-num" style="color:#6366f1">{asym.get("weeks_90pct_fall", "?")}</div>
            <div class="asym-unit" style="color:#6366f1">Weeks to fall</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Asymmetry Chart -->
  <div class="card">
    <div class="card-title">Cumulative Pass-Through by Week</div>
    <div class="card-desc">How much of a 1 cpl crude change has flowed into TGP by each week. Rises (orange) are passed through much faster than falls (purple).</div>
    {asym_svg}
  </div>

  <!-- Scenario Table -->
  <div class="card">
    <div class="card-title">Scenario Matrix</div>
    <div class="card-desc">Predicted diesel TGP under different crude oil prices and AUD/USD exchange rates. Green = lower, red = higher.</div>
    {sc_html}
  </div>

  <footer>
    Data sources: AIP Terminal Gate Prices &middot; Yahoo Finance (WTI, AUD/USD, Heating Oil futures)<br>
    Model: OLS multi-factor regression with 1-week lag. R&sup2; = {r_sq:.1%} on {data.get("run_date", "")}.
  </footer>

</div>

<script>
(function(){{
  var card=document.querySelector('.card[style*="relative"]');
  if(!card)return;
  var svg=card.querySelector('svg');
  var tip=document.getElementById('chartTip');
  var dot=svg.querySelector('.hd');
  var vl=svg.querySelector('.hl');
  if(!svg||!tip||!dot||!vl)return;
  svg.querySelectorAll('.ht').forEach(function(r){{
    r.onmouseenter=function(){{
      var x=this.getAttribute('data-x'),y=this.getAttribute('data-y');
      dot.setAttribute('cx',x);dot.setAttribute('cy',y);dot.style.display='';
      vl.setAttribute('x1',x);vl.setAttribute('x2',x);vl.style.display='';
      tip.innerHTML='<strong>'+this.getAttribute('data-d')+'</strong><br>'+this.getAttribute('data-v')+' cpl';
      tip.style.display='block';
      var sr=svg.getBoundingClientRect(),cr=card.getBoundingClientRect();
      var vb=svg.viewBox.baseVal;
      var px=(x/vb.width)*sr.width,py=(y/vb.height)*sr.height;
      var l=px+(sr.left-cr.left)+14,t=py+(sr.top-cr.top)-24;
      if(l+140>cr.width)l=px+(sr.left-cr.left)-140;
      tip.style.left=l+'px';tip.style.top=t+'px';
    }};
    r.onmouseleave=function(){{
      dot.style.display='none';vl.style.display='none';tip.style.display='none';
    }};
  }});
}})();
</script>

</body>
</html>"""
    return html


def main():
    data = load_latest()
    html = generate_html(data)
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / "forecast.html"
    path.write_text(html, encoding="utf-8")
    print(f"Dashboard saved to {path}")


if __name__ == "__main__":
    main()
