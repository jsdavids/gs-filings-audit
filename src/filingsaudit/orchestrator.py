"""filings -> metrics -> risk -> (only if approved) reports.publish_report.

The orchestrator can call publish_report, but the reports server only
accepts an approval signed with the risk agent's private key over the exact
report bytes, so neither a bug nor a prompt-injected orchestrator can
release an unapproved or altered report.
"""

from __future__ import annotations

from typing import Any

from agentmesh import MeshClient


async def analyze(mesh: MeshClient, ticker: str, years: int = 4) -> dict[str, Any]:
    data = await mesh.call_agent("filings", {"ticker": ticker, "years": years})
    computed = await mesh.call_agent("metrics", data)
    review = await mesh.call_agent("risk", {"report": computed["report"], "years": years})
    result = {"ticker": ticker, "decision": review["decision"], "checks": review["checks"],
              "report": computed["report"], "report_sha256": computed["report_sha256"], "published": None}
    if review["decision"] == "approved":
        result["published"] = await mesh.call_tool("reports", "publish_report",
                                                   report=computed["report"], approval=review["approval"])
    return result


def summary(result: dict[str, Any]) -> str:
    lines = [f"{result['ticker']}: {result['decision'].upper()}  (report sha256 {result['report_sha256'][:16]}…)"]
    for c in result["checks"]:
        lines.append(f"  [{c['status']:>4}] {c['check']}: {c['detail']}")
    latest = max((m["period_end"] for m in result["report"]["metrics"]), default=None)
    for m in result["report"]["metrics"]:
        if m["period_end"] == latest and m.get("value") is not None:
            sources = ", ".join(sorted({i["accession"] for i in m["inputs"]}))
            lines.append(f"  {m['metric']:<22} {m['value']:>18,.4f}   {m['formula']}   [{sources}]")
    if result["published"]:
        lines.append(f"  published -> {result['published']['published']}")
    return "\n".join(lines)
