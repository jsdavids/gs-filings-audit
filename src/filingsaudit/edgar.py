"""Client for SEC EDGAR's public JSON APIs (https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

SEC requires a User-Agent naming you and a contact email, and allows at
most 10 requests per second; set SEC_USER_AGENT="Your Name you@example.com".
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

DATA_URL = "https://data.sec.gov"
WWW_URL = "https://www.sec.gov"

# Concepts the metrics agent understands, in order of preference: companies
# report revenue under different tags depending on year and industry.
CONCEPTS: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}
INSTANT = {"current_assets", "current_liabilities", "total_liabilities", "equity"}


def pad_cik(cik: int | str) -> str:
    return f"{int(cik):010d}"


def _is_annual(fact: dict[str, Any], instant: bool) -> bool:
    if fact.get("form") not in ("10-K", "10-K/A") or fact.get("fp") != "FY":
        return False
    if instant:
        return "start" not in fact
    if "start" not in fact:
        return False
    days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
    return 350 <= days <= 380


class EdgarApi:
    def __init__(self, user_agent: str | None = None, cache_dir: str | Path | None = None,
                 transport: httpx.BaseTransport | None = None, data_url: str = DATA_URL, www_url: str = WWW_URL):
        user_agent = user_agent or os.environ.get("SEC_USER_AGENT")
        if not user_agent or "@" not in user_agent:
            raise RuntimeError('SEC requires a User-Agent with contact info: set SEC_USER_AGENT="Name email@domain"')
        cache = cache_dir if cache_dir is not None else os.environ.get("FILINGSAUDIT_CACHE", "cache/edgar")
        self.cache_dir = Path(cache) if cache else None
        self.cache_ttl = 12 * 3600
        self.http = httpx.Client(transport=transport, timeout=60.0, headers={"user-agent": user_agent})
        self.data_url, self.www_url = data_url, www_url
        self._lock = threading.Lock()
        self._last = 0.0

    def _get_json(self, url: str) -> Any:
        cached = self.cache_dir / hashlib.sha256(url.encode()).hexdigest()[:32] if self.cache_dir else None
        if cached and cached.exists() and time.time() - cached.stat().st_mtime < self.cache_ttl:
            return json.loads(cached.read_text())
        with self._lock:  # stay under SEC's 10 requests/second
            wait = 0.11 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        resp = self.http.get(url)
        resp.raise_for_status()
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(resp.text)
        return resp.json()

    def lookup(self, ticker: str) -> dict[str, Any]:
        for row in self._get_json(f"{self.www_url}/files/company_tickers.json").values():
            if row["ticker"].upper() == ticker.upper():
                return {"cik": int(row["cik_str"]), "ticker": row["ticker"], "name": row["title"]}
        raise LookupError(f"no SEC registrant with ticker {ticker}")

    def filings(self, cik: int | str, form: str | None = "10-K", limit: int = 10) -> list[dict[str, Any]]:
        recent = self._get_json(f"{self.data_url}/submissions/CIK{pad_cik(cik)}.json")["filings"]["recent"]
        rows = []
        for i, accn in enumerate(recent["accessionNumber"]):
            if form and recent["form"][i] != form:
                continue
            rows.append({"accession": accn, "form": recent["form"][i], "filed": recent["filingDate"][i],
                         "period": recent["reportDate"][i], "document": recent["primaryDocument"][i],
                         "url": f"{self.www_url}/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{recent['primaryDocument'][i]}"})
            if len(rows) >= limit:
                break
        return rows

    def annual_facts(self, cik: int | str, years: int = 4) -> dict[str, Any]:
        """Annual (10-K, full fiscal year) values for every CONCEPTS item, newest
        first, each with the accession number of the filing it came from.

        When several filings report the same period (later 10-Ks repeat prior
        years as comparatives), the most recently filed value wins and the
        others are kept under `alternatives` so restatements are visible."""
        data = self._get_json(f"{self.data_url}/api/xbrl/companyfacts/CIK{pad_cik(cik)}.json")
        gaap = data.get("facts", {}).get("us-gaap", {})
        out: dict[str, Any] = {"cik": int(cik), "entity": data.get("entityName"), "items": {}}
        for item, tags in CONCEPTS.items():
            for tag in tags:
                facts = [f for f in gaap.get(tag, {}).get("units", {}).get("USD", [])
                         if _is_annual(f, item in INSTANT)]
                if not facts:
                    continue
                by_end: dict[str, list[dict[str, Any]]] = {}
                for f in facts:
                    by_end.setdefault(f["end"], []).append(f)
                periods = []
                for end in sorted(by_end, reverse=True)[:years]:
                    reports = sorted(by_end[end], key=lambda f: f["filed"], reverse=True)
                    latest = reports[0]
                    periods.append({
                        "end": end, "start": latest.get("start"), "value": latest["val"], "concept": tag,
                        "accession": latest["accn"], "filed": latest["filed"], "form": latest["form"],
                        "alternatives": [{"value": f["val"], "accession": f["accn"], "filed": f["filed"]}
                                         for f in reports[1:] if f["val"] != latest["val"]],
                    })
                out["items"][item] = periods
                break
        return out
