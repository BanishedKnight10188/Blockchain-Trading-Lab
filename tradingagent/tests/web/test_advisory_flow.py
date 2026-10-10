"""The full advisory workbench records local decisions without execution privileges."""

import asyncio
import importlib
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_platform.application.attribution import AttributionService
from agent_platform.application.feedback import FeedbackService
from agent_platform.application.ledger_queries import LedgerQueryService
from agent_platform.application.queries import QueryService
from agent_platform.application.reports import ReportService
from agent_platform.application.reviews import ReviewService
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap import ApplicationServices
from agent_platform.runtime.review_jobs import ReviewJobService
from agent_platform.web.app import create_app
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_advice_queries import assembly
from tests.application.test_attribution import setup
from tests.domain.test_decisions import NOW
from tests.web.test_feedback_routes import Runtime, attribution_body, feedback_body

context = _context


def test_feedback_page_exposes_all_four_decisions(tmp_path):
    class Choices(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_kind = False
            self.options = []

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == "select":
                self.in_kind = attributes.get("name") == "kind"
            if tag == "option" and self.in_kind:
                self.options.append(attributes.get("value"))

        def handle_endtag(self, tag):
            if tag == "select":
                self.in_kind = False

    with TestClient(
        create_app(tmp_path / "choices.sqlite3"), base_url="http://127.0.0.1"
    ) as client:
        choices = Choices()
        choices.feed(client.get("/records").text)
        assert choices.options == ["independent", "accepted", "rejected", "modified"]


@asynccontextmanager
async def browser(context):
    await setup(context)
    factory, advice, _, cache, clock = await assembly(context)
    adapter = importlib.import_module("agent_platform.adapters.sqlite.store")
    queries = importlib.import_module("agent_platform.application.review_queries")
    status = importlib.import_module("agent_platform.application.system_queries")
    store = adapter.SqliteStore(context[0], clock=clock)
    overview = QueryService(cache, clock, advice=advice)
    jobs = ReviewJobService(store=store, clock=clock, account_ref="local-spot")
    services = ApplicationServices(
        SessionService(context[2], clock),
        overview,
        Runtime(),
        feedback=FeedbackService(
            store=store, states=store, snapshots=factory, clock=clock, account_ref="local-spot"
        ),
        attribution=AttributionService(store=store, clock=clock, account_ref="local-spot"),
        reports=ReportService(store=store, clock=clock, account_ref="local-spot"),
        ledger=LedgerQueryService(store=store, source=cache, account_ref="local-spot"),
        reviews=ReviewService(store=store, clock=clock, account_ref="local-spot"),
        review_jobs=jobs,
        review_queries=queries.ReviewQueryService(
            store=store, source=cache, account_ref="local-spot"
        ),
        system=status.SystemQueryService(
            queries=overview, budgets=store, clock=clock, review_jobs=jobs
        ),
    )
    app = create_app(context[0], services=services)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        page = await client.get("/")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        client.review_service = services.reviews
        client.system_service = services.system
        yield client, {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}, clock


@pytest.mark.parametrize(
    "path,script",
    [("/records", "records.js"), ("/reviews", "reviews.js"), ("/status", "status.js")],
)
def test_workbench_pages_are_accessible_with_local_assets(tmp_path, path, script):
    with TestClient(create_app(tmp_path / "pages.sqlite3"), base_url="http://127.0.0.1") as client:
        page = client.get(path)
        assert page.status_code == 200 and 'name="csrf-token"' in page.text
        assert "/static/" + script in page.text and "https://" not in page.text
        assert client.get("/static/" + script).status_code == 200
        assert "记录" in page.text and "复盘" in page.text and "系统" in page.text
        assert "unsafe-inline" not in page.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_feedback_trade_attribution_review_and_manual_followup_flow(context):
    async with browser(context) as (client, headers, clock):
        assert (await client.get("/api/overview")).json()["advice"]["style_strength"] == 67
        assert (
            await client.post("/api/feedback", json=feedback_body(), headers=headers)
        ).status_code == 201
        assert (
            await client.post("/api/attributions", json=attribution_body(), headers=headers)
        ).status_code == 201
        group = await client.post(
            "/api/review-groups",
            json=dict(group_id="web-group", cutoff=NOW.isoformat(), confirmed=True),
            headers=headers,
        )
        assert group.status_code == 201
        initial = dict(kind="initial", cutoff=NOW.isoformat(), confirmed=True)
        assert (
            await client.post("/api/review-groups/web-group/reviews", json=initial, headers=headers)
        ).status_code == 201
        clock.advance_to(NOW + timedelta(seconds=2))
        manual = dict(kind="manual", cutoff=clock.utcnow().isoformat(), confirmed=True)
        first = await client.post(
            "/api/review-groups/web-group/reviews", json=manual, headers=headers
        )
        assert first.status_code == 201
        assert (
            await client.post("/api/review-groups/web-group/reviews", json=manual, headers=headers)
        ).json() == first.json()
        records = (await client.get("/api/review-groups/web-group/versions")).json()
        assert len(records["versions"]) == 2 and records["mode"] == "fake"
        check = records["versions"][0]["trade_checks"][0]
        assert check["original_style"]["strength"] == 67 and check["action_deviation"] is True
        assert check["execution_discipline_status"] == "unavailable"
        for forbidden in (
            "account_ref",
            "local-spot",
            "order_id",
            "original_request_id",
            "publication_snapshot_id",
        ):
            assert forbidden not in str(records)


@pytest.mark.asyncio
async def test_review_writes_are_protected_confirmed_and_server_scoped(context):
    async with browser(context) as (client, headers, _):
        body = dict(group_id="web-group", cutoff=NOW.isoformat(), confirmed=True)
        assert (await client.post("/api/review-groups", json=body)).status_code == 403
        assert (
            await client.post(
                "/api/review-groups",
                json=body,
                headers={**headers, "Origin": "https://foreign.example"},
            )
        ).status_code == 403
        for extra in ({"confirmed": False}, {"confirmed": 1}, {"account_ref": "secret"}):
            response = await client.post(
                "/api/review-groups", json={**body, **extra}, headers=headers
            )
            assert response.status_code == 422 and "secret" not in response.text
        assert (
            await client.post("/api/review-groups", json=body, headers=headers)
        ).status_code == 201
        assert (
            await client.post(
                "/api/review-groups/web-group/followups",
                json=dict(hours=[], confirmed=True),
                headers=headers,
            )
        ).json() == []
        jobs = await client.post(
            "/api/review-groups/web-group/followups",
            json=dict(hours=[1, 24], confirmed=True),
            headers=headers,
        )
        assert jobs.status_code == 201 and len(jobs.json()) == 2
        canceled = await client.post(
            "/api/review-jobs/" + jobs.json()[0]["job_id"] + "/cancel",
            json=dict(expected_revision=1, confirmed=True),
            headers=headers,
        )
        assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_groups_and_versions_are_bounded_and_restart_discoverable(context):
    async with browser(context) as (client, headers, _):
        for identity in ("group-a", "group-b"):
            assert (
                await client.post(
                    "/api/review-groups",
                    json=dict(group_id=identity, cutoff=NOW.isoformat(), confirmed=True),
                    headers=headers,
                )
            ).status_code == 201
        first = (await client.get("/api/review-groups?limit=1")).json()
        assert len(first["groups"]) == 1 and first["has_more"] is True
        second = (
            await client.get(
                "/api/review-groups", params=dict(limit=1, after_sequence=first["next_sequence"])
            )
        ).json()
        assert second["groups"][0]["group_id"] != first["groups"][0]["group_id"]
        assert (await client.get("/api/review-groups?limit=51")).status_code == 422
        assert (
            await client.get("/api/review-groups/group-a/versions?trade_offset=4097")
        ).status_code == 422


def test_system_read_needs_cookie_and_preserves_explicit_disabled_state(tmp_path):
    with TestClient(create_app(tmp_path / "system.sqlite3"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/status").status_code == 403
        client.get("/status")
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "disabled" and data["jev_status"] == "unspecified"
        assert data["budget"]["daily_limit_usd"] == "0" and data["paid_models_enabled"] is False
        assert data["market"]["status"] == "not_connected"
        assert data["review_worker"]["running"] is True


@pytest.mark.asyncio
async def test_concurrent_group_retry_preserves_first_server_cutoff(context, monkeypatch):
    async with browser(context) as (client, headers, clock):
        original = client.review_service.create_group
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def racing(identity, cutoff):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return await original(identity, cutoff)

        monkeypatch.setattr(client.review_service, "create_group", racing)
        body = dict(group_id="same-group", confirmed=True)
        pending = asyncio.create_task(client.post("/api/review-groups", json=body, headers=headers))
        await started.wait()
        clock.advance_to(NOW + timedelta(seconds=1))
        winner = await client.post("/api/review-groups", json=body, headers=headers)
        release.set()
        retry = await pending
        assert winner.status_code == retry.status_code == 201
        assert winner.json() == retry.json()
        changed = await client.post(
            "/api/review-groups", json={**body, "cutoff": NOW.isoformat()}, headers=headers
        )
        assert changed.status_code == 409


@pytest.mark.asyncio
async def test_budget_read_failure_does_not_present_zero_or_exception_text(context, monkeypatch):
    from agent_platform.ports.sessions import PersistenceUnavailable

    async with browser(context) as (client, _, _):

        async def unavailable(*args, **kwargs):
            raise PersistenceUnavailable("private-error-never-display")

        monkeypatch.setattr(client.system_service.budgets, "budget_balance", unavailable)
        response = await client.get("/api/status")
        assert response.status_code == 200 and response.json()["budget"] is None
        assert response.json()["budget_status"] == "unavailable"
        assert "private-error-never-display" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [[True], ["1"], [12], [1, 1]])
async def test_followup_hours_require_unique_explicit_integers(context, hours):
    async with browser(context) as (client, headers, _):
        assert (
            await client.post(
                "/api/review-groups",
                json=dict(group_id="web-group", confirmed=True),
                headers=headers,
            )
        ).status_code == 201
        result = await client.post(
            "/api/review-groups/web-group/followups",
            json=dict(hours=hours, confirmed=True),
            headers=headers,
        )
        assert result.status_code == 422
