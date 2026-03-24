"""
TGP Multi-Factor Forecast Model
Predicts Australian diesel Terminal Gate Prices using:
  - WTI crude oil (USD/bbl)
  - AUD/USD exchange rate
  - Heating Oil crack spread (diesel refining margin proxy)
  - Fuel excise (fixed, ATO schedule)

Designed to run daily via GitHub Actions at ~11am AEST.
Outputs forecast to forecast/ directory and appends to prediction log.

Usage:
    python tgp_forecast.py              # full run: train + predict + save
    python tgp_forecast.py --json       # output JSON summary to stdout
"""

import csv
import io
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import statsmodels.api as sm
import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FORECAST_DIR: Path = Path(__file__).parent / "forecast"
HISTORY_DIR: Path = Path(__file__).parent / "history"
DATA_DIR: Path = Path(__file__).parent / "data"

# yfinance tickers
TICKER_WTI: str = "CL=F"
TICKER_AUDUSD: str = "AUDUSD=X"
TICKER_HEATING_OIL: str = "HO=F"
TICKER_RBOB: str = "RB=F"

# Barrel-to-litre conversion: 1 barrel = 158.987 litres
BARREL_TO_LITRES: float = 158.987

# Gallons per barrel (for RBOB/HO conversion from USD/gallon to USD/barrel)
GALLONS_PER_BARREL: float = 42.0

# Historical yfinance lookback
LOOKBACK_YEARS: int = 10

# AIP TGP data source
AIP_EXCEL_BASE: str = "https://www.aip.com.au/sites/default/files/download-files"

# Australian fuel excise schedule (diesel, cpl)
# Source: ATO. Updated twice yearly (Feb and Aug) via CPI indexation.
# Only need entries from 2016+ (yfinance lookback period)
EXCISE_SCHEDULE: list[tuple[str, float]] = [
    ("2016-02-01", 39.5),
    ("2016-08-01", 39.9),
    ("2017-02-01", 40.1),
    ("2017-08-01", 40.3),
    ("2018-02-01", 40.9),
    ("2018-08-01", 41.2),
    ("2019-02-01", 41.5),
    ("2019-08-01", 42.3),
    ("2020-02-01", 42.7),
    ("2020-08-01", 43.0),
    ("2021-02-01", 43.3),
    ("2021-08-01", 44.2),
    ("2022-02-01", 44.2),
    ("2022-03-30", 22.1),   # COVID halving (30 Mar - 28 Sep 2022)
    ("2022-09-29", 46.0),   # restored + indexation
    ("2023-02-01", 47.7),
    ("2023-08-01", 48.8),
    ("2024-02-01", 49.6),
    ("2024-08-01", 50.6),
    ("2025-02-01", 51.1),
    ("2025-08-01", 51.8),
    ("2026-02-01", 52.3),
]

GST_RATE: float = 0.10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tgp_forecast")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def fetch_yfinance_data(start_date: str) -> pd.DataFrame:
    """Fetch WTI crude, AUD/USD, Heating Oil, and RBOB from yfinance."""
    tickers = {
        "WTI_USD": TICKER_WTI,
        "AUDUSD": TICKER_AUDUSD,
        "HO_USD_GAL": TICKER_HEATING_OIL,
        "RBOB_USD_GAL": TICKER_RBOB,
    }
    frames: dict[str, pd.Series] = {}

    for name, ticker in tickers.items():
        log.info("Fetching %s (%s)...", name, ticker)
        try:
            df = yf.download(ticker, start=start_date, progress=False, auto_adjust=True)
            if df.empty:
                log.warning("No data for %s", ticker)
                continue
            # yfinance returns MultiIndex columns when single ticker; flatten
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            series = df["Close"].copy()
            series.name = name
            # Flatten timezone-aware index to date-only
            series.index = series.index.tz_localize(None) if series.index.tz else series.index
            frames[name] = series
        except Exception as exc:
            log.error("Failed to fetch %s: %s", ticker, exc)

    if not frames:
        raise RuntimeError("Could not fetch any market data from yfinance")

    combined = pd.DataFrame(frames)
    combined.index.name = "Date"
    return combined


def load_aip_tgp_history() -> pd.DataFrame:
    """
    Load diesel TGP history. Tries local CSV cache first,
    then falls back to downloading AIP Excel.
    """
    csv_path = DATA_DIR / "diesel_tgp_history.csv"

    if csv_path.exists():
        log.info("Loading TGP history from %s", csv_path)
        df = pd.read_csv(csv_path, parse_dates=["date"])
        df = df.set_index("date")
        return df

    # Fallback: try history/daily.csv from the fuel levy script
    daily_csv = HISTORY_DIR / "daily.csv"
    if daily_csv.exists():
        log.info("Loading TGP from fuel levy daily history")
        df = pd.read_csv(daily_csv, parse_dates=["date"])
        df = df.rename(columns={"avg_tgp_cpl": "diesel_tgp"})
        df = df.set_index("date")[["diesel_tgp"]]
        return df

    raise FileNotFoundError(
        f"No TGP history found. Place diesel_tgp_history.csv in {DATA_DIR}/ "
        f"or ensure {daily_csv} exists."
    )


def extract_tgp_from_excel(excel_path: str) -> pd.DataFrame:
    """Extract national average diesel TGP from an AIP Excel file."""
    import openpyxl

    log.info("Extracting TGP from %s", excel_path)
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb["Diesel TGP"]

    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        raw_date = row[0]
        if raw_date is None:
            continue
        if isinstance(raw_date, datetime):
            row_date = raw_date.date()
        elif isinstance(raw_date, date):
            row_date = raw_date
        else:
            continue

        # National average is column 8 (index 8)
        nat_avg = row[8] if len(row) > 8 else None
        if nat_avg is not None:
            try:
                rows.append({"date": row_date, "diesel_tgp": float(nat_avg)})
            except (ValueError, TypeError):
                continue

    wb.close()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def save_tgp_cache(df: pd.DataFrame) -> None:
    """Save TGP history to CSV cache for faster future loads."""
    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / "diesel_tgp_history.csv"
    df.to_csv(csv_path)
    log.info("Saved TGP cache to %s (%d rows)", csv_path, len(df))


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------
def get_excise_for_date(d: pd.Timestamp) -> float:
    """Look up diesel excise rate for a given date."""
    excise = EXCISE_SCHEDULE[0][1]
    for date_str, rate in EXCISE_SCHEDULE:
        if d >= pd.Timestamp(date_str):
            excise = rate
    return excise


def engineer_features(market: pd.DataFrame, tgp: pd.DataFrame) -> pd.DataFrame:
    """
    Merge market data with TGP and create derived features.
    All features are forward-filled to handle holiday misalignment.
    """
    # Forward-fill market data (weekends, holidays)
    market_daily = market.resample("D").ffill()
    tgp_daily = tgp.resample("D").ffill()

    combined = market_daily.join(tgp_daily, how="inner").dropna(
        subset=["WTI_USD", "AUDUSD", "diesel_tgp"]
    )

    if combined.empty:
        raise ValueError("No overlapping data between market and TGP series")

    # Core derived features
    combined["WTI_AUD"] = combined["WTI_USD"] / combined["AUDUSD"]
    combined["WTI_AUD_CPL"] = combined["WTI_AUD"] / BARREL_TO_LITRES * 100

    # Crack spreads (convert from USD/gallon to USD/barrel, then to AUD/barrel)
    if "HO_USD_GAL" in combined.columns:
        combined["HO_USD_BBL"] = combined["HO_USD_GAL"] * GALLONS_PER_BARREL
        combined["diesel_crack_usd"] = combined["HO_USD_BBL"] - combined["WTI_USD"]
        combined["diesel_crack_aud"] = combined["diesel_crack_usd"] / combined["AUDUSD"]
        combined["diesel_crack_aud_cpl"] = (
            combined["diesel_crack_aud"] / BARREL_TO_LITRES * 100
        )
    else:
        combined["diesel_crack_aud_cpl"] = 0.0

    if "RBOB_USD_GAL" in combined.columns:
        combined["RBOB_USD_BBL"] = combined["RBOB_USD_GAL"] * GALLONS_PER_BARREL
        combined["petrol_crack_usd"] = combined["RBOB_USD_BBL"] - combined["WTI_USD"]
        combined["petrol_crack_aud"] = combined["petrol_crack_usd"] / combined["AUDUSD"]
    else:
        combined["petrol_crack_aud"] = 0.0

    # Excise
    combined["excise"] = combined.index.map(get_excise_for_date)

    # GST component (GST is on the total price, so GST = TGP * rate / (1 + rate))
    combined["gst_component"] = (
        combined["diesel_tgp"] * GST_RATE / (1 + GST_RATE)
    )

    # Lagged crude (for asymmetric analysis)
    combined["WTI_AUD_CPL_lag7"] = combined["WTI_AUD_CPL"].shift(7)
    combined["WTI_AUD_CPL_lag14"] = combined["WTI_AUD_CPL"].shift(14)
    combined["WTI_AUD_CPL_chg"] = combined["WTI_AUD_CPL"].diff()
    combined["WTI_AUD_CPL_chg_pos"] = combined["WTI_AUD_CPL_chg"].clip(lower=0)
    combined["WTI_AUD_CPL_chg_neg"] = combined["WTI_AUD_CPL_chg"].clip(upper=0)

    return combined


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------
def train_multifactor_model(
    data: pd.DataFrame,
) -> dict:
    """
    Train OLS regression models:
      1. Baseline: TGP ~ WTI_USD
      2. FX-adjusted: TGP ~ WTI_AUD_CPL
      3. Full: TGP ~ WTI_AUD_CPL + diesel_crack_aud_cpl + excise
    Returns dict with model objects and diagnostics.
    """
    results = {}

    # Weekly aggregation for model training (reduces autocorrelation noise)
    weekly = data.resample("W").last().dropna(
        subset=["WTI_USD", "WTI_AUD_CPL", "diesel_tgp"]
    )

    # Use 1-week lag: predict TGP from LAST week's inputs
    for col in ["WTI_USD", "WTI_AUD_CPL", "diesel_crack_aud_cpl", "excise", "AUDUSD"]:
        if col in weekly.columns:
            weekly[f"{col}_lag1"] = weekly[col].shift(1)

    weekly = weekly.dropna()

    if len(weekly) < 20:
        raise ValueError(f"Insufficient data for model training ({len(weekly)} weeks)")

    # Model 1: Baseline (WTI USD only)
    y = weekly["diesel_tgp"]
    X1 = sm.add_constant(weekly["WTI_USD_lag1"])
    m1 = sm.OLS(y, X1).fit()
    results["baseline"] = m1

    # Model 2: FX-adjusted (WTI in AUD cpl)
    X2 = sm.add_constant(weekly["WTI_AUD_CPL_lag1"])
    m2 = sm.OLS(y, X2).fit()
    results["fx_adjusted"] = m2

    # Model 3: Full multi-factor
    factor_cols = ["WTI_AUD_CPL_lag1", "diesel_crack_aud_cpl_lag1", "excise_lag1"]
    available = [c for c in factor_cols if c in weekly.columns]
    X3 = sm.add_constant(weekly[available])
    m3 = sm.OLS(y, X3).fit()
    results["full"] = m3

    # Store training metadata
    results["weekly_data"] = weekly
    results["n_weeks"] = len(weekly)
    results["date_range"] = (weekly.index.min(), weekly.index.max())

    return results


# ---------------------------------------------------------------------------
# Asymmetric lag analysis (rockets and feathers)
# ---------------------------------------------------------------------------
def asymmetric_lag_analysis(data: pd.DataFrame) -> dict:
    """
    Estimate asymmetric pass-through: how fast do crude price INCREASES
    vs DECREASES flow through to TGP?
    Uses weekly changes with distributed lags.
    """
    weekly = data.resample("W").last().dropna(subset=["WTI_AUD_CPL", "diesel_tgp"])
    weekly["tgp_chg"] = weekly["diesel_tgp"].diff()
    weekly["crude_chg"] = weekly["WTI_AUD_CPL"].diff()
    weekly["crude_up"] = weekly["crude_chg"].clip(lower=0)
    weekly["crude_down"] = weekly["crude_chg"].clip(upper=0)

    # Create lagged positive and negative changes (0-8 weeks)
    max_lag = 8
    lag_cols_up = []
    lag_cols_down = []
    for lag in range(0, max_lag + 1):
        up_col = f"crude_up_lag{lag}"
        down_col = f"crude_down_lag{lag}"
        weekly[up_col] = weekly["crude_up"].shift(lag)
        weekly[down_col] = weekly["crude_down"].shift(lag)
        lag_cols_up.append(up_col)
        lag_cols_down.append(down_col)

    weekly = weekly.dropna()

    if len(weekly) < 30:
        return {"error": "Insufficient data for asymmetric analysis"}

    # Regression with distributed lags
    X = sm.add_constant(weekly[lag_cols_up + lag_cols_down])
    y = weekly["tgp_chg"]
    model = sm.OLS(y, X).fit()

    # Cumulative impulse response
    up_coeffs = [model.params.get(f"crude_up_lag{i}", 0) for i in range(max_lag + 1)]
    down_coeffs = [model.params.get(f"crude_down_lag{i}", 0) for i in range(max_lag + 1)]
    cum_up = np.cumsum(up_coeffs)
    cum_down = np.cumsum(down_coeffs)

    # Find weeks to 90% pass-through
    total_up = cum_up[-1] if cum_up[-1] != 0 else 1
    total_down = cum_down[-1] if cum_down[-1] != 0 else 1

    weeks_90_up = next(
        (i for i, v in enumerate(cum_up) if v >= 0.9 * total_up), max_lag
    )
    weeks_90_down = next(
        (i for i, v in enumerate(cum_down) if abs(v) >= 0.9 * abs(total_down)), max_lag
    )

    return {
        "model": model,
        "cum_up": cum_up.tolist(),
        "cum_down": cum_down.tolist(),
        "total_up_passthrough": float(total_up),
        "total_down_passthrough": float(total_down),
        "weeks_90pct_up": weeks_90_up,
        "weeks_90pct_down": weeks_90_down,
        "r_squared": model.rsquared,
    }


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------
def current_conditions(data: pd.DataFrame) -> dict:
    """Extract the latest available values for all inputs."""
    latest = data.dropna(subset=["WTI_USD", "AUDUSD"]).iloc[-1]
    return {
        "date": str(latest.name.date()),
        "wti_usd": float(latest["WTI_USD"]),
        "audusd": float(latest["AUDUSD"]),
        "wti_aud_cpl": float(latest["WTI_AUD_CPL"]),
        "diesel_crack_aud_cpl": float(latest.get("diesel_crack_aud_cpl", 0)),
        "excise": float(latest["excise"]),
        "diesel_tgp_actual": float(latest.get("diesel_tgp", 0)),
    }


def scenario_forecast(model_results: dict, conditions: dict) -> list[dict]:
    """
    Generate TGP forecasts under various crude / FX / margin scenarios.
    Uses the full multi-factor model.
    """
    model = model_results["full"]
    param_names = list(model.params.index)

    wti_scenarios = [65, 75, 85, 95, 101, 110, 120, 130]
    fx_scenarios = [0.56, 0.60, 0.64, 0.68, 0.72]
    crack_scenarios = [15, 25, 35]

    forecasts = []
    for wti_usd in wti_scenarios:
        for audusd in fx_scenarios:
            for crack_usd in crack_scenarios:
                wti_aud = wti_usd / audusd
                wti_aud_cpl = wti_aud / BARREL_TO_LITRES * 100
                crack_aud = crack_usd / audusd
                crack_aud_cpl = crack_aud / BARREL_TO_LITRES * 100
                excise = conditions["excise"]

                # Build predictor vector matching model params
                x_dict = {
                    "const": 1,
                    "WTI_AUD_CPL_lag1": wti_aud_cpl,
                    "diesel_crack_aud_cpl_lag1": crack_aud_cpl,
                    "excise_lag1": excise,
                }
                x_vals = [x_dict.get(p, 0) for p in param_names]
                predicted_tgp = float(model.predict(pd.DataFrame([x_vals], columns=param_names))[0])

                forecasts.append({
                    "wti_usd": wti_usd,
                    "audusd": audusd,
                    "crack_spread_usd": crack_usd,
                    "wti_aud_cpl": round(wti_aud_cpl, 1),
                    "predicted_diesel_tgp": round(predicted_tgp, 1),
                })

    return forecasts


def decompose_current_tgp(model_results: dict, conditions: dict) -> dict:
    """
    Break down current diesel TGP into its component drivers
    using the full model coefficients.
    """
    model = model_results["full"]
    params = model.params

    components = {}
    components["intercept"] = float(params.get("const", 0))

    if "WTI_AUD_CPL_lag1" in params:
        components["crude_oil_aud"] = float(
            params["WTI_AUD_CPL_lag1"] * conditions["wti_aud_cpl"]
        )
    if "diesel_crack_aud_cpl_lag1" in params:
        components["refining_margin"] = float(
            params["diesel_crack_aud_cpl_lag1"] * conditions["diesel_crack_aud_cpl"]
        )
    if "excise_lag1" in params:
        components["excise"] = float(
            params["excise_lag1"] * conditions["excise"]
        )

    components["predicted_total"] = sum(components.values())
    components["actual_total"] = conditions["diesel_tgp_actual"]
    components["residual"] = (
        conditions["diesel_tgp_actual"] - components["predicted_total"]
    )

    return components


# ---------------------------------------------------------------------------
# Output and reporting
# ---------------------------------------------------------------------------
def print_report(
    model_results: dict,
    conditions: dict,
    decomposition: dict,
    asymmetry: dict,
    forecasts: list[dict],
) -> str:
    """Generate comprehensive text report. Returns the report string."""
    lines = []

    def p(text: str = "") -> None:
        lines.append(text)

    p("=" * 72)
    p("  TGP MULTI-FACTOR FORECAST MODEL")
    p(f"  Run date: {date.today().isoformat()}")
    p(f"  Latest market data: {conditions['date']}")
    p("=" * 72)

    # Model comparison
    p("\n--- MODEL COMPARISON ---\n")
    for name, label in [
        ("baseline", "Baseline (WTI USD only)"),
        ("fx_adjusted", "FX-adjusted (WTI AUD cpl)"),
        ("full", "Full (crude AUD + crack + excise)"),
    ]:
        m = model_results[name]
        p(f"  {label}")
        p(f"    R-squared: {m.rsquared:.4f}")
        p(f"    Adj R-sq:  {m.rsquared_adj:.4f}")
        p(f"    AIC:       {m.aic:.0f}")
        p("")

    p(f"  Training period: {model_results['date_range'][0].date()} to "
      f"{model_results['date_range'][1].date()} ({model_results['n_weeks']} weeks)")

    # Full model coefficients
    p("\n--- FULL MODEL COEFFICIENTS ---\n")
    full = model_results["full"]
    for param in full.params.index:
        coef = full.params[param]
        pval = full.pvalues[param]
        sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else ""
        p(f"  {param:<30} {coef:>10.4f}  (p={pval:.4f}) {sig}")

    # Current conditions
    p("\n--- CURRENT CONDITIONS ---\n")
    p(f"  WTI Crude:       ${conditions['wti_usd']:.2f} USD/bbl")
    p(f"  AUD/USD:         {conditions['audusd']:.4f}")
    p(f"  WTI in AUD:      {conditions['wti_aud_cpl']:.1f} cpl")
    p(f"  Diesel crack:    {conditions['diesel_crack_aud_cpl']:.1f} cpl (AUD)")
    p(f"  Excise:          {conditions['excise']:.1f} cpl")
    p(f"  Actual TGP:      {conditions['diesel_tgp_actual']:.1f} cpl")

    # Decomposition
    p("\n--- TGP DECOMPOSITION (what is driving each cent) ---\n")
    p(f"  Crude oil (AUD):     {decomposition.get('crude_oil_aud', 0):>8.1f} cpl")
    p(f"  Refining margin:     {decomposition.get('refining_margin', 0):>8.1f} cpl")
    p(f"  Excise (model):      {decomposition.get('excise', 0):>8.1f} cpl")
    p(f"  Intercept (base):    {decomposition.get('intercept', 0):>8.1f} cpl")
    p(f"  ---")
    p(f"  Model prediction:    {decomposition.get('predicted_total', 0):>8.1f} cpl")
    p(f"  Actual TGP:          {decomposition.get('actual_total', 0):>8.1f} cpl")
    p(f"  Residual:            {decomposition.get('residual', 0):>8.1f} cpl")

    # Asymmetric analysis
    if "error" not in asymmetry:
        p("\n--- ASYMMETRIC PASS-THROUGH (rockets and feathers) ---\n")
        p(f"  R-squared of asymmetric model: {asymmetry['r_squared']:.4f}")
        p(f"  Total up pass-through:   {asymmetry['total_up_passthrough']:.2f} cpl per 1 cpl crude rise")
        p(f"  Total down pass-through: {asymmetry['total_down_passthrough']:.2f} cpl per 1 cpl crude fall")
        p(f"  Weeks to 90% pass-through (RISE):  {asymmetry['weeks_90pct_up']}")
        p(f"  Weeks to 90% pass-through (FALL):  {asymmetry['weeks_90pct_down']}")
        p("")
        p("  Cumulative response to 1 cpl crude INCREASE:")
        for i, v in enumerate(asymmetry["cum_up"]):
            bar = "#" * max(0, int(v * 5))
            p(f"    Week {i}: {v:>6.2f} cpl  {bar}")
        p("")
        p("  Cumulative response to 1 cpl crude DECREASE:")
        for i, v in enumerate(asymmetry["cum_down"]):
            bar = "#" * max(0, int(abs(v) * 5))
            p(f"    Week {i}: {v:>6.2f} cpl  {bar}")

    # Scenario table (filtered to key combos)
    p("\n--- SCENARIO FORECASTS (Diesel TGP, cpl) ---\n")

    # Current crack spread bucket
    current_crack = conditions["diesel_crack_aud_cpl"]
    crack_bucket = 25  # default to elevated
    if current_crack < 12:
        crack_bucket = 15
    elif current_crack > 28:
        crack_bucket = 35

    p(f"  Using crack spread = ${crack_bucket}/bbl (current conditions: ~${current_crack:.0f} AUD/bbl equiv)")
    p("")
    header = f"  {'WTI USD':>8}"
    for fx in [0.56, 0.60, 0.64, 0.68, 0.72]:
        header += f"  AUD {fx:.2f}"
    p(header)
    p("  " + "-" * 58)

    for wti in [65, 75, 85, 95, 101, 110, 120, 130]:
        row = f"  ${wti:>6}"
        for fx in [0.56, 0.60, 0.64, 0.68, 0.72]:
            match = [
                f for f in forecasts
                if f["wti_usd"] == wti
                and f["audusd"] == fx
                and f["crack_spread_usd"] == crack_bucket
            ]
            if match:
                row += f"  {match[0]['predicted_diesel_tgp']:>7.0f}"
            else:
                row += "      -"
        # Mark current conditions row
        if wti == 101:
            row += "  <-- current crude"
        p(row)

    # Directional forecast
    p("\n--- DIRECTIONAL OUTLOOK ---\n")
    pred = decomposition.get("predicted_total", 0)
    actual = conditions["diesel_tgp_actual"]
    gap = actual - pred

    if gap > 15:
        p("  TGP is ABOVE model equilibrium by {:.0f} cpl.".format(gap))
        p("  This suggests TGP has overshot OR the model is missing a factor")
        p("  (e.g. acute supply disruption premium, further AUD weakness).")
        p("  If crude and FX stabilise, expect TGP to DRIFT DOWN over 4-8 weeks.")
    elif gap < -15:
        p("  TGP is BELOW model equilibrium by {:.0f} cpl.".format(abs(gap)))
        p("  TGP has NOT YET caught up to current inputs.")
        p("  Expect TGP to RISE over the next 1-2 weeks even if crude is flat.")
    else:
        p("  TGP is near model equilibrium (within 15 cpl).")
        p("  Future direction depends on crude, AUD/USD, and refining margins.")

    report = "\n".join(lines)
    return report


def save_prediction_log(conditions: dict, decomposition: dict, forecasts: list[dict]) -> None:
    """Append today's prediction to the running log CSV."""
    FORECAST_DIR.mkdir(exist_ok=True)
    log_path = FORECAST_DIR / "prediction_log.csv"

    # Key scenario: current crude, current FX, elevated crack
    current_fx_bucket = round(conditions["audusd"] / 0.04) * 0.04
    current_crude = 101  # closest to current

    row = {
        "run_date": date.today().isoformat(),
        "data_date": conditions["date"],
        "wti_usd": conditions["wti_usd"],
        "audusd": conditions["audusd"],
        "wti_aud_cpl": conditions["wti_aud_cpl"],
        "diesel_crack_aud_cpl": conditions["diesel_crack_aud_cpl"],
        "excise": conditions["excise"],
        "actual_tgp": conditions["diesel_tgp_actual"],
        "predicted_tgp": decomposition["predicted_total"],
        "residual": decomposition["residual"],
    }

    write_header = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    log.info("Appended prediction to %s", log_path)


def save_scenario_csv(forecasts: list[dict]) -> None:
    """Save full scenario matrix to CSV."""
    FORECAST_DIR.mkdir(exist_ok=True)
    path = FORECAST_DIR / "scenarios.csv"
    df = pd.DataFrame(forecasts)
    df.to_csv(path, index=False)
    log.info("Saved scenarios to %s", path)


def compute_tgp_trajectory(
    conditions: dict,
    decomposition: dict,
    asymmetry: dict,
    weeks: int = 8,
) -> list[dict]:
    """
    Project where TGP is likely headed over the next N weeks,
    assuming crude and FX stay at current levels.

    Uses the asymmetric pass-through curves to model how fast
    TGP converges toward model equilibrium.
    """
    actual = conditions["diesel_tgp_actual"]
    predicted = decomposition["predicted_total"]
    gap = actual - predicted

    cum_up = asymmetry.get("cum_up", [])
    cum_down = asymmetry.get("cum_down", [])

    trajectory = []
    for w in range(weeks + 1):
        if abs(gap) < 1:
            # Already at equilibrium
            trajectory.append({"week": w, "projected_tgp": round(predicted, 1)})
        elif gap > 0:
            # TGP above equilibrium — expect decline (use fall curve)
            if cum_down and w < len(cum_down):
                # Normalise cum_down to get fraction of gap closed
                total = abs(cum_down[-1]) if abs(cum_down[-1]) > 0 else 1
                frac = abs(cum_down[w]) / total
            else:
                frac = min(w / 5.0, 1.0)
            projected = actual - gap * frac
            trajectory.append({"week": w, "projected_tgp": round(projected, 1)})
        else:
            # TGP below equilibrium — expect rise (use rise curve)
            if cum_up and w < len(cum_up):
                total = cum_up[-1] if cum_up[-1] > 0 else 1
                frac = cum_up[w] / total
            else:
                frac = min(w / 1.5, 1.0)
            projected = actual + abs(gap) * frac
            trajectory.append({"week": w, "projected_tgp": round(projected, 1)})

    return trajectory


def save_latest_json(
    conditions: dict,
    decomposition: dict,
    asymmetry: dict,
    model_results: dict,
    combined: pd.DataFrame | None = None,
) -> None:
    """Save latest forecast as JSON for consumption by other tools."""
    FORECAST_DIR.mkdir(exist_ok=True)
    path = FORECAST_DIR / "latest.json"

    full_model = model_results["full"]

    # Build WTI history for charting (last 120 days)
    wti_history = []
    if combined is not None:
        cutoff = combined.index.max() - pd.Timedelta(days=120)
        recent = combined[combined.index >= cutoff].dropna(subset=["WTI_USD"])
        for dt, row in recent.iterrows():
            wti_history.append({
                "date": str(dt.date()),
                "wti_usd": round(float(row["WTI_USD"]), 2),
                "wti_aud_cpl": round(float(row.get("WTI_AUD_CPL", 0)), 1),
            })

    # Compute TGP trajectory projection
    trajectory = compute_tgp_trajectory(
        conditions, decomposition, asymmetry
    )

    output = {
        "run_date": date.today().isoformat(),
        "conditions": conditions,
        "decomposition": {
            k: round(v, 2) if isinstance(v, float) else v
            for k, v in decomposition.items()
        },
        "model": {
            "r_squared": round(full_model.rsquared, 4),
            "r_squared_adj": round(full_model.rsquared_adj, 4),
            "coefficients": {
                k: round(v, 6) for k, v in full_model.params.to_dict().items()
            },
        },
        "asymmetry": {
            "weeks_90pct_rise": asymmetry.get("weeks_90pct_up"),
            "weeks_90pct_fall": asymmetry.get("weeks_90pct_down"),
            "total_up_passthrough": round(asymmetry.get("total_up_passthrough", 0), 4),
            "total_down_passthrough": round(asymmetry.get("total_down_passthrough", 0), 4),
            "cum_up": [round(v, 4) for v in asymmetry.get("cum_up", [])],
            "cum_down": [round(v, 4) for v in asymmetry.get("cum_down", [])],
        },
        "wti_history": wti_history,
        "trajectory": trajectory,
    }

    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    log.info("Saved latest.json to %s", path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    json_mode = "--json" in sys.argv

    # Step 1: Load market data from yfinance
    start_date = (date.today() - timedelta(days=365 * LOOKBACK_YEARS)).isoformat()
    market_data = fetch_yfinance_data(start_date)
    log.info("Market data: %d rows, %s to %s",
             len(market_data), market_data.index.min().date(), market_data.index.max().date())

    # Step 2: Load TGP history
    tgp_data = load_aip_tgp_history()
    log.info("TGP data: %d rows, %s to %s",
             len(tgp_data), tgp_data.index.min().date(), tgp_data.index.max().date())

    # Step 3: Engineer features
    combined = engineer_features(market_data, tgp_data)
    log.info("Combined dataset: %d rows", len(combined))

    # Step 4: Train models
    model_results = train_multifactor_model(combined)
    log.info("Models trained successfully")

    # Step 5: Current conditions
    conditions = current_conditions(combined)

    # Step 6: Decompose current TGP
    decomposition = decompose_current_tgp(model_results, conditions)

    # Step 7: Asymmetric lag analysis
    asymmetry = asymmetric_lag_analysis(combined)

    # Step 8: Scenario forecasts
    forecasts = scenario_forecast(model_results, conditions)

    # Step 9: Output
    report = print_report(model_results, conditions, decomposition, asymmetry, forecasts)

    if json_mode:
        save_latest_json(conditions, decomposition, asymmetry, model_results)
        # Print JSON summary to stdout
        print(json.dumps({
            "conditions": conditions,
            "predicted_tgp": decomposition["predicted_total"],
            "actual_tgp": conditions["diesel_tgp_actual"],
            "r_squared": model_results["full"].rsquared,
        }, indent=2))
    else:
        print(report)

    # Always save outputs
    save_prediction_log(conditions, decomposition, forecasts)
    save_scenario_csv(forecasts)
    save_latest_json(conditions, decomposition, asymmetry, model_results, combined)

    # Save report text
    FORECAST_DIR.mkdir(exist_ok=True)
    report_path = FORECAST_DIR / "latest_report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    log.info("Report saved to %s", report_path)


if __name__ == "__main__":
    main()
