"""Specialist agents and their grants (see mesh.yaml):

    filings  edgar: lookup_company, list_filings, get_annual_facts
    metrics  (no tools)          computes metrics with lineage from what it is given
    risk     edgar: get_annual_facts   re-fetches sources itself; signs approvals
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agentmesh import AgentContext, MeshClient, Skill, build_agent_app
from filingsaudit.metrics import build_report, canonical_sha256
from filingsaudit.risk import decision, run_checks, sign_approval


async def gather(payload: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    async with ctx.mesh.mcp("edgar") as edgar:
        company = await edgar.call("lookup_company", ticker=payload["ticker"])
        filings = await edgar.call("list_filings", cik=company["cik"], form="10-K", limit=int(payload.get("years", 4)))
        facts = await edgar.call("get_annual_facts", cik=company["cik"], years=int(payload.get("years", 4)))
    return {"company": company, "filings": filings, "facts": facts}


def filings(url: str = "http://127.0.0.1:9221", mesh: MeshClient | None = None):
    return build_agent_app(
        name="filings", description="Pulls 10-K filings and XBRL facts from SEC EDGAR", url=url, handler=gather,
        skills=[Skill("gather", "Gather filings", "Company, recent 10-Ks and annual XBRL facts",
                      tags=["edgar"], examples=['{"ticker": "AAPL", "years": 4}'])],
        mesh=mesh or MeshClient.from_env(),
    )


async def compute_metrics(payload: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    report = build_report(payload["company"], payload["facts"], payload["filings"])
    return {"report": report, "report_sha256": canonical_sha256(report)}


def metrics(url: str = "http://127.0.0.1:9222", mesh: MeshClient | None = None):
    return build_agent_app(
        name="metrics", description="Computes financial ratios with per-number lineage", url=url,
        handler=compute_metrics, mesh=mesh or MeshClient.from_env(),
        skills=[Skill("compute", "Compute metrics", "Margins, liquidity, leverage, growth", tags=["analysis"])],
    )


class RiskAgent:
    def __init__(self, private_key_pem: bytes):
        self._key = private_key_pem

    async def __call__(self, payload: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
        report = payload["report"]
        async with ctx.mesh.mcp("edgar") as edgar:
            source = await edgar.call("get_annual_facts", cik=report["company"]["cik"],
                                      years=int(payload.get("years", 4)))
        results = run_checks(report, source)
        verdict = decision(results)
        approval = sign_approval(report, results, self._key) if verdict == "approved" else None
        return {"decision": verdict, "checks": results, "report_sha256": canonical_sha256(report),
                "approval": approval}


def risk(url: str = "http://127.0.0.1:9223", mesh: MeshClient | None = None, private_key_pem: bytes | None = None):
    key = private_key_pem or Path(os.environ["RISK_SIGNING_KEY_FILE"]).read_bytes()
    return build_agent_app(
        name="risk", description="Independent controls; signs approvals for release", url=url,
        handler=RiskAgent(key), mesh=mesh or MeshClient.from_env(),
        skills=[Skill("review", "Risk review", "Provenance, recomputation, completeness, freshness, plausibility",
                      tags=["controls"])],
    )
