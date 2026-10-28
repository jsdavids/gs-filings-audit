"""Whole pipeline through the gateway: filings -> metrics -> risk -> reports."""

import copy
import json

import pytest
from edgar_mock import TICKER, api

from agentmesh import ToolCallError, mcp_app, verify_chain
from agentmesh.testing import AgentSpec, LocalMesh
from filingsaudit import agents
from filingsaudit.orchestrator import analyze
from filingsaudit.risk import generate_keys
from filingsaudit.servers import build_edgar_server, build_reports_server

PRINCIPALS = {
    "orchestrator": {"agents": ["filings", "metrics", "risk"], "mcp": {"reports": ["publish_report"]}},
    "filings": {"mcp": {"edgar": ["lookup_company", "list_filings", "get_annual_facts"]}},
    "metrics": {},
    "risk": {"mcp": {"edgar": ["get_annual_facts"]}},
}


@pytest.fixture(scope="module")
def mesh(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gs")
    private, public = generate_keys(tmp / "keys")
    with LocalMesh(
        principals=PRINCIPALS,
        mcp_servers={
            "edgar": lambda: mcp_app(build_edgar_server(api())),
            "reports": lambda: mcp_app(build_reports_server(public.read_bytes(), tmp / "reports")),
        },
        agents={
            "filings": AgentSpec(agents.filings, "filings"),
            "metrics": AgentSpec(agents.metrics, "metrics"),
            "risk": AgentSpec(lambda url, mesh: agents.risk(url, mesh, private.read_bytes()), "risk"),
        },
        audit_path=tmp / "audit.jsonl",
    ) as m:
        m.tmp = tmp
        yield m


async def test_approved_report_is_published(mesh):
    result = await analyze(mesh.client("orchestrator"), TICKER)
    assert result["decision"] == "approved"
    published = json.loads(open(result["published"]["published"]).read())
    assert published["approval_claims"]["sub"] == result["report_sha256"]


async def test_publish_without_or_with_altered_approval_is_refused(mesh):
    orch = mesh.client("orchestrator")
    data = await orch.call_agent("filings", {"ticker": TICKER})
    computed = await orch.call_agent("metrics", data)
    review = await orch.call_agent("risk", {"report": computed["report"]})
    with pytest.raises(ToolCallError, match="publication refused"):
        await orch.call_tool("reports", "publish_report", report=computed["report"], approval="forged")
    altered = copy.deepcopy(computed["report"])
    altered["metrics"][0]["value"] = 0.99
    with pytest.raises(ToolCallError, match="different report"):
        await orch.call_tool("reports", "publish_report", report=altered, approval=review["approval"])
    await orch.call_tool("reports", "publish_report", report=computed["report"], approval=review["approval"])
    with pytest.raises(ToolCallError, match="already used"):  # no replay
        await orch.call_tool("reports", "publish_report", report=computed["report"], approval=review["approval"])


async def test_risk_rejects_tampered_report(mesh):
    orch = mesh.client("orchestrator")
    data = await orch.call_agent("filings", {"ticker": TICKER})
    report = (await orch.call_agent("metrics", data))["report"]
    report["metrics"][0]["inputs"][0]["value"] += 1
    review = await orch.call_agent("risk", {"report": report})
    assert review["decision"] == "rejected" and review["approval"] is None


async def test_segregation_of_duties(mesh):
    with pytest.raises(ToolCallError):  # metrics agent has no data access
        await mesh.client("metrics").call_tool("edgar", "lookup_company", ticker=TICKER)
    with pytest.raises(ToolCallError):  # risk can verify but never publish
        await mesh.client("risk").call_tool("reports", "publish_report", report={}, approval="x")
    with pytest.raises(ToolCallError):  # risk can't list filings, only re-read facts
        await mesh.client("risk").call_tool("edgar", "list_filings", cik=1234567)
    with pytest.raises(ToolCallError):  # orchestrator can't read EDGAR directly
        await mesh.client("orchestrator").call_tool("edgar", "get_annual_facts", cik=1234567)


async def test_audit_trail_counts_blocked_calls(mesh):
    path = mesh.gateway.audit.path
    assert verify_chain(path).ok
    denied = [r for r in mesh.gateway.audit.records if r["decision"] == "denied"]
    assert len(denied) >= 4
    publishes = [r for r in mesh.gateway.audit.records if r.get("tool") == "publish_report" and r["decision"] == "allowed"]
    assert publishes and all(r["response_sha256"] for r in publishes)
