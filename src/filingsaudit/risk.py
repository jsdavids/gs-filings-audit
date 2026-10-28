"""Risk controls and the signed approval that gates publication.

The risk agent does not trust the metrics agent. It re-fetches source facts
from EDGAR itself, checks every input against them by period and accession
number, re-evaluates every formula, and applies freshness and plausibility
rules. Only if nothing fails does it sign an approval: an EdDSA (Ed25519)
JWT over the SHA-256 of the exact report. The reports server holds only the
public key, so it can verify approvals but never forge one.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from filingsaudit.metrics import FORMULAS, canonical_sha256, evaluate

APPROVAL_ISSUER = "risk-agent"
APPROVAL_TTL = 15 * 60
REQUIRED = {"gross_margin", "operating_margin", "net_margin", "current_ratio"}
BOUNDS = {  # outside these, a human should look before the number is quoted
    "gross_margin": (-1.0, 1.0), "operating_margin": (-5.0, 1.0), "net_margin": (-5.0, 1.0),
    "current_ratio": (0.0, 50.0), "liabilities_to_equity": (-50.0, 50.0),
    "return_on_equity": (-10.0, 10.0), "revenue_growth": (-1.0, 10.0),
}
MAX_FILING_AGE_DAYS = 15 * 31


def check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **extra}


def run_checks(report: dict[str, Any], source: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    results = []
    metrics = report.get("metrics", [])

    # 1. every input matches the independently fetched EDGAR fact
    index = {(item, p["end"]): p for item, periods in source["items"].items() for p in periods}
    mismatches = []
    for m in metrics:
        for inp in m["inputs"]:
            fact = index.get((inp["item"], inp["end"]))
            if fact is None or fact["value"] != inp["value"] or fact["accession"] != inp["accession"]:
                mismatches.append(f"{m['metric']}@{m['period_end']}: {inp['name']}={inp['value']} "
                                  f"({inp['accession']}) vs EDGAR {fact and fact['value']} ({fact and fact['accession']})")
    results.append(check("provenance", "fail" if mismatches else "pass",
                         f"{len(mismatches)} input(s) do not match EDGAR" if mismatches
                         else "all inputs match EDGAR by period and accession", mismatches=mismatches[:20]))

    # 2. every value re-evaluates to what the report says
    wrong = []
    for m in metrics:
        if m.get("value") is None:
            continue
        if m["metric"] not in FORMULAS or FORMULAS[m["metric"]] != m["formula"]:
            wrong.append(f"{m['metric']}: unrecognised formula {m['formula']!r}")
            continue
        recomputed = evaluate(m["formula"], {i["name"]: Decimal(str(i["value"])) for i in m["inputs"]})
        if abs(float(recomputed) - m["value"]) > 1e-6 * max(1.0, abs(m["value"])):
            wrong.append(f"{m['metric']}@{m['period_end']}: report {m['value']}, recomputed {float(recomputed)}")
    results.append(check("recomputation", "fail" if wrong else "pass",
                         f"{len(wrong)} metric(s) do not recompute" if wrong else "all metrics recompute",
                         errors=wrong))

    # 3. the latest period has every required metric
    latest = max((m["period_end"] for m in metrics), default=None)
    have = {m["metric"] for m in metrics if m["period_end"] == latest and m.get("value") is not None}
    missing = sorted(REQUIRED - have)
    results.append(check("completeness", "fail" if missing or latest is None else "pass",
                         f"latest period {latest} missing {missing}" if missing else f"latest period {latest} complete"))

    # 4. data is recent enough to quote
    filed = max((s["filed"] for s in report.get("sources", [])), default=None)
    age = (today - date.fromisoformat(filed)).days if filed else None
    results.append(check("freshness", "fail" if age is None or age > MAX_FILING_AGE_DAYS else "pass",
                         f"latest 10-K filed {filed} ({age} days ago)" if filed else "no source filings"))

    # 5. plausibility (warn only)
    odd = [f"{m['metric']}@{m['period_end']}={m['value']}" for m in metrics
           if m.get("value") is not None and m["metric"] in BOUNDS
           and not BOUNDS[m["metric"]][0] <= m["value"] <= BOUNDS[m["metric"]][1]]
    results.append(check("plausibility", "warn" if odd else "pass",
                         f"{len(odd)} value(s) outside expected ranges" if odd else "values within expected ranges",
                         values=odd))

    # 6. restatements (warn only): a later filing changed a reported number
    restated = [f"{item}@{p['end']}: now {p['value']} ({p['accession']}), earlier "
                + ", ".join(f"{a['value']} ({a['accession']})" for a in p["alternatives"])
                for item, periods in source["items"].items() for p in periods if p.get("alternatives")]
    results.append(check("restatements", "warn" if restated else "pass",
                         f"{len(restated)} restated value(s); report uses the latest filing" if restated
                         else "no restatements", values=restated[:20]))
    return results


def decision(results: list[dict[str, Any]]) -> str:
    return "rejected" if any(r["status"] == "fail" for r in results) else "approved"


# -- signing ------------------------------------------------------------------------

def generate_keys(directory: str | Path) -> tuple[Path, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private = directory / "risk_ed25519.pem"
    public = directory / "risk_ed25519.pub.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    private.chmod(0o600)
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def sign_approval(report: dict[str, Any], results: list[dict[str, Any]], private_key_pem: bytes) -> str:
    now = int(time.time())
    claims = {"iss": APPROVAL_ISSUER, "sub": canonical_sha256(report), "checks_sha256": canonical_sha256(results),
              "decision": "approved", "iat": now, "exp": now + APPROVAL_TTL, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, private_key_pem, algorithm="EdDSA")


class ApprovalError(Exception):
    pass


def verify_approval(report: dict[str, Any], token: str, public_key_pem: bytes) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, public_key_pem, algorithms=["EdDSA"], issuer=APPROVAL_ISSUER,
                            options={"require": ["sub", "exp", "iat", "jti"]})
    except jwt.PyJWTError as exc:
        raise ApprovalError(f"invalid approval: {exc}") from exc
    if claims.get("decision") != "approved":
        raise ApprovalError("approval token does not approve")
    if claims["sub"] != canonical_sha256(report):
        raise ApprovalError("approval was issued for a different report (hash mismatch)")
    return claims
