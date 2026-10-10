import ast
from pathlib import Path


def test_new_domain_and_application_do_not_import_adapters():
    root = Path("agent_platform")
    names = (
        "event_agent",
        "agent_tools",
        "agent_context",
        "agent_orchestrator",
        "agent_intents",
        "agent_models",
        "agent_trade_evidence",
        "trade_authorization",
        "trade_intents",
        "position_protection",
        "position_guardian",
        "shared_futures_risk",
        "trading_scope",
    )
    for layer in ("domain", "application"):
        for name in names:
            path = root / layer / (name + ".py")
            if not path.exists():
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert all(not m.startswith("agent_platform.adapters") for m in modules), str(path)
