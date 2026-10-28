import copy
from datetime import date, timedelta

import pytest
from edgar_mock import LATEST_FY, RESTATED_ORIGINAL, TICKER, YEARS, accession, api

from filingsaudit.metrics import build_report, canonical_sha256, evaluate
from filingsaudit.risk import ApprovalError, decision, generate_keys, run_checks, sign_approval, verify_approval


@pytest.fixture(scope="module")
def data():
    a = api()
    company = a.lookup(TICKER)
    facts = a.annual_facts(company["cik"])
    filings = a.filings(company["cik"])
    return company, facts, filings, build_report(company, facts, filings)


def metric(report, name, fy):
    return next(m for m in report["metrics"] if m["metric"] == name and m["period_end"] == f"{fy}-12-31")


def test_annual_selection_and_restatement(data):
    _, facts, filings, _ = data
    revenue = facts["items"]["revenue"]
    assert [p["end"] for p in revenue] == [f"{fy}-12-31" for fy in YEARS]  # quarterly fact ignored
    restated = revenue[2]
    assert restated["value"] == 900_000_000 and restated["accession"] == accession(YEARS[1])
    assert restated["alternatives"] == [{"value": RESTATED_ORIGINAL, "accession": accession(YEARS[2]),
                                         "filed": f"{YEARS[2] + 1}-02-20"}]
    assert [f["form"] for f in filings] == ["10-K"] * 4


def test_metric_values_and_lineage(data):
    report = data[3]
    gm = metric(report, "gross_margin", LATEST_FY)
    assert gm["value"] == pytest.approx(500 / 1200)
    assert {i["accession"] for i in gm["inputs"]} == {accession(LATEST_FY)}
    assert metric(report, "revenue_growth", LATEST_FY)["value"] == pytest.approx(0.2)
    assert metric(report, "free_cash_flow", LATEST_FY)["value"] == 150_000_000
    oldest = metric(report, "revenue_growth", YEARS[-1])
    assert oldest["value"] is None and oldest["missing"] == ["revenue_prior"]


def test_formula_evaluator_rejects_code():
    from decimal import Decimal

    assert evaluate("(a - b) / b", {"a": Decimal(3), "b": Decimal(2)}) == Decimal("0.5")
    with pytest.raises(ValueError):
        evaluate("__import__('os').system('true')", {})


def test_clean_report_is_approved(data):
    _, facts, _, report = data
    results = run_checks(report, facts)
    assert decision(results) == "approved"
    statuses = {r["check"]: r["status"] for r in results}
    assert statuses["restatements"] == "warn"  # visible, but not blocking
    assert statuses["provenance"] == statuses["recomputation"] == "pass"


@pytest.mark.parametrize("tamper,failing", [
    (lambda r: metric(r, "gross_margin", LATEST_FY).update(value=0.9), "recomputation"),
    (lambda r: metric(r, "gross_margin", LATEST_FY)["inputs"][0].update(value=1), "provenance"),
    (lambda r: metric(r, "net_margin", LATEST_FY)["inputs"][0].update(accession="0000000000-00-000000"), "provenance"),
    (lambda r: metric(r, "net_margin", LATEST_FY).update(formula="net_income / cost_of_revenue"), "recomputation"),
    (lambda r: r.update(metrics=[m for m in r["metrics"] if m["metric"] != "current_ratio"]), "completeness"),
])
def test_tampered_reports_are_rejected(data, tamper, failing):
    _, facts, _, report = data
    bad = copy.deepcopy(report)
    tamper(bad)
    results = run_checks(bad, facts)
    assert decision(results) == "rejected"
    assert failing in {r["check"] for r in results if r["status"] == "fail"}


def test_stale_data_is_rejected(data):
    _, facts, _, report = data
    results = run_checks(report, facts, today=date.today() + timedelta(days=600))
    assert [r["check"] for r in results if r["status"] == "fail"] == ["freshness"]


def test_approval_binds_to_exact_report(tmp_path, data):
    _, facts, _, report = data
    private, public = generate_keys(tmp_path)
    token = sign_approval(report, run_checks(report, facts), private.read_bytes())
    claims = verify_approval(report, token, public.read_bytes())
    assert claims["sub"] == canonical_sha256(report)

    altered = copy.deepcopy(report)
    altered["metrics"][0]["value"] = 0.99
    with pytest.raises(ApprovalError, match="different report"):
        verify_approval(altered, token, public.read_bytes())

    _, other_public = generate_keys(tmp_path / "other")
    with pytest.raises(ApprovalError):
        verify_approval(report, token, other_public.read_bytes())


async def test_replay_blocked_across_reports_server_restart(tmp_path, data):
    from mcp import Client

    from filingsaudit.servers import build_reports_server

    _, facts, _, report = data
    private, public = generate_keys(tmp_path / "keys")
    token = sign_approval(report, run_checks(report, facts), private.read_bytes())
    args = {"report": report, "approval": token}
    async with Client(build_reports_server(public.read_bytes(), tmp_path / "out")) as client:
        assert not (await client.call_tool("publish_report", args)).is_error
    async with Client(build_reports_server(public.read_bytes(), tmp_path / "out")) as client:  # "restarted"
        result = await client.call_tool("publish_report", args)
        assert result.is_error and "already used" in result.content[0].text
