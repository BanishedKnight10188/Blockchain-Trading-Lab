"""Bounded historical answer replay. Never starts a wallet or submits a trade.

Uses the existing permanent fee policy and shared ledger; captures the bounded,
credential-filtered Decisions response so an adapter rejection can be explained.
Default is free. --execute permits at most three requests, stopping on rejection.
"""

# ruff: noqa: E402 -- standalone workspace tool.
import argparse
import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "answer_trial", ROOT / "tools/run-jev-futures-trial.py"
)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)

from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
from agent_platform.adapters.openrouter.transport import OpenRouterClient
from agent_platform.adapters.sqlite import open_store
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.bootstrap_paper import TrialDecisionModel
from agent_platform.domain.decision_models import DecisionModelRequest
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.ports.model import ModelCallFailed
from agent_platform.runtime.clock import SystemClock


class CapturingClient(OpenRouterClient):
    response = None

    async def post(self, *args, **kwargs):
        value = await super().post(*args, **kwargs)
        self.response = {
            k: value[k] for k in ("id", "model", "provider", "answers", "usage") if k in value
        }
        return value


def wallet_evidence(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = list(db.execute("SELECT body FROM futures_paper_wallets ORDER BY account_ref"))
        if any(json.loads(r[0])["status"] != "paused" for r in rows):
            raise ValueError("diagnostic_requires_paused_wallet")
        body = json.dumps(rows).encode()
        return {
            "wallet_hash": hashlib.sha256(body).hexdigest(),
            "trade_archive_count": db.execute(
                "SELECT count(*) FROM futures_trade_archive"
            ).fetchone()[0],
        }


async def run(args):
    task_path = (ROOT / "data/jev-futures-real-20261008-tasks" / (args.task + ".sqlite3")).resolve()
    if task_path.parent != (ROOT / "data/jev-futures-real-20261008-tasks").resolve():
        raise ValueError("invalid_task")
    policy = PaperTrialPolicy.model_validate_json(
        (ROOT / "data/jev-paper-continuous.local.json").read_text()
    )
    clock = SystemClock()
    policy.validate_active(clock.utcnow())
    before = wallet_evidence(task_path)
    with sqlite3.connect(task_path.as_uri() + "?mode=ro", uri=True) as db:
        source = json.loads(
            db.execute(
                "SELECT body FROM futures_trading_cycles WHERE request_id=?", (args.request,)
            ).fetchone()[0]
        )
    original = DecisionModelRequest.model_validate_json(json.dumps(source["model_request"]))
    report = {
        "scope": "historical_answer_replay_only",
        "source_request": args.request,
        "task_id": args.task,
        "source_diagnostic": source.get("diagnostic"),
        "wallet_started": False,
        "exchange_orders": 0,
        "attempts": [],
        "before": before,
    }
    if args.execute:
        stamp = clock.utcnow().strftime("%Y%m%dT%H%M%S%f")
        for path in (task_path, trial.SHARED_BUDGET):
            backup = (
                ROOT / "output/verification" / (path.stem + "-answer-before-" + stamp + ".sqlite3")
            )
            with (
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source_db,
                sqlite3.connect(backup) as destination_db,
            ):
                source_db.backup(destination_db)
    core = await open_store(trial.SHARED_BUDGET)
    credentials = trial.read_key()
    client = CapturingClient(clock, credentials=credentials, enabled=True, proxy_url=trial.PROXY)
    model = TrialDecisionModel(
        BudgetedDecisionModel(
            port=OpenRouterDecisionModel(client, clock),
            budgets=core,
            clock=clock,
            price=policy.price,
            daily_limit_usd=policy.trial_total_usd,
            max_single_cost_usd=policy.single_call_usd,
            hourly_call_limit=3600,
            settings=JevModuleSettings(enabled=True),
        ),
        policy,
        clock,
    )
    report["budget_before"] = await model.budget_status()
    try:
        for _ in range(args.max_calls if args.execute else 1):
            now, identity = clock.utcnow(), uuid4().hex
            data = original.model_dump(mode="json")
            data.update(
                request_id=identity,
                captured_at=now.isoformat(),
                deadline=(now + timedelta(seconds=15)).isoformat(),
            )
            data["route"].update(
                route_id="jev-answer-diag:" + identity, price_version=policy.price.version
            )
            request = DecisionModelRequest.model_validate_json(json.dumps(data))
            quote = model.model.quote(request)
            report["estimated_single_usd"] = str(quote.estimated_cost_usd)
            if not args.execute:
                break
            client.response = None
            began = asyncio.get_running_loop().time()
            try:
                response = await model.decide(request)
                item = {
                    "request_id": identity,
                    "status": "accepted",
                    "usage": response.usage.model_dump(mode="json"),
                    "choice_probability_adjustments": [
                        a.model_dump(mode="json", exclude={"original_probabilities"})
                        for a in response.choice_probability_adjustments
                    ],
                }
            except ModelCallFailed as error:
                item = {
                    "request_id": identity,
                    "status": "rejected",
                    "reason": error.reason,
                    "diagnostic": error.diagnostic.model_dump(mode="json")
                    if error.diagnostic
                    else None,
                    "usage": error.usage.model_dump(mode="json") if error.usage else None,
                }
            item.update(
                latency_ms=int((asyncio.get_running_loop().time() - began) * 1000),
                provider_response=client.response,
            )
            report["attempts"].append(item)
            if item["status"] == "rejected":
                break
    finally:
        report["budget_after"] = await model.budget_status()
        report["after"] = wallet_evidence(task_path)
        report["wallet_preserved"] = report["before"] == report["after"]
        await client.aclose()
        stamp = clock.utcnow().strftime("%Y%m%dT%H%M%S%f")
        output = ROOT / "output/verification" / ("jev-answer-diagnostic-" + stamp + ".json")
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "report": str(output),
                    "attempts": [
                        {k: v for k, v in a.items() if k != "provider_response"}
                        for a in report["attempts"]
                    ],
                    "estimated_single_usd": report.get("estimated_single_usd"),
                    "wallet_preserved": report["wallet_preserved"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-calls", type=int, choices=(1, 2, 3), default=3)
    asyncio.run(run(parser.parse_args()))
