"""
Appends today's diesel TGP from the AIP API to data/diesel_tgp_history.csv.
Run daily BEFORE tgp_forecast.py to keep training data current.
"""

import csv
import json
import logging
from datetime import date
from pathlib import Path
from urllib.request import urlopen, Request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("update_tgp")

DATA_DIR: Path = Path(__file__).parent / "data"
CSV_PATH: Path = DATA_DIR / "diesel_tgp_history.csv"

AIP_API_URL: str = (
    "https://www.aip.com.au/aip-api-request"
    "?api-path=public/api&call=tgpTables&location="
)


def fetch_latest_diesel_tgp() -> tuple[str, float]:
    """Fetch latest diesel TGP national average from AIP API."""
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
    return date_str, round(avg_cpl, 2)


def get_existing_dates() -> set[str]:
    """Read existing dates from CSV to avoid duplicates."""
    if not CSV_PATH.exists():
        return set()
    dates = set()
    with open(CSV_PATH, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dates.add(row["date"][:10])
    return dates


def append_row(date_str: str, diesel_tgp: float) -> None:
    """Append a single row to the history CSV."""
    DATA_DIR.mkdir(exist_ok=True)
    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["date", "diesel_tgp"])
        writer.writerow([date_str, diesel_tgp])


def main() -> None:
    date_str, avg_tgp = fetch_latest_diesel_tgp()
    log.info("AIP API returned: %s = %.2f cpl", date_str, avg_tgp)

    existing = get_existing_dates()
    if date_str in existing:
        log.info("Date %s already in history, skipping", date_str)
        return

    append_row(date_str, avg_tgp)
    log.info("Appended %s: %.2f cpl to %s", date_str, avg_tgp, CSV_PATH)


if __name__ == "__main__":
    main()
