"""filingsaudit command line.

    filingsaudit keygen                     create the risk agent's Ed25519 key pair in keys/
    filingsaudit analyze AAPL               run the pipeline (needs `agentmesh up mesh.yaml`)
    filingsaudit verify reports/AAPL-*.json check a published report's approval offline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agentmesh import MeshClient, MeshConfig, TokenIssuer
from filingsaudit.orchestrator import analyze, summary
from filingsaudit.risk import ApprovalError, generate_keys, verify_approval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filingsaudit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_key = sub.add_parser("keygen")
    p_key.add_argument("--dir", default="keys")
    p_an = sub.add_parser("analyze")
    p_an.add_argument("ticker")
    p_an.add_argument("--years", type=int, default=4)
    p_an.add_argument("--config", default="mesh.yaml")
    p_an.add_argument("--json", action="store_true")
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("path", type=Path)
    p_ver.add_argument("--public-key", type=Path, default=Path("keys/risk_ed25519.pub.pem"))
    args = parser.parse_args(argv)

    if args.cmd == "keygen":
        private, public = generate_keys(args.dir)
        print(f"wrote {private} (risk agent only) and {public} (reports server)")
    elif args.cmd == "analyze":
        config = MeshConfig.load(args.config)
        token = os.environ.get("AGENTMESH_TOKEN") or TokenIssuer(
            config.secret, config.gateway.issuer, config.gateway.audience).mint("orchestrator")
        result = asyncio.run(analyze(MeshClient(config.gateway.url, token), args.ticker, args.years))
        print(json.dumps(result, indent=2) if args.json else summary(result))
        return 0 if result["decision"] == "approved" else 2
    else:
        doc = json.loads(args.path.read_text())
        try:
            claims = verify_approval(doc["report"], doc["approval"], args.public_key.read_bytes())
        except ApprovalError as exc:
            print(f"INVALID: {exc}")
            return 1
        print(f"valid: approved by {claims['iss']} (approval {claims['jti']}), report sha256 {claims['sub']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
