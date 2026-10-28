"""Financial metrics with lineage: every number in a report names the XBRL
concept, period and filing accession number it came from."""

from __future__ import annotations

import ast
import hashlib
import json
import operator
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

# name -> formula over item names. The risk agent re-evaluates these
# strings itself against independently fetched values.
FORMULAS: dict[str, str] = {
    "gross_margin": "gross_profit / revenue",
    "operating_margin": "operating_income / revenue",
    "net_margin": "net_income / revenue",
    "current_ratio": "current_assets / current_liabilities",
    "liabilities_to_equity": "total_liabilities / equity",
    "return_on_equity": "net_income / equity",
    "free_cash_flow": "operating_cash_flow - capex",
    "revenue_growth": "(revenue - revenue_prior) / revenue_prior",
}


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def formula_names(formula: str) -> list[str]:
    return sorted({n.id for n in ast.walk(ast.parse(formula, mode="eval")) if isinstance(n, ast.Name)})


def evaluate(formula: str, values: dict[str, Decimal]) -> Decimal:
    """Evaluate `a / b`, `a - b`, `(a - b) / b` style formulas over Decimals.
    Only names, + - * / and parentheses are allowed."""

    def ev(node):
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        raise ValueError(f"unsupported expression in formula: {ast.dump(node)}")

    return ev(ast.parse(formula, mode="eval").body)


def _periods(facts: dict[str, Any]) -> list[str]:
    revenue = facts["items"].get("revenue", [])
    return [p["end"] for p in revenue]


def _lookup(facts: dict[str, Any], item: str, end: str) -> dict[str, Any] | None:
    return next((p for p in facts["items"].get(item, []) if p["end"] == end), None)


def compute(facts: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    periods = _periods(facts)
    for i, end in enumerate(periods):
        prior_end = periods[i + 1] if i + 1 < len(periods) else None
        for name, formula in FORMULAS.items():
            inputs, values, missing = [], {}, []
            for item in formula_names(formula):
                base, period = (item.removesuffix("_prior"), prior_end) if item.endswith("_prior") else (item, end)
                fact = _lookup(facts, base, period) if period else None
                if fact is None:
                    missing.append(item)
                    continue
                values[item] = Decimal(str(fact["value"]))
                inputs.append({"name": item, "item": base, "concept": fact["concept"], "end": fact["end"],
                               "value": fact["value"], "accession": fact["accession"]})
            metric = {"metric": name, "period_end": end, "formula": formula, "inputs": inputs}
            if missing:
                out.append({**metric, "value": None, "missing": missing})
                continue
            try:
                metric["value"] = float(round(evaluate(formula, values), 6))
            except (DivisionByZero, InvalidOperation, ZeroDivisionError):
                metric.update(value=None, missing=["division by zero"])
            out.append(metric)
    return out


def build_report(company: dict[str, Any], facts: dict[str, Any], filings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "company": {"name": company["name"], "ticker": company["ticker"], "cik": company["cik"]},
        "metrics": compute(facts),
        "sources": [{"accession": f["accession"], "form": f["form"], "filed": f["filed"], "url": f["url"]}
                    for f in filings],
    }
