"""SYNTHETIC SEC EDGAR responses for a fictional company (not real data).

Example Widgets Inc. (EXWD, CIK 1234567) with four fiscal years. Each 10-K
repeats the prior year as a comparative, the FY-2 revenue was restated in
the following 10-K, and quarterly (10-Q) facts are mixed in to make sure
they are ignored. Dates are relative to today so freshness checks pass.
"""

from datetime import date

import httpx

CIK, TICKER, NAME = 1234567, "EXWD", "Example Widgets Inc."
LATEST_FY = date.today().year - 1 if date.today().month >= 3 else date.today().year - 2
YEARS = [LATEST_FY - i for i in range(4)]  # newest first

# fiscal year -> item -> value (USD)
DATA = {
    YEARS[0]: dict(Revenues=1_200_000_000, CostOfRevenue=700_000_000, GrossProfit=500_000_000, OperatingIncomeLoss=180_000_000,
                   NetIncomeLoss=120_000_000, AssetsCurrent=600_000_000, LiabilitiesCurrent=300_000_000, Liabilities=900_000_000,
                   StockholdersEquity=750_000_000, NetCashProvidedByUsedInOperatingActivities=210_000_000,
                   PaymentsToAcquirePropertyPlantAndEquipment=60_000_000),
    YEARS[1]: dict(Revenues=1_000_000_000, CostOfRevenue=600_000_000, GrossProfit=400_000_000, OperatingIncomeLoss=140_000_000,
                   NetIncomeLoss=90_000_000, AssetsCurrent=550_000_000, LiabilitiesCurrent=310_000_000, Liabilities=880_000_000,
                   StockholdersEquity=640_000_000, NetCashProvidedByUsedInOperatingActivities=170_000_000,
                   PaymentsToAcquirePropertyPlantAndEquipment=55_000_000),
    YEARS[2]: dict(Revenues=900_000_000, CostOfRevenue=560_000_000, GrossProfit=340_000_000, OperatingIncomeLoss=100_000_000,
                   NetIncomeLoss=60_000_000, AssetsCurrent=500_000_000, LiabilitiesCurrent=320_000_000, Liabilities=860_000_000,
                   StockholdersEquity=560_000_000, NetCashProvidedByUsedInOperatingActivities=130_000_000,
                   PaymentsToAcquirePropertyPlantAndEquipment=50_000_000),
    YEARS[3]: dict(Revenues=800_000_000, CostOfRevenue=520_000_000, GrossProfit=280_000_000, OperatingIncomeLoss=70_000_000,
                   NetIncomeLoss=40_000_000, AssetsCurrent=450_000_000, LiabilitiesCurrent=330_000_000, Liabilities=840_000_000,
                   StockholdersEquity=500_000_000, NetCashProvidedByUsedInOperatingActivities=100_000_000,
                   PaymentsToAcquirePropertyPlantAndEquipment=45_000_000),
}
RESTATED_ORIGINAL = 905_000_000  # FY YEARS[2] revenue as first filed
INSTANT = {"AssetsCurrent", "LiabilitiesCurrent", "Liabilities", "StockholdersEquity"}


def accession(fy: int) -> str:
    return f"0001234567-{str(fy + 1)[2:]}-000010"


def filed(fy: int) -> str:
    return f"{fy + 1}-02-20"


def companyfacts() -> dict:
    gaap: dict = {}
    for report_fy in YEARS:  # each 10-K reports its year and the prior one
        for fy in (report_fy, report_fy - 1):
            if fy not in DATA:
                continue
            for tag, val in DATA[fy].items():
                if fy == YEARS[2] and report_fy == YEARS[2] and tag == "Revenues":
                    val = RESTATED_ORIGINAL
                fact = {"end": f"{fy}-12-31", "val": val, "accn": accession(report_fy), "fy": report_fy,
                        "fp": "FY", "form": "10-K", "filed": filed(report_fy)}
                if tag not in INSTANT:
                    fact["start"] = f"{fy}-01-01"
                gaap.setdefault(tag, {"units": {"USD": []}})["units"]["USD"].append(fact)
    # quarterly noise
    gaap["Revenues"]["units"]["USD"].append({"start": f"{LATEST_FY}-04-01", "end": f"{LATEST_FY}-06-30", "val": 1,
                                             "accn": "q", "fy": LATEST_FY, "fp": "Q2", "form": "10-Q", "filed": filed(LATEST_FY)})
    return {"cik": CIK, "entityName": NAME, "facts": {"us-gaap": gaap}}


def submissions() -> dict:
    forms = ["10-Q", *["10-K"] * 4]
    accns = ["q", *[accession(fy) for fy in YEARS]]
    return {"cik": str(CIK), "name": NAME, "filings": {"recent": {
        "accessionNumber": accns, "form": forms,
        "filingDate": [filed(LATEST_FY), *[filed(fy) for fy in YEARS]],
        "reportDate": [f"{LATEST_FY}-06-30", *[f"{fy}-12-31" for fy in YEARS]],
        "primaryDocument": ["q.htm", *[f"exwd-{fy}1231.htm" for fy in YEARS]],
    }}}


def handler(request: httpx.Request) -> httpx.Response:
    assert "@" in request.headers.get("user-agent", ""), "SEC requires a contact User-Agent"
    path = request.url.path
    if path == "/files/company_tickers.json":
        return httpx.Response(200, json={"0": {"cik_str": CIK, "ticker": TICKER, "title": NAME},
                                         "1": {"cik_str": 1, "ticker": "OTHR", "title": "Other"}})
    if path == f"/submissions/CIK{CIK:010d}.json":
        return httpx.Response(200, json=submissions())
    if path == f"/api/xbrl/companyfacts/CIK{CIK:010d}.json":
        return httpx.Response(200, json=companyfacts())
    return httpx.Response(404)


def api():
    from filingsaudit.edgar import EdgarApi

    return EdgarApi(user_agent="tests tests@example.com", cache_dir="", transport=httpx.MockTransport(handler),
                    data_url="https://data.sec.gov", www_url="https://www.sec.gov")
