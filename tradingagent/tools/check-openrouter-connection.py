"""Free connection diagnostics using the production pre-send reconnect boundary.

All apparent inference requests are replaced at the transport boundary with an
unauthenticated GET /api/v1/key. No credentials are read and no model is called.
"""
# ruff: noqa: E402 -- standalone workspace tool.

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_platform.adapters.openrouter.transport import (
    OpenRouterClient,
    OpenRouterCredentials,
    OpenRouterError,
)
from agent_platform.runtime.clock import SystemClock


class _FreeProbeTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy):
        self.transport = httpx.AsyncHTTPTransport(proxy=proxy, trust_env=False)

    async def handle_async_request(self, request):
        # Fixed GET, empty body and no copied headers, including Authorization.
        free = httpx.Request(
            "GET", "https://openrouter.ai/api/v1/key", extensions=request.extensions.copy()
        )
        response = await self.transport.handle_async_request(free)
        # Drain the tiny unauthenticated response so reused probes retain sockets;
        # production inference already consumes its complete 200 JSON body.
        await response.aread()
        return response

    async def aclose(self):
        await self.transport.aclose()


async def probe(client, clock):
    try:
        await client.post(
            "/api/alpha/decisions",
            {"scope": "free_connection_probe"},
            clock.utcnow() + timedelta(seconds=3),
        )
        raise RuntimeError("unexpected_unauthenticated_success")
    except OpenRouterError as error:
        return {
            "result": str(error),
            "connection_ok": str(error) == "provider_http_401",
            "transport_evidence": error.transport_evidence.model_dump(mode="json")
            if error.transport_evidence
            else None,
        }


async def run(args):
    # This deliberately invalid credential never reaches the forwarded request.
    credentials = OpenRouterCredentials(api_key="offline-free-connection-probe")
    clock = SystemClock()
    rows = []
    for _ in range(args.cold):
        async with httpx.AsyncClient(transport=_FreeProbeTransport(args.proxy)) as http:
            client = OpenRouterClient(clock, credentials=credentials, enabled=True, client=http)
            rows.append({"pool": "cold", **await probe(client, clock)})
    async with httpx.AsyncClient(transport=_FreeProbeTransport(args.proxy)) as http:
        client = OpenRouterClient(clock, credentials=credentials, enabled=True, client=http)
        for _ in range(args.warm):
            rows.append({"pool": "reused", **await probe(client, clock)})
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "scope": "unauthenticated_GET_key_no_inference",
        "model_calls": 0,
        "samples": rows,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = ROOT / "output/verification" / f"openrouter-free-reconnect-{stamp}.json"
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps({"report": str(target), "model_calls": 0, "samples": rows}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--cold", type=int, choices=range(1, 11), default=6)
    parser.add_argument("--warm", type=int, choices=range(1, 11), default=4)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
