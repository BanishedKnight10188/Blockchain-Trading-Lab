"""Assembly smoke, without executing network, models, or installing test dependencies."""

from pathlib import Path

from agent_platform.config import RuntimeConfig
from agent_platform.domain.acceptance import AcceptanceCheck, AcceptanceReport
from agent_platform.domain.common import RunMode
from agent_platform.runtime.clock import SystemClock

_GAPS = (
    "manual_read_only_pending",
    "soak_pending",
    "model_provider_pending",
    "mcp_mapping_pending",
    "jev_unspecified",
    "discipline_configuration_pending",
)


async def run_acceptance(mode: RunMode, *, database_path: Path) -> AcceptanceReport:
    if mode != RunMode.ADVISORY:
        raise ValueError("offline assembly acceptance requires advisory mode")
    from fastapi.routing import APIRoute
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    from agent_platform.bootstrap import build_application_services
    from agent_platform.web.app import create_app

    clock = SystemClock()
    checks, services = [], None

    def check(name, passed):
        checks.append(AcceptanceCheck(name=name, passed=bool(passed)))

    try:
        async with build_application_services(database_path, RuntimeConfig()) as services:
            data = await services.system.current()
            check(
                "default_network_disabled",
                data["mode"] == "disabled" and not data["archive"]["enabled"],
            )
            check("paid_models_disabled", data["paid_models_enabled"] is False)
            check("jev_unspecified", data["jev_status"] == "unspecified")
            check("safe_runtime_running", data["runtime"]["status"] == "running")
            check("budget_readable", data["budget_status"] == "recorded")
            allowed = {
                "/api/controls",
                "/api/controls/advice",
                "/api/controls/trader",
                "/api/paper/configure",
                "/api/paper/start",
                "/api/paper/pause",
                "/api/sessions",
                "/api/sessions/{session_id}/style",
                "/api/sessions/{session_id}/state",
                "/api/feedback",
                "/api/attributions",
                "/api/reports",
                "/api/reports/{report_id}/verify",
                "/api/review-groups",
                "/api/review-groups/{group_id}/reviews",
                "/api/review-groups/{group_id}/followups",
                "/api/review-jobs/{job_id}/cancel",
            }
            app = create_app(database_path)
            writes, known_routes = set(), True
            for route in app.routes:
                if isinstance(route, APIRoute):
                    if route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
                        writes.add(route.path)
                elif isinstance(route, Mount):
                    known_routes &= route.path == "/static" and isinstance(route.app, StaticFiles)
                else:
                    known_routes = False
            check("funds_routes_absent", known_routes and writes <= allowed)
        stopped = (await services.runtime.health()).status
        check("owned_workers_stopped", stopped == "stopped")
        return AcceptanceReport(
            generated_at=clock.utcnow(),
            checks=tuple(checks),
            gaps=_GAPS,
            outcome="passed_with_gaps" if all(item.passed for item in checks) else "failed",
            shutdown_status=stopped,
        )
    except Exception:
        return AcceptanceReport(
            generated_at=clock.utcnow(),
            outcome="failed",
            checks=tuple(checks),
            gaps=_GAPS,
            shutdown_status="not_started" if services is None else "degraded",
            failure="local_assembly_unavailable",
        )
