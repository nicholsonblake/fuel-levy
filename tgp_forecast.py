"""
TGP Multi-Factor Forecast Model
Predicts Australian diesel Terminal Gate Prices using a cointegrated
Error Correction Model (ECM) on ex-excise TGP:
  - WTI crude oil in AUD cents-per-litre
  - Diesel crack spread (heating oil - WTI, in AUD cpl)
  - Fuel excise subtracted as known deterministic component
  - Asymmetric pass-through (rockets and feathers)

Designed to run daily via GitHub Actions during AEST business hours.
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
        for attempt in range(1, 4):
            try:
                df = yf.download(ticker, start=start_date, progress=False, auto_adjust=True)
                if df.empty:
                    log.warning("No data for %s (attempt %d)", ticker, attempt)
                    if attempt < 3:
                        import time; time.sleep(2 ** attempt)
                        continue
                    break
                # yfinance returns MultiIndex columns when single ticker; flatten
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                series = df["Close"].copy()
                series.name = name
                # Flatten timezone-aware index to date-only
                series.index = series.index.tz_localize(None) if series.index.tz else series.index
                frames[name] = series
                break
            except Exception as exc:
                log.error("Failed to fetch %s (attempt %d): %s", ticker, attempt, exc)
                if attempt < 3:
                    import time; time.sleep(2 ** attempt)

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

    # Ex-excise TGP: subtract the known deterministic excise component.
    # This isolates the market-driven portion of TGP for regression,
    # avoiding excise acting as a spurious time-trend proxy.
    combined["tgp_ex_excise"] = combined["diesel_tgp"] - combined["excise"]

    # Lagged crude (for asymmetric analysis)
    combined["WTI_AUD_CPL_lag7"] = combined["WTI_AUD_CPL"].shift(7)
    combined["WTI_AUD_CPL_lag14"] = combined["WTI_AUD_CPL"].shift(14)
    combined["WTI_AUD_CPL_chg"] = combined["WTI_AUD_CPL"].diff()
    combined["WTI_AUD_CPL_chg_pos"] = combined["WTI_AUD_CPL_chg"].clip(lower=0)
    combined["WTI_AUD_CPL_chg_neg"] = combined["WTI_AUD_CPL_chg"].clip(upper=0)

    return combined


# ---------------------------------------------------------------------------
# Cointegration testing
# ---------------------------------------------------------------------------
def engle_granger_test(y: pd.Series, X: pd.DataFrame) -> dict:
    """
    Engle-Granger two-step cointegration test.
    Step 1: OLS on levels to get residuals.
    Step 2: ADF test on residuals — if stationary, series are cointegrated.
    Returns dict with test statistic, p-value, and the long-run OLS model.
    """
    from statsmodels.tsa.stattools import adfuller

    # Step 1: long-run (levels) regression
    X_const = sm.add_constant(X)
    lr_model = sm.OLS(y, X_const).fit()
    residuals = lr_model.resid

    # Step 2: ADF on residuals (no constant — residuals are mean-zero by construction)
    adf_result = adfuller(residuals, maxlag=None, regression="c", autolag="AIC")
    adf_stat, adf_pvalue = adf_result[0], adf_result[1]

    return {
        "long_run_model": lr_model,
        "residuals": residuals,
        "adf_stat": float(adf_stat),
        "adf_pvalue": float(adf_pvalue),
        "cointegrated": adf_pvalue < 0.05,
    }


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------
def train_multifactor_model(
    data: pd.DataFrame,
) -> dict:
    """
    Train models on ex-excise TGP (excise subtracted as known deterministic):
      1. Baseline: TGP ~ WTI_USD (levels, for comparison only)
      2. FX-adjusted: TGP ~ WTI_AUD_CPL (levels, for comparison only)
      3. Long-run: TGP_ex_excise ~ WTI_AUD_CPL + diesel_crack_aud_cpl
      4. ECM: ΔTGP_ex_excise ~ EC_{t-1} + ΔWTI_AUD_CPL + Δcrack
         where EC = residual from the long-run equation.

    The ECM separates equilibrium (where TGP should be) from adjustment
    speed (how fast it gets there), replacing the ad hoc trajectory logic.
    """
    results = {}

    # Weekly aggregation for model training (reduces autocorrelation noise)
    weekly = data.resample("W").last().dropna(
        subset=["WTI_USD", "WTI_AUD_CPL", "diesel_tgp", "tgp_ex_excise"]
    )

    if len(weekly) < 30:
        raise ValueError(f"Insufficient data for model training ({len(weekly)} weeks)")

    # ---- Legacy comparison models (levels, for R² reporting) ----
    # Use 1-week lag for legacy models
    for col in ["WTI_USD", "WTI_AUD_CPL", "diesel_crack_aud_cpl", "excise", "AUDUSD"]:
        if col in weekly.columns:
            weekly[f"{col}_lag1"] = weekly[col].shift(1)

    weekly_clean = weekly.dropna()

    y_full = weekly_clean["diesel_tgp"]
    X1 = sm.add_constant(weekly_clean["WTI_USD_lag1"])
    results["baseline"] = sm.OLS(y_full, X1).fit()

    X2 = sm.add_constant(weekly_clean["WTI_AUD_CPL_lag1"])
    results["fx_adjusted"] = sm.OLS(y_full, X2).fit()

    # Legacy "full" model (inc excise) kept for backwards compatibility
    factor_cols = ["WTI_AUD_CPL_lag1", "diesel_crack_aud_cpl_lag1", "excise_lag1"]
    available = [c for c in factor_cols if c in weekly_clean.columns]
    X3 = sm.add_constant(weekly_clean[available])
    results["full"] = sm.OLS(y_full, X3).fit()

    # ---- Step 1: Long-run equation on ex-excise TGP (levels) ----
    y_ex = weekly_clean["tgp_ex_excise"]
    lr_cols = ["WTI_AUD_CPL"]
    if "diesel_crack_aud_cpl" in weekly_clean.columns:
        lr_cols.append("diesel_crack_aud_cpl")
    X_lr = weekly_clean[lr_cols]

    coint = engle_granger_test(y_ex, X_lr)
    results["cointegration"] = coint
    results["long_run"] = coint["long_run_model"]
    log.info(
        "Cointegration test: ADF stat=%.3f, p=%.4f, cointegrated=%s",
        coint["adf_stat"], coint["adf_pvalue"], coint["cointegrated"],
    )

    # ---- Step 2: Error Correction Model ----
    # EC term = lagged residual from long-run equation
    weekly_clean = weekly_clean.copy()
    weekly_clean["ec_term"] = coint["residuals"].shift(1)

    # First differences
    weekly_clean["d_tgp_ex"] = weekly_clean["tgp_ex_excise"].diff()
    weekly_clean["d_wti_aud_cpl"] = weekly_clean["WTI_AUD_CPL"].diff()
    if "diesel_crack_aud_cpl" in weekly_clean.columns:
        weekly_clean["d_crack"] = weekly_clean["diesel_crack_aud_cpl"].diff()

    ecm_data = weekly_clean.dropna(subset=["ec_term", "d_tgp_ex", "d_wti_aud_cpl"])

    ecm_y = ecm_data["d_tgp_ex"]
    ecm_x_cols = ["ec_term", "d_wti_aud_cpl"]
    if "d_crack" in ecm_data.columns:
        ecm_x_cols.append("d_crack")
    ecm_X = sm.add_constant(ecm_data[ecm_x_cols])

    ecm_model = sm.OLS(ecm_y, ecm_X).fit()
    results["ecm"] = ecm_model

    # The EC coefficient should be negative (error-correcting)
    ec_coeff = float(ecm_model.params.get("ec_term", 0))
    results["ec_speed"] = ec_coeff
    log.info(
        "ECM: EC coefficient=%.4f (negative = correcting), R²=%.4f",
        ec_coeff, ecm_model.rsquared,
    )

    # Store training metadata
    results["weekly_data"] = weekly_clean
    results["n_weeks"] = len(weekly_clean)
    results["date_range"] = (weekly_clean.index.min(), weekly_clean.index.max())

    # ---- Out-of-sample validation (last 26 weeks) ----
    oos = out_of_sample_validation(weekly_clean, holdout_weeks=26)
    results["oos_validation"] = oos
    if oos:
        log.info(
            "OOS validation (26 weeks): MAE=%.2f, RMSE=%.2f, n=%d",
            oos["mae"], oos["rmse"], oos["n"],
        )

    return results


# ---------------------------------------------------------------------------
# Out-of-sample validation
# ---------------------------------------------------------------------------
OOS_HOLDOUT_WEEKS: int = 26


def out_of_sample_validation(weekly: pd.DataFrame, holdout_weeks: int = OOS_HOLDOUT_WEEKS) -> dict:
    """
    Train on all-but-last-N weeks, predict the holdout, report MAE/RMSE.
    Uses the long-run ex-excise model specification.
    """
    if len(weekly) < holdout_weeks + 30:
        return {}

    cutoff = len(weekly) - holdout_weeks
    train = weekly.iloc[:cutoff]
    test = weekly.iloc[cutoff:]

    y_train = train["tgp_ex_excise"]
    lr_cols = ["WTI_AUD_CPL"]
    if "diesel_crack_aud_cpl" in train.columns:
        lr_cols.append("diesel_crack_aud_cpl")
    X_train = sm.add_constant(train[lr_cols])

    model = sm.OLS(y_train, X_train).fit()

    X_test = sm.add_constant(test[lr_cols])
    predictions = model.predict(X_test)

    # Add excise back for full TGP comparison
    pred_tgp = predictions + test["excise"]
    actual_tgp = test["diesel_tgp"]

    errors = actual_tgp - pred_tgp
    mae = float(errors.abs().mean())
    rmse = float(np.sqrt((errors ** 2).mean()))
    mape = float((errors.abs() / actual_tgp).mean() * 100)

    return {
        "mae": mae,
        "rmse": rmse,
        "mape": mape,
        "n": len(test),
        "holdout_start": str(test.index.min().date()),
        "holdout_end": str(test.index.max().date()),
    }


# ---------------------------------------------------------------------------
# Model-based prediction intervals
# ---------------------------------------------------------------------------
def prediction_interval(model_results: dict, conditions: dict, alpha: float = 0.10) -> dict:
    """
    Compute model-based prediction interval for current conditions.
    Uses statsmodels get_prediction() for proper interval estimation.
    alpha=0.10 gives a 90% CI.
    """
    lr_model = model_results["long_run"]
    param_names = list(lr_model.params.index)

    x_dict = {
        "const": 1,
        "WTI_AUD_CPL": conditions["wti_aud_cpl"],
        "diesel_crack_aud_cpl": conditions["diesel_crack_aud_cpl"],
    }
    x_vals = [x_dict.get(p, 0) for p in param_names]
    x_df = pd.DataFrame([x_vals], columns=param_names)

    pred = lr_model.get_prediction(x_df)
    summary = pred.summary_frame(alpha=alpha)

    excise = conditions["excise"]
    point = float(summary["mean"].iloc[0]) + excise
    ci_lower = float(summary["obs_ci_lower"].iloc[0]) + excise
    ci_upper = float(summary["obs_ci_upper"].iloc[0]) + excise

    return {
        "point": round(point, 1),
        "ci_lower": round(ci_lower, 1),
        "ci_upper": round(ci_upper, 1),
        "alpha": alpha,
        "confidence": f"{(1 - alpha) * 100:.0f}%",
    }


# ---------------------------------------------------------------------------
# Asymmetric pass-through (rockets and feathers)
# ---------------------------------------------------------------------------
def asymmetric_lag_analysis(data: pd.DataFrame) -> dict:
    """
    Parsimonious asymmetric ECM for pass-through speed.

    Model (6 parameters):
        Δtgp = const + β_up·Δcrude_up + β_down·Δcrude_down
               + γ_up·EC⁺_{t-1} + γ_down·EC⁻_{t-1} + δ·Δtgp_{t-1}

    where EC = tgp - equilibrium (from long-run model).

    This replaces the previous 18-parameter distributed lag with a
    model that has clear economic interpretation:
      - β_up, β_down: immediate (contemporaneous) pass-through asymmetry
      - γ_up, γ_down: asymmetric error-correction speed
      - δ: TGP momentum / persistence

    The cumulative impulse response is then derived analytically from
    these coefficients using geometric decay, rather than estimated
    freely at each lag (which wastes degrees of freedom).
    """
    weekly = data.resample("W").last().dropna(subset=["WTI_AUD_CPL", "tgp_ex_excise"])
    weekly = weekly.copy()
    weekly["tgp_chg"] = weekly["tgp_ex_excise"].diff()
    weekly["crude_chg"] = weekly["WTI_AUD_CPL"].diff()
    weekly["crude_up"] = weekly["crude_chg"].clip(lower=0)
    weekly["crude_down"] = weekly["crude_chg"].clip(upper=0)

    # Lagged TGP change (persistence / momentum)
    weekly["tgp_chg_lag1"] = weekly["tgp_chg"].shift(1)

    # Simple equilibrium residual: tgp_ex_excise - f(crude, crack)
    # Use a quick OLS for the long-run relationship
    lr_subset = weekly.dropna(subset=["WTI_AUD_CPL", "tgp_ex_excise"])
    lr_cols = ["WTI_AUD_CPL"]
    if "diesel_crack_aud_cpl" in lr_subset.columns:
        lr_cols.append("diesel_crack_aud_cpl")
    X_lr = sm.add_constant(lr_subset[lr_cols])
    lr_fit = sm.OLS(lr_subset["tgp_ex_excise"], X_lr).fit()
    weekly["ec_residual"] = weekly["tgp_ex_excise"] - lr_fit.predict(
        sm.add_constant(weekly[lr_cols])
    )

    # Split EC into positive (TGP above equilibrium) and negative
    weekly["ec_pos"] = weekly["ec_residual"].shift(1).clip(lower=0)
    weekly["ec_neg"] = weekly["ec_residual"].shift(1).clip(upper=0)

    weekly = weekly.dropna(subset=[
        "tgp_chg", "crude_up", "crude_down", "ec_pos", "ec_neg", "tgp_chg_lag1"
    ])

    if len(weekly) < 40:
        return {"error": "Insufficient data for asymmetric analysis"}

    # Asymmetric ECM regression (6 parameters)
    y = weekly["tgp_chg"]
    X = sm.add_constant(weekly[[
        "crude_up", "crude_down", "ec_pos", "ec_neg", "tgp_chg_lag1",
    ]])
    model = sm.OLS(y, X).fit()

    beta_up = float(model.params.get("crude_up", 0))
    beta_down = float(model.params.get("crude_down", 0))
    gamma_up = float(model.params.get("ec_pos", 0))    # should be negative
    gamma_down = float(model.params.get("ec_neg", 0))   # should be negative
    delta = float(model.params.get("tgp_chg_lag1", 0))  # persistence

    # Derive cumulative impulse response analytically.
    # After a 1 cpl shock, week-0 pass-through = β, then each subsequent
    # week the remaining gap decays geometrically at rate (1 - |γ| + δ).
    max_weeks = 9
    cum_up = _cumulative_impulse(beta_up, gamma_up, delta, max_weeks)
    cum_down = _cumulative_impulse(beta_down, gamma_down, delta, max_weeks)

    # Weeks to 90% pass-through
    total_up = cum_up[-1] if cum_up[-1] != 0 else 1
    total_down = cum_down[-1] if cum_down[-1] != 0 else 1

    weeks_90_up = next(
        (i for i, v in enumerate(cum_up) if v >= 0.9 * total_up), max_weeks - 1
    )
    weeks_90_down = next(
        (i for i, v in enumerate(cum_down) if abs(v) >= 0.9 * abs(total_down)),
        max_weeks - 1,
    )

    return {
        "model": model,
        "beta_up": beta_up,
        "beta_down": beta_down,
        "gamma_up": gamma_up,
        "gamma_down": gamma_down,
        "persistence": delta,
        "cum_up": cum_up,
        "cum_down": cum_down,
        "total_up_passthrough": float(total_up),
        "total_down_passthrough": float(total_down),
        "weeks_90pct_up": weeks_90_up,
        "weeks_90pct_down": weeks_90_down,
        "r_squared": model.rsquared,
        "n_params": len(model.params),
        "n_obs": len(weekly),
    }


def _cumulative_impulse(
    beta: float, gamma: float, delta: float, weeks: int,
) -> list[float]:
    """
    Analytically derive cumulative impulse response from a 1 cpl shock.
    Week 0: pass-through = beta
    Week k>0: remaining gap shrinks by |gamma| and momentum decays by delta
    """
    cum = [beta]
    remaining = 1.0 - beta
    for _ in range(1, weeks):
        # Error correction closes |gamma| of remaining gap
        # Plus momentum carries forward delta of last week's change
        correction = -gamma * remaining + delta * (cum[-1] - (cum[-2] if len(cum) > 1 else 0))
        new_pass = max(0, min(correction, remaining)) if remaining > 0 else min(0, max(correction, remaining))
        cum.append(cum[-1] + new_pass)
        remaining -= new_pass
    return cum


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------
def current_conditions(data: pd.DataFrame) -> dict:
    """Extract the latest available values for all inputs."""
    latest = data.dropna(subset=["WTI_USD", "AUDUSD"]).iloc[-1]
    excise = float(latest["excise"])
    tgp_actual = float(latest.get("diesel_tgp", 0))
    return {
        "date": str(latest.name.date()),
        "wti_usd": float(latest["WTI_USD"]),
        "audusd": float(latest["AUDUSD"]),
        "wti_aud_cpl": float(latest["WTI_AUD_CPL"]),
        "diesel_crack_aud_cpl": float(latest.get("diesel_crack_aud_cpl", 0)),
        "excise": excise,
        "diesel_tgp_actual": tgp_actual,
        "tgp_ex_excise": tgp_actual - excise,
    }


def scenario_forecast(model_results: dict, conditions: dict) -> list[dict]:
    """
    Generate TGP forecasts under various crude / FX / margin scenarios.
    Uses the long-run model on ex-excise TGP, then adds current excise back.
    """
    lr_model = model_results["long_run"]
    param_names = list(lr_model.params.index)
    excise = conditions["excise"]

    # Centre WTI scenarios around current price in $10 steps
    cur_wti = conditions["wti_usd"]
    wti_centre = round(cur_wti / 5) * 5
    wti_scenarios = sorted(set(
        [wti_centre + d for d in (-30, -20, -10, 0, 10, 20, 30)]
    ))

    # Centre FX scenarios around current rate in 0.04 steps
    cur_fx = conditions["audusd"]
    fx_centre = round(cur_fx / 0.02) * 0.02  # snap to nearest 0.02
    fx_scenarios = sorted(set(
        round(fx_centre + d, 2) for d in (-0.08, -0.04, 0.00, 0.04, 0.08)
    ))

    crack_scenarios = [15, 25, 35]

    forecasts = []
    for wti_usd in wti_scenarios:
        for audusd in fx_scenarios:
            for crack_usd in crack_scenarios:
                wti_aud = wti_usd / audusd
                wti_aud_cpl = wti_aud / BARREL_TO_LITRES * 100
                crack_aud = crack_usd / audusd
                crack_aud_cpl = crack_aud / BARREL_TO_LITRES * 100

                # Build predictor vector for long-run ex-excise model
                x_dict = {
                    "const": 1,
                    "WTI_AUD_CPL": wti_aud_cpl,
                    "diesel_crack_aud_cpl": crack_aud_cpl,
                }
                x_vals = [x_dict.get(p, 0) for p in param_names]
                predicted_ex = float(lr_model.predict(
                    pd.DataFrame([x_vals], columns=param_names)
                )[0])
                # Add excise back as known component
                predicted_tgp = predicted_ex + excise

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
    Break down current diesel TGP into its component drivers.

    Uses the long-run model on ex-excise TGP, then adds excise back
    as a known deterministic component. This avoids excise absorbing
    trend residuals through a regression coefficient.
    """
    lr_model = model_results["long_run"]
    params = lr_model.params

    components = {}
    components["intercept"] = float(params.get("const", 0))

    if "WTI_AUD_CPL" in params:
        components["crude_oil_aud"] = float(
            params["WTI_AUD_CPL"] * conditions["wti_aud_cpl"]
        )
    if "diesel_crack_aud_cpl" in params:
        components["refining_margin"] = float(
            params["diesel_crack_aud_cpl"] * conditions["diesel_crack_aud_cpl"]
        )

    # Excise is a known component, not estimated
    components["excise"] = conditions["excise"]

    # Model RMSE for threshold calculations
    rmse = float(np.sqrt(lr_model.mse_resid))
    components["model_rmse"] = rmse

    # Predicted = long-run ex-excise prediction + excise
    predicted_ex_excise = components["intercept"]
    if "crude_oil_aud" in components:
        predicted_ex_excise += components["crude_oil_aud"]
    if "refining_margin" in components:
        predicted_ex_excise += components["refining_margin"]

    components["predicted_ex_excise"] = predicted_ex_excise
    components["predicted_total"] = predicted_ex_excise + conditions["excise"]
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
        ("full", "Legacy full (crude AUD + crack + excise)"),
    ]:
        m = model_results[name]
        p(f"  {label}")
        p(f"    R-squared: {m.rsquared:.4f}")
        p(f"    Adj R-sq:  {m.rsquared_adj:.4f}")
        p(f"    AIC:       {m.aic:.0f}")
        p("")

    # Cointegration test
    coint = model_results.get("cointegration", {})
    p("  --- Cointegration (Engle-Granger) ---")
    p(f"    ADF statistic: {coint.get('adf_stat', 0):.3f}")
    p(f"    p-value:       {coint.get('adf_pvalue', 1):.4f}")
    p(f"    Cointegrated:  {'YES' if coint.get('cointegrated') else 'NO'}")
    p("")

    # Long-run model (ex-excise)
    lr = model_results["long_run"]
    p("  Long-run model (ex-excise TGP ~ crude AUD + crack)")
    p(f"    R-squared: {lr.rsquared:.4f}")
    p(f"    Adj R-sq:  {lr.rsquared_adj:.4f}")
    p(f"    RMSE:      {np.sqrt(lr.mse_resid):.2f} cpl")
    p("")

    # ECM
    ecm = model_results.get("ecm")
    if ecm is not None:
        p("  Error Correction Model (weekly)")
        p(f"    EC coefficient: {model_results.get('ec_speed', 0):.4f} (should be negative)")
        p(f"    R-squared:      {ecm.rsquared:.4f}")
        p("")

    p(f"  Training period: {model_results['date_range'][0].date()} to "
      f"{model_results['date_range'][1].date()} ({model_results['n_weeks']} weeks)")

    # Long-run model coefficients
    p("\n--- LONG-RUN MODEL COEFFICIENTS (ex-excise TGP) ---\n")
    for param in lr.params.index:
        coef = lr.params[param]
        pval = lr.pvalues[param]
        sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else ""
        p(f"  {param:<30} {coef:>10.4f}  (p={pval:.4f}) {sig}")

    if ecm is not None:
        p("\n--- ECM COEFFICIENTS ---\n")
        for param in ecm.params.index:
            coef = ecm.params[param]
            pval = ecm.pvalues[param]
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
    p(f"  Excise (known):      {decomposition.get('excise', 0):>8.1f} cpl")
    p(f"  Intercept (base):    {decomposition.get('intercept', 0):>8.1f} cpl")
    p(f"  ---")
    p(f"  Model prediction:    {decomposition.get('predicted_total', 0):>8.1f} cpl")
    p(f"  Actual TGP:          {decomposition.get('actual_total', 0):>8.1f} cpl")
    p(f"  Residual:            {decomposition.get('residual', 0):>8.1f} cpl")
    p(f"  Model RMSE:          {decomposition.get('model_rmse', 0):>8.1f} cpl")

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

    cur_wti = conditions["wti_usd"]
    snapped_wti = round(cur_wti / 5) * 5
    wti_rows = sorted(set([65, 75, 85, 95, 110, 120, 130, snapped_wti]))
    for wti in wti_rows:
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
        if wti == snapped_wti:
            row += "  <-- ~current crude"
        p(row)

    # Directional forecast — threshold from model RMSE (1 standard deviation)
    p("\n--- DIRECTIONAL OUTLOOK ---\n")
    pred = decomposition.get("predicted_total", 0)
    actual = conditions["diesel_tgp_actual"]
    gap = actual - pred
    rmse = decomposition.get("model_rmse", 15)
    threshold = rmse  # 1σ — outside this is "significant"

    p(f"  Signal threshold: {threshold:.0f} cpl (1x model RMSE)")
    p("")

    if gap > threshold:
        p("  TGP is ABOVE model equilibrium by {:.0f} cpl (>{:.0f} cpl threshold).".format(gap, threshold))
        p("  This suggests TGP has overshot OR the model is missing a factor")
        p("  (e.g. acute supply disruption premium, further AUD weakness).")
        p("  If crude and FX stabilise, expect TGP to DRIFT DOWN.")
    elif gap < -threshold:
        p("  TGP is BELOW model equilibrium by {:.0f} cpl (>{:.0f} cpl threshold).".format(abs(gap), threshold))
        p("  TGP has NOT YET caught up to current inputs.")
        p("  Expect TGP to RISE even if crude is flat.")
    else:
        p("  TGP is near model equilibrium (within {:.0f} cpl RMSE band).".format(threshold))
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


def _save_trajectory_log(conditions: dict, trajectory: list[dict]) -> None:
    """Append trajectory projections to a CSV log for future accuracy tracking."""
    FORECAST_DIR.mkdir(exist_ok=True)
    log_path = FORECAST_DIR / "trajectory_log.csv"
    run_date = conditions.get("date", date.today().isoformat())

    write_header = not log_path.exists()
    with open(log_path, "a", encoding="utf-8") as f:
        if write_header:
            f.write("run_date,target_date,week,projected_tgp\n")
        for t in trajectory:
            week = t["week"]
            target = (pd.Timestamp(run_date) + pd.Timedelta(weeks=week)).date()
            f.write(f"{run_date},{target},{week},{t['projected_tgp']}\n")
    log.info("Appended %d trajectory rows to %s", len(trajectory), log_path)


def compute_tgp_trajectory(
    conditions: dict,
    decomposition: dict,
    asymmetry: dict,
    weeks: int = 8,
    ec_speed: float | None = None,
) -> list[dict]:
    """
    Project where TGP is likely headed over the next N weeks,
    assuming crude and FX stay at current levels.

    Each trajectory point includes 90% confidence bounds that widen
    with sqrt(weeks), reflecting growing uncertainty at longer horizons.
    The base uncertainty comes from the long-run model RMSE.
    """
    actual = conditions["diesel_tgp_actual"]
    predicted = decomposition["predicted_total"]
    gap = actual - predicted
    rmse = decomposition.get("model_rmse", 15)

    def _ci_bounds(point: float, week: int) -> tuple[float, float]:
        """90% CI half-width scales with sqrt(week) from weekly RMSE."""
        half = 1.645 * rmse * (max(week, 1) ** 0.5) / (4 ** 0.5)
        return round(point - half, 1), round(point + half, 1)

    lo, hi = _ci_bounds(actual, 0)
    trajectory = [{"week": 0, "projected_tgp": round(actual, 1), "ci_lower": lo, "ci_upper": hi}]

    if abs(gap) < 1:
        for w in range(1, weeks + 1):
            lo, hi = _ci_bounds(predicted, w)
            trajectory.append({"week": w, "projected_tgp": round(predicted, 1), "ci_lower": lo, "ci_upper": hi})
        return trajectory

    # ECM-based: each week, gap closes by |ec_speed| fraction
    if ec_speed is not None and ec_speed < 0:
        adj_rate = min(abs(ec_speed), 0.95)
        remaining_gap = gap
        current = actual
        for w in range(1, weeks + 1):
            correction = remaining_gap * adj_rate
            current -= correction
            remaining_gap -= correction
            lo, hi = _ci_bounds(current, w)
            trajectory.append({"week": w, "projected_tgp": round(current, 1), "ci_lower": lo, "ci_upper": hi})
        return trajectory

    # Fallback: use asymmetric pass-through curves (legacy behaviour)
    cum_up = asymmetry.get("cum_up", [])
    cum_down = asymmetry.get("cum_down", [])

    for w in range(1, weeks + 1):
        if gap > 0:
            if cum_down and w < len(cum_down):
                total = abs(cum_down[-1]) if abs(cum_down[-1]) > 0 else 1
                frac = abs(cum_down[w]) / total
            else:
                frac = min(w / 5.0, 1.0)
            projected = actual - gap * frac
        else:
            if cum_up and w < len(cum_up):
                total = cum_up[-1] if cum_up[-1] > 0 else 1
                frac = cum_up[w] / total
            else:
                frac = min(w / 1.5, 1.0)
            projected = actual + abs(gap) * frac
        lo, hi = _ci_bounds(projected, w)
        trajectory.append({"week": w, "projected_tgp": round(projected, 1), "ci_lower": lo, "ci_upper": hi})

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

    lr_model = model_results["long_run"]
    ecm_model = model_results.get("ecm")
    coint = model_results.get("cointegration", {})

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

    # Compute TGP trajectory projection using ECM adjustment speed
    trajectory = compute_tgp_trajectory(
        conditions, decomposition, asymmetry,
        ec_speed=model_results.get("ec_speed"),
    )

    output = {
        "run_date": date.today().isoformat(),
        "conditions": conditions,
        "decomposition": {
            k: round(v, 2) if isinstance(v, float) else v
            for k, v in decomposition.items()
        },
        "model": {
            "r_squared": round(lr_model.rsquared, 4),
            "r_squared_adj": round(lr_model.rsquared_adj, 4),
            "rmse": round(float(np.sqrt(lr_model.mse_resid)), 2),
            "coefficients": {
                k: round(v, 6) for k, v in lr_model.params.to_dict().items()
            },
        },
        "cointegration": {
            "adf_stat": round(coint.get("adf_stat", 0), 4),
            "adf_pvalue": round(coint.get("adf_pvalue", 1), 4),
            "cointegrated": coint.get("cointegrated", False),
        },
        "ecm": {
            "ec_speed": round(model_results.get("ec_speed", 0), 4),
            "r_squared": round(ecm_model.rsquared, 4) if ecm_model else None,
        },
        "asymmetry": {
            "weeks_90pct_rise": asymmetry.get("weeks_90pct_up"),
            "weeks_90pct_fall": asymmetry.get("weeks_90pct_down"),
            "total_up_passthrough": round(asymmetry.get("total_up_passthrough", 0), 4),
            "total_down_passthrough": round(asymmetry.get("total_down_passthrough", 0), 4),
            "cum_up": [round(v, 4) for v in asymmetry.get("cum_up", [])],
            "cum_down": [round(v, 4) for v in asymmetry.get("cum_down", [])],
        },
        "oos_validation": model_results.get("oos_validation", {}),
        "wti_history": wti_history,
        "trajectory": trajectory,
    }

    # Add prediction interval if conditions are available
    try:
        pi = prediction_interval(model_results, conditions)
        output["prediction_interval"] = pi
    except Exception:
        pass

    # Atomic write: write to temp file then rename, so a concurrent
    # cancellation can never leave a truncated latest.json on disk.
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(output, f, indent=2)
    tmp_path.replace(path)
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

    # Step 8b: Model-based prediction interval
    pred_interval = prediction_interval(model_results, conditions)
    log.info("90%% CI: %.0f - %.0f cpl", pred_interval["ci_lower"], pred_interval["ci_upper"])

    # Step 9: Output
    report = print_report(model_results, conditions, decomposition, asymmetry, forecasts)

    if json_mode:
        save_latest_json(conditions, decomposition, asymmetry, model_results)
        # Print JSON summary to stdout
        print(json.dumps({
            "conditions": conditions,
            "predicted_tgp": decomposition["predicted_total"],
            "actual_tgp": conditions["diesel_tgp_actual"],
            "r_squared": model_results["long_run"].rsquared,
        }, indent=2))
    else:
        print(report)

    # Always save outputs
    save_prediction_log(conditions, decomposition, forecasts)
    save_scenario_csv(forecasts)
    save_latest_json(conditions, decomposition, asymmetry, model_results, combined)

    # Log trajectory projections for future accuracy tracking
    trajectory = compute_tgp_trajectory(
        conditions, decomposition, asymmetry,
        ec_speed=model_results.get("ec_speed"),
    )
    _save_trajectory_log(conditions, trajectory)

    # Save report text
    FORECAST_DIR.mkdir(exist_ok=True)
    report_path = FORECAST_DIR / "latest_report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    log.info("Report saved to %s", report_path)


if __name__ == "__main__":
    main()
