"""
Fuel Levy Calculator — Daily, Weekly, Monthly + History
Scrapes AIP Terminal Gate Prices and calculates fuel levy.

Formula: Fuel Levy % = ((Average TGP - Base Price) / Base Price) * Weighting

All prices are INCLUSIVE of GST (matching AIP published data).

Modes:
    daily    — Today's TGP (from API)
    weekly   — Prior week Mon-Fri average (from Excel), applicable next week
    monthly  — Prior month 1st-last average (from Excel), applicable next month
    report   — HTML report with all three + history, opens in browser
    backfill — Compute weekly & monthly history from Jan 2026, save to CSVs

Usage:
    python fuel_levy.py                  # daily (console)
    python fuel_levy.py weekly           # prior week (console)
    python fuel_levy.py monthly          # prior month (console)
    python fuel_levy.py report           # HTML report (opens in browser)
    python fuel_levy.py backfill         # populate history from Jan 2026
"""

import csv
import io
import json
import logging
import shutil
import sys
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_PRICE_CPL: float = 80.0          # $0.80 inc GST = 80 cpl (default for CLI)
FUEL_WEIGHTING: float = 0.30          # 30%

# Multiple levy views for HTML report
LEVY_CONFIGS: list[dict] = [
    {"id": "global", "label": "Global Levy", "base_cpl": 80.0, "weighting": 0.30},
    {"id": "dollar", "label": "$1.10 Levy", "base_cpl": 110.0, "weighting": 0.30},
]
AIP_API_URL: str = (
    "https://www.aip.com.au/aip-api-request"
    "?api-path=public/api&call=tgpTables&location="
)
AIP_EXCEL_BASE: str = "https://www.aip.com.au/sites/default/files/download-files"
HISTORY_DIR: Path = Path(__file__).parent / "history"
REPORT_DIR: Path = Path(__file__).parent / "reports"
SHAREPOINT_DIR: Path = Path.home() / "Booth Transport" / "Customers - Customer Documents"

TERMINALS: list[str] = [
    "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Darwin", "Hobart",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fuel_levy")


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------
def calculate_fuel_levy(avg_tgp_cpl: float, base_cpl: float = BASE_PRICE_CPL,
                        weighting: float = FUEL_WEIGHTING) -> dict:
    movement_pct = (avg_tgp_cpl - base_cpl) / base_cpl
    levy_pct = movement_pct * weighting
    return {
        "base_price_cpl": base_cpl,
        "avg_tgp_cpl": round(avg_tgp_cpl, 2),
        "base_dollar": base_cpl / 100.0,
        "avg_tgp_dollar": round(avg_tgp_cpl / 100.0, 4),
        "movement_pct": round(movement_pct * 100, 2),
        "weighting_pct": weighting * 100,
        "levy_pct": round(levy_pct * 100, 2),
    }


# ---------------------------------------------------------------------------
# API fetch (daily)
# ---------------------------------------------------------------------------
def fetch_daily_from_api() -> tuple[float, str, dict[str, float]]:
    request = Request(AIP_API_URL, headers={"User-Agent": "BoothTransport-FuelLevy/1.0"})
    with urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    terminal_prices: dict[str, float] = {}
    date_str: str = ""
    for key, entries in data.items():
        if "Diesel" not in key:
            continue
        latest = entries.get("0")
        if latest is None:
            continue
        terminal_prices[latest["location"]] = float(latest["fuelPrice"])
        if not date_str:
            date_str = latest["date"][:10]

    if not terminal_prices:
        raise ValueError("No diesel prices in API response")

    avg_cpl = sum(terminal_prices.values()) / len(terminal_prices)
    return avg_cpl, date_str, terminal_prices


# ---------------------------------------------------------------------------
# Excel fetch
# ---------------------------------------------------------------------------
def find_latest_excel_url() -> str:
    today = date.today()
    for days_back in range(0, 14):
        d = today - timedelta(days=days_back)
        url = (
            f"{AIP_EXCEL_BASE}/{d.strftime('%Y-%m')}/"
            f"AIP_TGP_Data_{d.strftime('%d-%b-%Y')}.xlsx"
        )
        try:
            req = Request(url, method="HEAD", headers={"User-Agent": "BoothTransport-FuelLevy/1.0"})
            resp = urlopen(req, timeout=10)
            if resp.status == 200:
                log.info("Found Excel: %s", url)
                return url
        except URLError:
            continue
    raise FileNotFoundError("Could not find AIP Excel file in the last 14 days")


def load_excel_diesel_data() -> list[tuple[date, dict[str, float]]]:
    """
    Download AIP Excel and return ALL diesel rows as
    [(row_date, {terminal: price_cpl}), ...] sorted by date.
    """
    import openpyxl

    url = find_latest_excel_url()
    log.info("Downloading Excel...")
    request = Request(url, headers={"User-Agent": "BoothTransport-FuelLevy/1.0"})
    with urlopen(request, timeout=60) as response:
        excel_bytes = response.read()

    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes), read_only=True, data_only=True)
    ws = wb["Diesel TGP"]

    col_map: dict[str, int] = {
        "Sydney": 1, "Melbourne": 2, "Brisbane": 3, "Adelaide": 4,
        "Perth": 5, "Darwin": 6, "Hobart": 7,
    }

    rows: list[tuple[date, dict[str, float]]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        raw = row[0]
        if raw is None:
            continue
        if isinstance(raw, datetime):
            row_date = raw.date()
        elif isinstance(raw, date):
            row_date = raw
        else:
            continue

        prices: dict[str, float] = {}
        for terminal, idx in col_map.items():
            val = row[idx]
            if val is not None:
                prices[terminal] = float(val)
        if prices:
            rows.append((row_date, prices))

    wb.close()
    return rows


def average_for_range(
    all_rows: list[tuple[date, dict[str, float]]],
    start_date: date,
    end_date: date,
) -> tuple[float, dict[str, float], int]:
    """
    Compute simple average of all terminals for days in [start, end].
    Returns (overall_avg, {terminal: avg}, num_days).
    """
    terminal_totals: dict[str, float] = {}
    terminal_counts: dict[str, int] = {}
    days: int = 0

    for row_date, prices in all_rows:
        if row_date < start_date or row_date > end_date:
            continue
        days += 1
        for terminal, price in prices.items():
            terminal_totals[terminal] = terminal_totals.get(terminal, 0.0) + price
            terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1

    if days == 0:
        raise ValueError(f"No data between {start_date} and {end_date}")

    terminal_avgs = {
        t: terminal_totals[t] / terminal_counts[t]
        for t in terminal_totals
    }
    overall = sum(terminal_avgs.values()) / len(terminal_avgs)
    return overall, terminal_avgs, days


# ---------------------------------------------------------------------------
# Date range helpers
# ---------------------------------------------------------------------------
def get_prior_week_range(reference: Optional[date] = None) -> tuple[date, date, str]:
    """Prior week Sat-Fri. Applicable to following Sat-Fri."""
    ref = reference or date.today()
    # Saturday = weekday 5. Find the most recent Saturday on or before ref.
    days_since_sat = (ref.weekday() + 2) % 7  # Sat=0, Sun=1, Mon=2, ...
    this_saturday = ref - timedelta(days=days_since_sat)
    prior_saturday = this_saturday - timedelta(weeks=1)
    prior_friday = prior_saturday + timedelta(days=6)
    # Applicable = this Sat to next Fri
    applicable_end = this_saturday + timedelta(days=6)
    label = f"{this_saturday.strftime('%d %b')} - {applicable_end.strftime('%d %b %Y')}"
    return prior_saturday, prior_friday, label


def get_prior_month_range(reference: Optional[date] = None) -> tuple[date, date, str]:
    ref = reference or date.today()
    first_of_this_month = ref.replace(day=1)
    last_of_prior = first_of_this_month - timedelta(days=1)
    first_of_prior = last_of_prior.replace(day=1)
    applicable_label = ref.strftime("%B %Y")
    return first_of_prior, last_of_prior, applicable_label


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_to_history(mode: str, record: dict) -> None:
    HISTORY_DIR.mkdir(exist_ok=True)
    filepath = HISTORY_DIR / f"{mode}.csv"

    if mode == "daily":
        header = "date,avg_tgp_cpl,levy_pct\n"
        line = f"{record['date']},{record['avg_tgp_cpl']:.2f},{record['levy_pct']:.2f}\n"
        dedup_key = record["date"]
    else:
        header = "period_start,period_end,applicable_to,avg_tgp_cpl,levy_pct,days\n"
        line = (
            f"{record['period_start']},{record['period_end']},"
            f"{record['applicable_to']},{record['avg_tgp_cpl']:.2f},"
            f"{record['levy_pct']:.2f},{record['days']}\n"
        )
        dedup_key = record["period_start"]

    if not filepath.exists():
        filepath.write_text(header, encoding="utf-8")

    existing = filepath.read_text(encoding="utf-8")
    if dedup_key in existing:
        return

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)
    log.info("Saved %s entry for %s", mode, dedup_key)


def load_history(mode: str) -> list[dict]:
    filepath = HISTORY_DIR / f"{mode}.csv"
    if not filepath.exists():
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ---------------------------------------------------------------------------
# Backfill — compute history from Jan 2026
# ---------------------------------------------------------------------------
def run_backfill() -> None:
    log.info("Mode: BACKFILL (from Jan 2026)")
    all_rows = load_excel_diesel_data()

    # Daily: every trading day from 1 Jan 2026
    log.info("Backfilling daily levies...")
    backfill_start = date(2026, 1, 1)
    for row_date, prices in all_rows:
        if row_date < backfill_start:
            continue
        avg_cpl = sum(prices.values()) / len(prices)
        levy = calculate_fuel_levy(avg_cpl)
        save_to_history("daily", {
            "date": str(row_date),
            "avg_tgp_cpl": avg_cpl,
            "levy_pct": levy["levy_pct"],
        })

    # Weekly: every Sat-Fri from 4 Jan 2026 up to last full prior week
    today = date.today()
    days_since_sat = (today.weekday() + 2) % 7
    this_saturday = today - timedelta(days=days_since_sat)
    week_saturday = date(2026, 1, 3)  # first Saturday of 2026

    log.info("Backfilling weekly levies (Sat-Fri)...")
    while week_saturday < this_saturday:
        week_friday = week_saturday + timedelta(days=6)
        applicable_sat = week_saturday + timedelta(weeks=1)
        applicable_fri = applicable_sat + timedelta(days=6)
        applicable_label = f"{applicable_sat.strftime('%d %b')} - {applicable_fri.strftime('%d %b %Y')}"

        try:
            avg_cpl, _, num_days = average_for_range(all_rows, week_saturday, week_friday)
            levy = calculate_fuel_levy(avg_cpl)
            save_to_history("weekly", {
                "period_start": str(week_saturday),
                "period_end": str(week_friday),
                "applicable_to": applicable_label,
                "avg_tgp_cpl": avg_cpl,
                "levy_pct": levy["levy_pct"],
                "days": num_days,
            })
        except ValueError:
            log.warning("No data for week %s", week_saturday)

        week_saturday += timedelta(weeks=1)

    # Monthly: Jan 2026 (applicable Feb), Feb 2026 (applicable Mar), etc.
    log.info("Backfilling monthly levies...")
    month_starts = []
    m = date(2026, 1, 1)
    while m < today.replace(day=1):
        month_starts.append(m)
        if m.month == 12:
            m = date(m.year + 1, 1, 1)
        else:
            m = date(m.year, m.month + 1, 1)

    for month_start in month_starts:
        if month_start.month == 12:
            month_end = date(month_start.year + 1, 1, 1) - timedelta(days=1)
            applicable = date(month_start.year + 1, 1, 1)
        else:
            month_end = date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)
            applicable = date(month_start.year, month_start.month + 1, 1)
        applicable_label = applicable.strftime("%B %Y")

        try:
            avg_cpl, _, num_days = average_for_range(all_rows, month_start, month_end)
            levy = calculate_fuel_levy(avg_cpl)
            save_to_history("monthly", {
                "period_start": str(month_start),
                "period_end": str(month_end),
                "applicable_to": applicable_label,
                "avg_tgp_cpl": avg_cpl,
                "levy_pct": levy["levy_pct"],
                "days": num_days,
            })
        except ValueError:
            log.warning("No data for month %s", month_start)

    log.info("Backfill complete.")


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------
def print_daily_report(avg_cpl: float, terminals: dict[str, float], levy: dict, date_str: str) -> None:
    print(f"\n{'='*60}")
    print(f"  DAILY FUEL LEVY  --  {date_str}")
    print(f"{'='*60}\n")
    print("  Diesel TGP by Terminal (inc GST)")
    print("  " + "-" * 40)
    for t in sorted(terminals):
        print(f"    {t:12s}  {terminals[t]:8.2f} cpl")
    print(f"    {'Average':12s}  {avg_cpl:8.2f} cpl  (${avg_cpl/100:.4f}/L)\n")
    _print_levy_block(levy)


def print_period_report(mode: str, avg_cpl: float, terminal_avgs: dict[str, float],
                        levy: dict, start: date, end: date, applicable: str, days: int) -> None:
    print(f"\n{'='*60}")
    print(f"  {mode.upper()} FUEL LEVY")
    print(f"  Period: {start.strftime('%d %b %Y')} - {end.strftime('%d %b %Y')} ({days} trading days)")
    print(f"  Applicable to: {applicable}")
    print(f"{'='*60}\n")
    print("  Average Diesel TGP by Terminal (inc GST)")
    print("  " + "-" * 40)
    for t in sorted(terminal_avgs):
        print(f"    {t:12s}  {terminal_avgs[t]:8.2f} cpl")
    print(f"    {'AVERAGE':12s}  {avg_cpl:8.2f} cpl  (${avg_cpl/100:.4f}/L)\n")
    _print_levy_block(levy)


def _print_levy_block(levy: dict) -> None:
    print("  Levy Calculation")
    print("  " + "-" * 40)
    print(f"    Base price:        ${levy['base_dollar']:.2f}/L  ({levy['base_price_cpl']:.0f} cpl)")
    print(f"    Avg TGP:           ${levy['avg_tgp_dollar']:.4f}/L  ({levy['avg_tgp_cpl']:.2f} cpl)")
    print(f"    Price movement:    {levy['movement_pct']:+.2f}%")
    print(f"    Fuel weighting:    {levy['weighting_pct']:.0f}%")
    print(f"\n    >>> FUEL LEVY:     {levy['levy_pct']:.2f}% <<<\n")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
def generate_html_report() -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    generated_at = datetime.now().strftime("%d %b %Y %H:%M")

    def fmt_date(iso_str: str) -> str:
        """Convert YYYY-MM-DD to DD-MM-YYYY."""
        try:
            return datetime.strptime(iso_str, "%Y-%m-%d").strftime("%d-%m-%Y")
        except (ValueError, TypeError):
            return iso_str

    # Fetch current data
    try:
        daily_avg, daily_date, daily_terminals = fetch_daily_from_api()
        daily_levy = calculate_fuel_levy(daily_avg)
        save_to_history("daily", {"date": daily_date, "avg_tgp_cpl": daily_avg, "levy_pct": daily_levy["levy_pct"]})
    except Exception as exc:
        log.error("Daily fetch failed: %s", exc)
        daily_avg = daily_levy = daily_terminals = None
        daily_date = "unavailable"

    all_rows = None
    try:
        all_rows = load_excel_diesel_data()
    except Exception as exc:
        log.error("Excel download failed: %s", exc)

    try:
        w_start, w_end, w_applicable = get_prior_week_range()
        weekly_avg, weekly_terminals, weekly_days = average_for_range(all_rows, w_start, w_end)
        weekly_levy = calculate_fuel_levy(weekly_avg)
        save_to_history("weekly", {"period_start": str(w_start), "period_end": str(w_end),
                                   "applicable_to": w_applicable, "avg_tgp_cpl": weekly_avg,
                                   "levy_pct": weekly_levy["levy_pct"], "days": weekly_days})
    except Exception as exc:
        log.error("Weekly failed: %s", exc)
        weekly_avg = weekly_levy = weekly_terminals = None
        w_start = w_end = w_applicable = "unavailable"
        weekly_days = 0

    try:
        m_start, m_end, m_applicable = get_prior_month_range()
        monthly_avg, monthly_terminals, monthly_days = average_for_range(all_rows, m_start, m_end)
        monthly_levy = calculate_fuel_levy(monthly_avg)
        save_to_history("monthly", {"period_start": str(m_start), "period_end": str(m_end),
                                    "applicable_to": m_applicable, "avg_tgp_cpl": monthly_avg,
                                    "levy_pct": monthly_levy["levy_pct"], "days": monthly_days})
    except Exception as exc:
        log.error("Monthly failed: %s", exc)
        monthly_avg = monthly_levy = monthly_terminals = None
        m_start = m_end = m_applicable = "unavailable"
        monthly_days = 0

    # Load history for tables & chart
    daily_history = load_history("daily")
    weekly_history = load_history("weekly")
    monthly_history = load_history("monthly")

    # Build levy cards
    def levy_card(title: str, subtitle: str, applicable: str, levy: Optional[dict],
                  avg_cpl: Optional[float], terminals: Optional[dict],
                  days: Optional[int] = None, accent: str = "#C17F4E") -> str:
        if levy is None:
            return f'<div class="card" style="border-top:4px solid #C45B5B;"><h2>{title}</h2><p class="subtitle">{subtitle}</p><p class="error">Data unavailable</p></div>'
        days_str = f" ({days} trading days)" if days else ""
        t_rows = "".join(f"<tr><td>{t}</td><td>{terminals[t]:.2f}</td></tr>" for t in sorted(terminals)) if terminals else ""
        return f"""
        <div class="card" style="border-top:4px solid {accent};">
            <h2>{title}</h2>
            <p class="subtitle">{subtitle}{days_str}</p>
            <p class="applicable">Applicable to: <strong>{applicable}</strong></p>
            <div class="levy-hero"><span class="levy-number">{levy['levy_pct']:.2f}%</span><span class="levy-label">Fuel Levy</span></div>
            <table class="details">
                <tr><td>Avg Diesel TGP</td><td><strong>${avg_cpl/100:.4f}/L</strong></td></tr>
                <tr><td>Base Price</td><td>${levy['base_dollar']:.2f}/L</td></tr>
                <tr><td>Price Movement</td><td>{levy['movement_pct']:+.2f}%</td></tr>
                <tr><td>Fuel Weighting</td><td>{levy['weighting_pct']:.0f}%</td></tr>
            </table>
            <details><summary>Terminal Breakdown</summary>
                <table class="terminals"><tr><th>Terminal</th><th>Avg cpl</th></tr>{t_rows}
                <tr class="total"><td>Average</td><td><strong>{avg_cpl:.2f}</strong></td></tr></table>
            </details>
        </div>"""

    daily_sub = fmt_date(daily_date) if daily_date and daily_date != "unavailable" else "unavailable"
    weekly_sub = f"{w_start.strftime('%d-%m-%Y')} - {w_end.strftime('%d-%m-%Y')}" if isinstance(w_start, date) else "unavailable"
    monthly_sub = f"{m_start.strftime('%d-%m-%Y')} - {m_end.strftime('%d-%m-%Y')}" if isinstance(m_start, date) else "unavailable"
    daily_app = fmt_date(daily_date) if daily_date and daily_date != "unavailable" else "N/A"
    weekly_app = w_applicable if isinstance(w_applicable, str) else "N/A"
    monthly_app = m_applicable if isinstance(m_applicable, str) else "N/A"

    # Shared chart layout constants used by build_page
    chart_w, chart_h = 960, 280
    pad_l, pad_r, pad_t, pad_b = 55, 15, 25, 45
    plot_w = chart_w - pad_l - pad_r
    plot_h = chart_h - pad_t - pad_b
    chart_data = daily_history if len(daily_history) >= 2 else weekly_history

    if "date" in (chart_data[0] if chart_data else {}):
        full_dates = [fmt_date(r["date"]) for r in chart_data]
        short_dates = [r["date"][8:10] + "-" + r["date"][5:7] for r in chart_data]
    elif chart_data:
        full_dates = [r["applicable_to"] for r in chart_data]
        short_dates = [r["applicable_to"].split(" - ")[0] for r in chart_data]
    else:
        full_dates = []
        short_dates = []

    tgps = [float(r.get("avg_tgp_cpl", 0)) for r in chart_data]
    hit_w = plot_w / max(1, len(chart_data) - 1) if len(chart_data) >= 2 else plot_w

    # ---------------------------------------------------------------
    # Build a page content block for each levy config
    # ---------------------------------------------------------------
    def build_page(cfg: dict) -> str:
        base = cfg["base_cpl"]
        wt = cfg["weighting"]

        def lev(tgp: float) -> dict:
            return calculate_fuel_levy(tgp, base_cpl=base, weighting=wt)

        d_levy = lev(daily_avg) if daily_avg else None
        w_levy = lev(weekly_avg) if weekly_avg else None
        m_levy = lev(monthly_avg) if monthly_avg else None

        cards = '<div class="grid">'
        cards += levy_card("Daily", daily_sub, daily_app, d_levy, daily_avg, daily_terminals, accent="#C17F4E")
        cards += levy_card("Weekly", weekly_sub, weekly_app, w_levy, weekly_avg, weekly_terminals, weekly_days, accent="#3B8A6A")
        cards += levy_card("Monthly", monthly_sub, monthly_app, m_levy, monthly_avg, monthly_terminals, monthly_days, accent="#C49B3B")
        cards += "</div>"

        formula = f'<div class="formula">Fuel Levy = <code>((Avg Diesel TGP - Base ${base/100:.2f}/L) / Base) x {wt*100:.0f}% weighting</code></div>'

        # Recalculate history levies for this config
        d_rows = ""
        for row in reversed(daily_history):
            tgp = float(row["avg_tgp_cpl"])
            l = lev(tgp)
            d_rows += f"<tr><td>{fmt_date(row['date'])}</td><td>${tgp/100:.4f}/L</td><td><strong>{l['levy_pct']:.2f}%</strong></td></tr>\n"

        w_rows = ""
        for row in reversed(weekly_history):
            tgp = float(row["avg_tgp_cpl"])
            l = lev(tgp)
            w_rows += (
                f"<tr><td>{fmt_date(row['period_start'])}</td><td>{fmt_date(row['period_end'])}</td>"
                f"<td>{row['applicable_to']}</td><td>${tgp/100:.4f}/L</td>"
                f"<td><strong>{l['levy_pct']:.2f}%</strong></td><td>{row['days']}</td></tr>\n"
            )

        m_rows = ""
        for row in reversed(monthly_history):
            tgp = float(row["avg_tgp_cpl"])
            l = lev(tgp)
            m_rows += (
                f"<tr><td>{fmt_date(row['period_start'])}</td><td>{fmt_date(row['period_end'])}</td>"
                f"<td>{row['applicable_to']}</td><td>${tgp/100:.4f}/L</td>"
                f"<td><strong>{l['levy_pct']:.2f}%</strong></td><td>{row['days']}</td></tr>\n"
            )

        cid = cfg["id"]  # unique prefix for tab IDs

        # Build chart for this config
        page_chart = ""
        if len(chart_data) >= 2:
            p_levies = [lev(float(r.get("avg_tgp_cpl", 0)))["levy_pct"] for r in chart_data]
            p_min = min(p_levies) - 2
            p_max = max(p_levies) + 2
            p_span = p_max - p_min if p_max != p_min else 1
            p_points = []
            for i, pl in enumerate(p_levies):
                x = pad_l + (i / (len(p_levies) - 1)) * plot_w
                y = pad_t + plot_h - ((pl - p_min) / p_span) * plot_h
                p_points.append((x, y, pl, full_dates[i], short_dates[i], tgps[i]))

            p_polyline = " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in p_points)
            p_area = f"M {p_points[0][0]:.1f},{pad_t + plot_h} " + " ".join(f"L {p[0]:.1f},{p[1]:.1f}" for p in p_points) + f" L {p_points[-1][0]:.1f},{pad_t + plot_h} Z"

            p_grid = ""
            for gi in range(5):
                val = p_min + (gi / 4) * p_span
                gy = pad_t + plot_h - (gi / 4) * plot_h
                p_grid += f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{chart_w - pad_r}" y2="{gy:.1f}" stroke="#E0DDD8" stroke-width="1"/>\n'
                p_grid += f'<text x="{pad_l - 8}" y="{gy:.1f}" text-anchor="end" fill="#999" font-size="11" dy="4">{val:.1f}%</text>\n'

            p_hovers = ""
            for i, (x, y, pl, fd, sd, tgp) in enumerate(p_points):
                p_hovers += (
                    f'<rect x="{x - hit_w/2:.1f}" y="{pad_t}" width="{hit_w:.1f}" height="{plot_h}" '
                    f'fill="transparent" class="hover-target" '
                    f'data-x="{x:.1f}" data-y="{y:.1f}" data-levy="{pl:.2f}" '
                    f'data-date="{fd}" data-tgp="{tgp/100:.4f}"/>\n'
                )

            p_xlabels = ""
            step = max(1, len(p_points) // 10)
            for i, (x, y, pl, fd, sd, tgp) in enumerate(p_points):
                if i % step == 0 or i == len(p_points) - 1:
                    p_xlabels += f'<text x="{x:.1f}" y="{chart_h - 6}" text-anchor="middle" fill="#999" font-size="10">{sd}</text>\n'

            p_fl = ""
            if p_points:
                p_fl += f'<text x="{p_points[0][0]:.1f}" y="{p_points[0][1]:.1f}" dy="-10" text-anchor="start" fill="#1C1F26" font-size="11" font-weight="600">{p_points[0][2]:.2f}%</text>\n'
                p_fl += f'<text x="{p_points[-1][0]:.1f}" y="{p_points[-1][1]:.1f}" dy="-10" text-anchor="end" fill="#1C1F26" font-size="11" font-weight="600">{p_points[-1][2]:.2f}%</text>\n'

            page_chart = f"""
            <div class="chart-card" style="padding:16px;">
                <h3 style="margin-bottom:8px;">Daily Fuel Levy Trend</h3>
                <div style="position:relative;">
                    <svg class="trendChart" viewBox="0 0 {chart_w} {chart_h}" style="width:100%;height:auto;display:block;">
                        {p_grid}
                        <path d="{p_area}" fill="#C17F4E" fill-opacity="0.08"/>
                        <polyline points="{p_polyline}" fill="none" stroke="#C17F4E" stroke-width="2.5" stroke-linejoin="round"/>
                        {p_fl}
                        {p_xlabels}
                        <circle class="hoverDot" cx="0" cy="0" r="5" fill="#C17F4E" stroke="#fff" stroke-width="2" style="display:none;pointer-events:none;"/>
                        <line class="hoverLine" x1="0" y1="{pad_t}" x2="0" y2="{pad_t + plot_h}" stroke="#C17F4E" stroke-width="1" stroke-dasharray="4,3" style="display:none;pointer-events:none;"/>
                        {p_hovers}
                    </svg>
                    <div class="chart-tooltip" style="display:none;position:absolute;background:#1C1F26;color:#F5F5F3;padding:8px 12px;border-radius:6px;font-size:13px;pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.2);z-index:10;"></div>
                </div>
            </div>"""

        return f"""
        {formula}
        {cards}
        <div class="history-section">
            <h2>Levy History (from January 2026)</h2>
            {page_chart}
            <div class="tabs">
                <button class="tab active" onclick="switchHistoryTab('{cid}','daily')">Daily</button>
                <button class="tab" onclick="switchHistoryTab('{cid}','weekly')">Weekly</button>
                <button class="tab" onclick="switchHistoryTab('{cid}','monthly')">Monthly</button>
            </div>
            <div id="{cid}-daily" class="htab-content htab-{cid} active">
                <table class="history"><tr><th>Date</th><th>Avg TGP ($/L)</th><th>Fuel Levy</th></tr>{d_rows or "<tr><td colspan='3' style='text-align:center;padding:20px;color:#999;'>No data yet</td></tr>"}</table>
            </div>
            <div id="{cid}-weekly" class="htab-content htab-{cid}">
                <table class="history"><tr><th>Period Start</th><th>Period End</th><th>Applicable To</th><th>Avg TGP ($/L)</th><th>Fuel Levy</th><th>Days</th></tr>{w_rows or "<tr><td colspan='6' style='text-align:center;padding:20px;color:#999;'>No data yet</td></tr>"}</table>
            </div>
            <div id="{cid}-monthly" class="htab-content htab-{cid}">
                <table class="history"><tr><th>Period Start</th><th>Period End</th><th>Applicable To</th><th>Avg TGP ($/L)</th><th>Fuel Levy</th><th>Days</th></tr>{m_rows or "<tr><td colspan='6' style='text-align:center;padding:20px;color:#999;'>No data yet</td></tr>"}</table>
            </div>
        </div>"""

    # Build sidebar nav items and page divs
    sidebar_items = ""
    page_divs = ""
    for i, cfg in enumerate(LEVY_CONFIGS):
        active = " active" if i == 0 else ""
        sidebar_items += f'<button class="sidebar-btn{active}" onclick="switchPage(\'{cfg["id"]}\')">{cfg["label"]}</button>\n'
        display = "block" if i == 0 else "none"
        page_divs += f'<div id="page-{cfg["id"]}" class="page-content" style="display:{display};">{build_page(cfg)}</div>\n'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fuel Levy Report - Booth Transport</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:Calibri,'Segoe UI',sans-serif; background:#F5F5F3; color:#1C1F26; display:flex; min-height:100vh; }}

    /* Sidebar */
    .sidebar {{ width:200px; background:#1C1F26; color:#F5F5F3; padding:24px 0; flex-shrink:0; position:fixed; top:0; left:0; height:100vh; display:flex; flex-direction:column; }}
    .sidebar-brand {{ padding:0 20px 20px; border-bottom:1px solid #333; margin-bottom:16px; }}
    .sidebar-brand h1 {{ font-size:16px; font-weight:600; line-height:1.3; }}
    .sidebar-brand .meta {{ font-size:11px; color:#888; margin-top:6px; }}
    .sidebar-btn {{ display:block; width:100%; text-align:left; padding:12px 20px; background:none; border:none; color:#999; font-family:inherit; font-size:14px; cursor:pointer; transition:all .15s; }}
    .sidebar-btn:hover {{ background:#2a2d36; color:#F5F5F3; }}
    .sidebar-btn.active {{ background:#C17F4E; color:#fff; font-weight:600; }}

    /* Main content */
    .main {{ margin-left:200px; flex:1; padding:24px 32px; max-width:1100px; }}

    .formula {{ background:#fff; border:1px solid #E0DDD8; border-radius:6px; padding:12px 20px; margin-bottom:24px; font-size:14px; color:#555; text-align:center; }}
    .formula code {{ background:#F0EDEA; padding:2px 8px; border-radius:4px; font-family:Consolas,monospace; font-size:13px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:20px; margin-bottom:32px; }}
    .card {{ background:#fff; border-radius:8px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
    .card h2 {{ font-size:16px; font-weight:600; margin-bottom:4px; }}
    .card .subtitle {{ font-size:13px; color:#777; margin-bottom:4px; }}
    .card .applicable {{ font-size:13px; color:#3B8A6A; margin-bottom:16px; }}
    .card .error {{ color:#C45B5B; font-weight:600; padding:20px 0; }}
    .levy-hero {{ text-align:center; padding:20px 0; margin-bottom:16px; background:#FAFAF8; border-radius:6px; }}
    .levy-number {{ display:block; font-size:42px; font-weight:700; color:#C17F4E; }}
    .levy-label {{ font-size:13px; color:#999; text-transform:uppercase; letter-spacing:1px; }}
    table.details {{ width:100%; font-size:14px; border-collapse:collapse; margin-bottom:12px; }}
    table.details td {{ padding:6px 0; border-bottom:1px solid #F0EDEA; }}
    table.details td:last-child {{ text-align:right; }}
    details {{ font-size:13px; color:#777; }}
    details summary {{ cursor:pointer; padding:6px 0; }}
    table.terminals {{ width:100%; font-size:13px; border-collapse:collapse; margin-top:8px; }}
    table.terminals th, table.terminals td {{ padding:4px 0; border-bottom:1px solid #F0EDEA; text-align:left; }}
    table.terminals td:last-child, table.terminals th:last-child {{ text-align:right; }}
    table.terminals tr.total td {{ border-top:2px solid #1C1F26; font-weight:600; }}

    .history-section {{ margin-bottom:32px; }}
    .history-section h2 {{ font-size:18px; font-weight:600; margin-bottom:16px; padding-bottom:8px; border-bottom:2px solid #E0DDD8; }}
    .chart-card {{ background:#fff; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:20px; }}
    .chart-card h3 {{ font-size:15px; font-weight:600; margin-bottom:8px; color:#555; }}
    table.history {{ width:100%; font-size:13px; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
    table.history th {{ background:#1C1F26; color:#F5F5F3; padding:10px 12px; text-align:left; font-weight:500; }}
    table.history td {{ padding:8px 12px; border-bottom:1px solid #F0EDEA; }}
    table.history tr:hover {{ background:#FAFAF8; }}

    .tabs {{ display:flex; gap:0; margin-bottom:0; }}
    .tab {{ padding:10px 24px; background:#E0DDD8; border:none; cursor:pointer; font-family:inherit; font-size:14px; font-weight:500; color:#555; border-radius:8px 8px 0 0; }}
    .tab.active {{ background:#fff; color:#1C1F26; font-weight:600; }}
    .htab-content {{ display:none; }}
    .htab-content.active {{ display:block; }}

    footer {{ text-align:center; font-size:12px; color:#999; padding:16px 0; }}
    @media print {{
        .sidebar {{ display:none; }}
        .main {{ margin-left:0; }}
        body {{ background:#fff; }}
        .levy-hero {{ background:#FAFAF8; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
        .card,.chart-card {{ break-inside:avoid; }}
        table.history th {{ background:#1C1F26; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
    }}
</style>
</head>
<body>

<div class="sidebar">
    <div class="sidebar-brand">
        <h1>Booth Transport</h1>
        <div class="meta">Fuel Levy Report<br>{generated_at}</div>
    </div>
    {sidebar_items}
</div>

<div class="main">
    {page_divs}
    <footer>
        Data sourced from Australian Institute of Petroleum (aip.com.au). All prices inclusive of GST.
    </footer>
</div>

<script>
function switchPage(id) {{
    document.querySelectorAll('.sidebar-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page-content').forEach(p => p.style.display = 'none');
    event.target.classList.add('active');
    document.getElementById('page-' + id).style.display = 'block';
    initChartTooltips();
}}

function switchHistoryTab(prefix, tab) {{
    var parent = event.target.closest('.history-section');
    parent.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    parent.querySelectorAll('.htab-content').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById(prefix + '-' + tab).classList.add('active');
}}

function initChartTooltips() {{
    document.querySelectorAll('.trendChart').forEach(function(svg) {{
        var card = svg.closest('.chart-card');
        if (!card) return;
        var tooltip = card.querySelector('.chart-tooltip');
        var dot = svg.querySelector('.hoverDot');
        var vline = svg.querySelector('.hoverLine');
        if (!tooltip || !dot || !vline) return;

        svg.querySelectorAll('.hover-target').forEach(function(rect) {{
            rect.onmouseenter = function() {{
                var x = this.getAttribute('data-x');
                var y = this.getAttribute('data-y');
                var levy = this.getAttribute('data-levy');
                var dt = this.getAttribute('data-date');
                var tgp = this.getAttribute('data-tgp');
                dot.setAttribute('cx', x);
                dot.setAttribute('cy', y);
                dot.style.display = '';
                vline.setAttribute('x1', x);
                vline.setAttribute('x2', x);
                vline.style.display = '';
                tooltip.innerHTML = '<strong>' + dt + '</strong><br>Levy: ' + levy + '%<br>TGP: $' + tgp + '/L';
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
}}
initChartTooltips();
</script>

</body>
</html>"""

    report_path = REPORT_DIR / f"fuel-levy-{date.today().isoformat()}.html"
    report_path.write_text(html, encoding="utf-8")
    latest_path = REPORT_DIR / "latest.html"
    latest_path.write_text(html, encoding="utf-8")
    # index.html for GitHub Pages
    index_path = REPORT_DIR / "index.html"
    index_path.write_text(html, encoding="utf-8")
    # Copy to SharePoint if available
    if SHAREPOINT_DIR.exists():
        sp_path = SHAREPOINT_DIR / "Fuel Levy Report.html"
        shutil.copy2(report_path, sp_path)
        log.info("Copied to SharePoint: %s", sp_path)
    log.info("Report saved to %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------
def run_daily() -> None:
    log.info("Mode: DAILY")
    avg_cpl, date_str, terminals = fetch_daily_from_api()
    levy = calculate_fuel_levy(avg_cpl)
    print_daily_report(avg_cpl, terminals, levy, date_str)
    save_to_history("daily", {"date": date_str, "avg_tgp_cpl": avg_cpl, "levy_pct": levy["levy_pct"]})


def run_weekly() -> None:
    log.info("Mode: WEEKLY")
    start, end, applicable = get_prior_week_range()
    all_rows = load_excel_diesel_data()
    avg_cpl, tavgs, days = average_for_range(all_rows, start, end)
    levy = calculate_fuel_levy(avg_cpl)
    print_period_report("weekly", avg_cpl, tavgs, levy, start, end, applicable, days)
    save_to_history("weekly", {"period_start": str(start), "period_end": str(end),
                               "applicable_to": applicable, "avg_tgp_cpl": avg_cpl,
                               "levy_pct": levy["levy_pct"], "days": days})


def run_monthly() -> None:
    log.info("Mode: MONTHLY")
    start, end, applicable = get_prior_month_range()
    all_rows = load_excel_diesel_data()
    avg_cpl, tavgs, days = average_for_range(all_rows, start, end)
    levy = calculate_fuel_levy(avg_cpl)
    print_period_report("monthly", avg_cpl, tavgs, levy, start, end, applicable, days)
    save_to_history("monthly", {"period_start": str(start), "period_end": str(end),
                                "applicable_to": applicable, "avg_tgp_cpl": avg_cpl,
                                "levy_pct": levy["levy_pct"], "days": days})


def run_report() -> None:
    log.info("Mode: HTML REPORT")
    path = generate_html_report()
    print(f"\nReport: {path}")
    if "--no-open" not in sys.argv:
        webbrowser.open(path.as_uri())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "daily"
    runners = {
        "daily": run_daily, "weekly": run_weekly, "monthly": run_monthly,
        "report": run_report, "backfill": run_backfill,
    }
    runner = runners.get(mode)
    if runner is None:
        print(f"Unknown mode: {mode}")
        print("Usage: python fuel_levy.py [daily|weekly|monthly|report|backfill]")
        sys.exit(1)
    runner()


if __name__ == "__main__":
    main()
