"""Official ISM Manufacturing PMI retrieval.

FRED's former NAPMPMI CSV endpoint is no longer available.  Keep the PMI
acquisition in one small adapter so Streamlit and the background worker publish
the same value to every frontend.
"""

from __future__ import annotations

import html
import re
import ssl
import urllib.request
from datetime import date

import certifi


_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_BASE_URL = (
    "https://www.ismworld.org/supply-management-news-and-reports/"
    "reports/ism-pmi-reports/pmi/{month}/"
)


def _plain_text(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_manufacturing_pmi(markup: str) -> list[dict]:
    """Return the current and prior PMI observations from an ISM report page."""
    text = _plain_text(markup)
    match = re.search(
        r"Manufacturing PMI(?:\s*®)?\s+registered\s+([0-9]+(?:\.[0-9]+)?)\s+percent\s+in\s+"
        r"([A-Za-z]+),\s+([0-9.]+)\s+percentage points?\s+"
        r"(?:above|below)\s+the\s+([A-Za-z]+)\s+figure\s+of\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+percent",
        text,
        re.IGNORECASE,
    )
    if not match:
        # The report heading is a stable secondary source for the latest value.
        heading = re.search(
            r"([A-Za-z]+)\s+(20\d{2})\s+ISM(?:\s*®)?\s+Manufacturing.*?"
            r"Manufacturing PMI(?:\s*®)?\s+at\s+([0-9]+(?:\.[0-9]+)?)%",
            text,
            re.IGNORECASE,
        )
        if not heading:
            return []
        month_name, year, value = heading.groups()
        month_number = _MONTHS.index(month_name.lower()) + 1
        return [{"date": f"{int(year):04d}-{month_number:02d}-01", "value": float(value)}]

    current_value, current_month, _, previous_month, previous_value = match.groups()
    current_number = _MONTHS.index(current_month.lower()) + 1
    previous_number = _MONTHS.index(previous_month.lower()) + 1

    # The page URL is reused annually, so take the year from its report title.
    year_match = re.search(
        rf"{re.escape(current_month)}\s+(20\d{{2}})\s+ISM(?:\s*®)?\s+Manufacturing",
        text,
        re.IGNORECASE,
    )
    report_year = int(year_match.group(1)) if year_match else date.today().year
    previous_year = report_year - 1 if previous_number > current_number else report_year
    return [
        {"date": f"{previous_year:04d}-{previous_number:02d}-01", "value": float(previous_value)},
        {"date": f"{report_year:04d}-{current_number:02d}-01", "value": float(current_value)},
    ]


def manufacturing_pmi_rows(*, today: date | None = None, timeout: int = 10) -> list[dict]:
    """Fetch the newest available official ISM Manufacturing PMI report."""
    today = today or date.today()
    # The newest report covers a prior month. Try the last three report months
    # so the first day of a month remains safe before the new release appears.
    candidates: list[str] = []
    year, month = today.year, today.month
    for offset in range(1, 4):
        candidate_month = ((month - 1 - offset) % 12) + 1
        name = _MONTHS[candidate_month - 1]
        if name not in candidates:
            candidates.append(name)

    for month_name in candidates:
        try:
            request = urllib.request.Request(
                _BASE_URL.format(month=month_name),
                # ISM's edge currently serves an interstitial to custom bot UAs
                # while its public report remains available to ordinary HTTP clients.
                headers={"User-Agent": "curl/8.7.1", "Accept": "*/*"},
            )
            context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                rows = parse_manufacturing_pmi(response.read().decode("utf-8", errors="replace"))
            if rows:
                return rows
        except Exception:
            continue
    return []
