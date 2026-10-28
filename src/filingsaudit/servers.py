"""The two MCP servers.

    edgar    read-only SEC EDGAR data
    reports  the release gate: publishes a report only with a valid risk approval
"""

from __future__ import annotations

import json
import os
import threading
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentmesh import mcp_app
from filingsaudit.edgar import EdgarApi
from filingsaudit.metrics import canonical_sha256
from filingsaudit.risk import ApprovalError, verify_approval


def build_edgar_server(api: EdgarApi) -> MCPServer:
    server = MCPServer("edgar", instructions="Read-only SEC EDGAR filings and XBRL financial data.")

    async def run(fn, *args):
        return await anyio.to_thread.run_sync(partial(fn, *args))

    @server.tool()
    async def lookup_company(ticker: str) -> dict[str, Any]:
        """Resolve a ticker to the SEC registrant (CIK and name)."""
        return await run(api.lookup, ticker)

    @server.tool()
    async def list_filings(cik: int, form: str = "10-K", limit: int = 10) -> list[dict[str, Any]]:
        """Most recent filings of one form type, with accession numbers and document URLs."""
        return await run(api.filings, cik, form, limit)

    @server.tool()
    async def get_annual_facts(cik: int, years: int = 4) -> dict[str, Any]:
        """Annual XBRL values (revenue, margins inputs, balance sheet, cash flow) with the
        accession number each value came from."""
        return await run(api.annual_facts, cik, years)

    return server


def build_reports_server(public_key_pem: bytes, out_dir: str | Path) -> MCPServer:
    server = MCPServer("reports", instructions="Release gate for analyst reports.")
    out_dir = Path(out_dir)
    ledger = out_dir / "used_approvals.log"  # survives restarts, so approvals can't be replayed
    used: set[str] = set(ledger.read_text().split()) if ledger.exists() else set()
    lock = threading.Lock()

    @server.tool()
    def publish_report(report: dict[str, Any], approval: str) -> dict[str, Any]:
        """Publish a report. Requires a risk-agent approval signed over this exact report."""
        # ToolError (not ValueError) so the refusal reason reaches the caller
        try:
            claims = verify_approval(report, approval, public_key_pem)
        except ApprovalError as exc:
            raise ToolError(f"publication refused: {exc}") from exc
        with lock:
            if claims["jti"] in used:
                raise ToolError("publication refused: approval already used")
            used.add(claims["jti"])
            out_dir.mkdir(parents=True, exist_ok=True)
            with ledger.open("a") as fh:
                fh.write(claims["jti"] + "\n")
        digest = canonical_sha256(report)
        ticker = report.get("company", {}).get("ticker", "report")
        path = out_dir / f"{ticker}-{digest[:12]}.json"
        path.write_text(json.dumps({"report": report, "approval": approval, "approval_claims": claims}, indent=2))
        return {"published": str(path), "report_sha256": digest, "approval_id": claims["jti"]}

    return server


def edgar_app():
    return mcp_app(build_edgar_server(EdgarApi()))


def reports_app():
    key = Path(os.environ["RISK_PUBLIC_KEY_FILE"]).read_bytes()
    return mcp_app(build_reports_server(key, os.environ.get("REPORTS_DIR", "reports")))
