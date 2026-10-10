"""Run the preserved Paper wallet against live JEV with the original fee ledger.

Normal operation uses the OpenRouter Key limit and records per-session costs.
--local-budget enables the legacy local ceilings; --prepare writes a legacy grant.
Credentials are read locally into this process, never into a file or command line.
"""

# ruff: noqa: E402 -- standalone workspace tool.
import argparse
import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("jev_diagnostic", ROOT / "tools/diagnose-jev-real.py")
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)

from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.domain.routing import ModelPrice
from agent_platform.runtime.clock import SystemClock
from agent_platform.web.app import create_app


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument(
        "--read-only",
        action="store_true",
        help="Load the existing policy and wallet with all model calls blocked",
    )
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument(
        "--local-budget", action="store_true",
        help="Use the legacy local fee ceilings instead of the OpenRouter Key limit",
    )
    background_file = ROOT / "data/jev-background-flash.local.json"
    parser.add_argument(
        "--background-model-config",
        type=Path,
        default=background_file if background_file.exists() else None,
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data/jev-paper-continuous.local.json"
    )
    args = parser.parse_args()
    clock = SystemClock()
    credentials = diagnostic.trial_tool.read_key()
    if args.prepare:
        diagnostic.read_inputs()  # Initial continuation retains the same selection and flat wallet.
        if args.config.exists():
            raise ValueError("existing_run_config_is_not_extended")
        spent, held = diagnostic.exposure()
        with httpx.Client(proxy=diagnostic.trial_tool.PROXY, trust_env=False, timeout=10) as http:
            response = http.get(
                "https://openrouter.ai/api/v1/key",
                headers={
                    "Authorization": "Bearer " + credentials.api_key.get_secret_value(),
                },
            )
            if response.status_code != 200:
                raise ValueError("free_key_authentication_failed")
            data = response.json()["data"]
        limit = Decimal(str(data["limit"])) if data.get("limit") is not None else Decimal("1")
        ceiling = min(Decimal("0.1"), limit, Decimal("1"))
        if spent + held >= ceiling or ceiling < Decimal("0.02"):
            raise ValueError("existing_global_budget_unavailable")
        now = clock.utcnow()
        policy = PaperTrialPolicy(
            grant_id="continuous-paper-v1",
            trial_total_usd=str(ceiling),
            single_call_usd="0.02",
            issued_at=now,
            expires_at=None,
            price=ModelPrice(
                version="jev-continuous-verified-20261008",
                input_usd_per_million="0.042",
                output_usd_per_million="0",
                verified_at=now,
                valid_until=None,
            ),
        )
        with args.config.open("x", encoding="utf-8") as output:
            output.write(policy.model_dump_json(indent=2) + "\n")
        print(
            json.dumps(
                {
                    "scope": "cumulative_real_jev_paper_without_expiry",
                    "config": str(args.config),
                    "policy": policy.model_dump(mode="json"),
                    "prior_actual_usd": str(spent),
                    "prior_held_usd": str(held),
                    "model_dispatch_attempts": 0,
                    "wallet": str(diagnostic.WALLET_DATABASE),
                    "shared_budget": str(diagnostic.trial_tool.SHARED_BUDGET),
                }
            )
        )
        return
    policy = PaperTrialPolicy.model_validate_json(args.config.read_text(encoding="utf-8"))
    if not args.read_only:
        policy.validate_active(clock.utcnow())
    os.environ["OPENROUTER_API_KEY"] = credentials.api_key.get_secret_value()
    config = diagnostic.RuntimeConfig(
        paper=True,
        live_public=True,
        futures_task_only=True,
        market_archive=False,
        paper_model_config=args.config,
        paper_read_only=args.read_only,
        futures_proxy=diagnostic.trial_tool.PROXY,
        openrouter_proxy=diagnostic.trial_tool.PROXY,
        model_budget_database=diagnostic.trial_tool.SHARED_BUDGET,
        model_budget_provider_managed=not args.local_budget,
        jev_workbench=True,
        background_model_config=args.background_model_config,
        operation={"mode": "auto", "execution_environment": "paper"},
        model_modules={"jev_trader": {"enabled": True}},
    )
    # Runtime startup recovers the existing wallet into paused state. Its protected
    # Web start action is separate; no grant alone authorizes a backend order.
    uvicorn.run(
        create_app(diagnostic.WALLET_DATABASE, runtime_config=config),
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"run_setup_failed": type(error).__name__}))
        raise SystemExit(1) from None
