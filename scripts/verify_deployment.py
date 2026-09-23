"""Verify that Render and the public frontend contract match a pushed revision."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def fetch_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "TradingDeskDeploymentCheck/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def verify(base_url: str, expected_revision: str) -> dict:
    base = base_url.rstrip("/")
    health = fetch_json(f"{base}/v1/health")
    deployed = str(health.get("deployment_revision") or "")
    expected = str(expected_revision or "")[:12]
    if expected and not deployed.startswith(expected):
        raise RuntimeError(f"Render revision {deployed or 'unknown'} does not match {expected}")

    manifest = fetch_json(f"{base}/v1/app-manifest")
    pages = manifest.get("pages") or []
    incomplete = [page.get("key") for page in pages if page.get("status") != "shared" or page.get("missing")]
    if incomplete:
        raise RuntimeError(f"Incomplete frontend page contracts: {', '.join(map(str, incomplete))}")
    if not manifest.get("contract_fingerprint"):
        raise RuntimeError("Frontend contract fingerprint is missing")
    for page in pages:
        if not page.get("sections") or not page.get("response_keys"):
            raise RuntimeError(f"Page contract is incomplete: {page.get('key')}")

    regime = fetch_json(f"{base}/v1/regime")
    if not isinstance(regime.get("regime"), dict):
        raise RuntimeError("Market regime response is missing")
    decision = fetch_json(f"{base}/v1/decisions/NVDA")
    for key in ("decision", "security_profile", "research", "analyze_page"):
        if key not in decision:
            raise RuntimeError(f"Analyze response is missing {key}")
    return {
        "revision": deployed,
        "contract_version": manifest.get("contract_version"),
        "contract_fingerprint": manifest.get("contract_fingerprint"),
        "page_count": len(pages),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    last_error = None
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            print(json.dumps(verify(args.base_url, args.revision), sort_keys=True))
            return 0
        except (RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            print(f"Attempt {attempt}/{args.attempts}: {exc}")
            if attempt < args.attempts:
                time.sleep(max(1, args.interval))
    raise SystemExit(f"Deployment verification failed: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
