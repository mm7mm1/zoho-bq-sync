#!/usr/bin/env python3
"""One-time OAuth bootstrap for a Zoho Self Client.

1. https://api-console.zoho.com -> Self Client -> "Generate Code" with scopes
   ZohoCRM.modules.ALL,ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ
   (the grant code lives 3-10 minutes).
2. Run:
     export ZOHO_CLIENT_ID=... ZOHO_CLIENT_SECRET=...
     python scripts/exchange_grant_token.py --dc us-developer --code 1000.xxxx --out secrets/zoho-oauth.json
3. Upload the file to Secret Manager (see docs/deployment.md) and delete the local copy.

The refresh token is written to a 0600 file, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zoho_bq_sync.config import DATA_CENTERS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dc", default="us", choices=sorted(DATA_CENTERS))
    parser.add_argument("--code", required=True, help="grant code from the Zoho API console")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    client_id = os.environ.get("ZOHO_CLIENT_ID")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in the environment", file=sys.stderr)
        return 1

    accounts_url, default_api = DATA_CENTERS[args.dc]
    response = requests.post(
        f"{accounts_url}/oauth/v2/token",
        data={"grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret, "code": args.code},
        timeout=30,
    )
    body = response.json() if response.content else {}
    if "refresh_token" not in body:
        print(f"Token exchange failed: HTTP {response.status_code} error={body.get('error', 'unknown')}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": body["refresh_token"],
                "accounts_url": accounts_url,
                # Developer/sandbox orgs need the dedicated host even if the token response says otherwise.
                "api_domain": default_api if args.dc.startswith("us-") else body.get("api_domain", default_api),
            },
            handle,
            indent=2,
        )
    print(f"Wrote {args.out} (scopes: {body.get('scope', 'n/a')}, api_domain: {body.get('api_domain', 'n/a')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
