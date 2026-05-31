"""
Tests for dashboard/app.py.

Part 1 — pure helper functions (build_budget_report, pct_change):
  No HTTP, no mocking needed.

Part 2 — FastAPI HTTP endpoints via httpx.AsyncClient:
  sheets.py functions are monkey-patched so no Google Sheets calls occur.

conftest.py handles all heavy-dep stubs and loads the real sheets module.
"""
import sys
import os

import pytest
import pytest_asyncio

# ── import the dashboard app ──────────────────────────────────────────────────
_dashboard_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")
sys.path.insert(0, os.path.abspath(_dashboard_dir))

if "app" in sys.modules:
    del sys.modules["app"]

import app as dashboard_app  # noqa: E402
from app import build_budget_report, pct_change  # noqa: E402

# ── httpx async client ────────────────────────────────────────────────────────
from httpx import AsyncClient, BasicAuth  # noqa: E402
from httpx._transports.asgi import ASGITransport  # noqa: E402

GOOD_AUTH = BasicAuth("testuser", "testpass")
BAD_AUTH  = BasicAuth("wrong",    "creds")

BASE = "http://test"


def _transport():
    return ASGITransport(app=dashboard_app.app)


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 — pure helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildBudgetReport:
    def test_within_budget(self):
        result = build_budget_report({"식비": 100_000}, {"식비": 60_000})
        assert len(result) == 1
        row = result[0]
        assert row["category"] == "식비"
        assert row["budget"] == 100_000
        assert row["actual"] == 60_000
        assert row["percentage"] == 60.0
        assert row["over_budget"] is False

    def test_over_budget(self):
        result = build_budget_report({"식비": 50_000}, {"식비": 70_000})
        assert result[0]["over_budget"] is True
        assert result[0]["percentage"] == 140.0

    def test_zero_budget_gives_zero_percent(self):
        # budget=0 means unset; percentage is 0 but actual > 0 still flags over_budget
        result = build_budget_report({"식비": 0}, {"식비": 30_000})
        assert result[0]["percentage"] == 0
        assert result[0]["over_budget"] is True

    def test_unbudgeted_category_appended(self):
        result = build_budget_report({}, {"교통비": 15_000})
        assert result[0]["category"] == "교통비"
        assert result[0]["budget"] == 0
        assert result[0]["percentage"] == 100
        assert result[0]["over_budget"] is True

    def test_sorted_by_actual_descending(self):
        budgets = {"식비": 100_000, "교통비": 50_000, "통신비": 30_000}
        actuals = {"식비": 80_000,  "교통비": 10_000, "통신비": 25_000}
        result = build_budget_report(budgets, actuals)
        amounts = [r["actual"] for r in result]
        assert amounts == sorted(amounts, reverse=True)

    def test_empty_both_returns_empty(self):
        assert build_budget_report({}, {}) == []

    def test_budgeted_category_with_zero_actual(self):
        result = build_budget_report({"보험": 50_000}, {})
        assert result[0]["actual"] == 0
        assert result[0]["percentage"] == 0.0
        assert result[0]["over_budget"] is False

    def test_mixed_budgeted_and_unbudgeted(self):
        result = build_budget_report({"식비": 100_000}, {"식비": 60_000, "쇼핑": 40_000})
        categories = {r["category"] for r in result}
        assert categories == {"식비", "쇼핑"}
        shopping = next(r for r in result if r["category"] == "쇼핑")
        assert shopping["budget"] == 0
        assert shopping["over_budget"] is True

    def test_percentage_rounded_to_one_decimal(self):
        result = build_budget_report({"식비": 30_000}, {"식비": 10_000})
        assert result[0]["percentage"] == 33.3


class TestPctChange:
    def test_positive_change(self):
        assert pct_change(120, 100) == 20.0

    def test_negative_change(self):
        assert pct_change(80, 100) == -20.0

    def test_no_change(self):
        assert pct_change(100, 100) == 0.0

    def test_previous_zero_returns_none(self):
        assert pct_change(50, 0) is None

    def test_current_zero(self):
        assert pct_change(0, 100) == -100.0

    def test_rounded_to_one_decimal(self):
        assert pct_change(110, 30) == 266.7

    def test_negative_previous_value(self):
        # net can be negative (spending > income)
        assert pct_change(-80, -100) == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# Part 2 — HTTP endpoint tests
# ─────────────────────────────────────────────────────────────────────────────

# Sample records reused across endpoint tests
_SAMPLE_RECORDS = [
    {"type": "income",  "category": "급여",   "amount": 500_000, "display_name": "홍길동", "date": "2024-06-01"},
    {"type": "expense", "category": "식비",   "amount": 30_000,  "display_name": "홍길동", "date": "2024-06-02"},
    {"type": "expense", "category": "교통비", "amount": 15_000,  "display_name": "홍길동", "date": "2024-06-03"},
]

_SAMPLE_USERS = [
    {"user_id": "1", "display_name": "홍길동", "role": "admin"},
]


@pytest.fixture(autouse=True)
def patch_sheets(monkeypatch):
    """Replace all sheets.* calls with deterministic stubs for every test."""
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "get_records_for_month",  lambda *a, **kw: list(_SAMPLE_RECORDS))
    monkeypatch.setattr(_sheets, "get_all_users",          lambda *a, **kw: list(_SAMPLE_USERS))
    monkeypatch.setattr(_sheets, "get_all_budgets_for_month", lambda *a, **kw: {"식비": 100_000})
    monkeypatch.setattr(_sheets, "monthly_total",          _sheets.__dict__["monthly_total"])
    monkeypatch.setattr(_sheets, "monthly_breakdown",      _sheets.__dict__["monthly_breakdown"])
    monkeypatch.setattr(_sheets, "breakdown_by_user",      _sheets.__dict__["breakdown_by_user"])

    # Clear TTL caches between tests so cached responses don't leak
    dashboard_app._dash_cache.clear()
    dashboard_app._annual_cache.clear()
    dashboard_app._trend_cache.clear()


# ── authentication ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_no_credentials_returns_401():
    async with AsyncClient(transport=_transport(), base_url=BASE) as client:
        r = await client.get("/api/summary")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_wrong_credentials_returns_401():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=BAD_AUTH) as client:
        r = await client.get("/api/summary")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_endpoint_no_auth_required():
    async with AsyncClient(transport=_transport(), base_url=BASE) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── /api/summary ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_response_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/summary", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    body = r.json()
    for key in ("year", "month", "income", "expense", "net", "transaction_count"):
        assert key in body, f"missing key: {key}"


@pytest.mark.asyncio
async def test_summary_correct_values():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/summary", params={"year": 2024, "month": 6})
    body = r.json()
    assert body["income"]  == 500_000
    assert body["expense"] == 45_000
    assert body["net"]     == 455_000
    assert body["transaction_count"] == 3


# ── /api/breakdown ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_breakdown_expense_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/breakdown", params={"year": 2024, "month": 6, "record_type": "expense"})
    assert r.status_code == 200
    body = r.json()
    assert "breakdown" in body
    assert "type" in body
    assert body["type"] == "expense"


@pytest.mark.asyncio
async def test_breakdown_expense_values():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/breakdown", params={"year": 2024, "month": 6, "record_type": "expense"})
    breakdown = r.json()["breakdown"]
    assert breakdown.get("식비")   == 30_000
    assert breakdown.get("교통비") == 15_000


# ── /api/budgets ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_budgets_response_is_list():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_budgets_row_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    for row in r.json():
        for key in ("category", "budget", "actual", "percentage", "over_budget"):
            assert key in row, f"missing key '{key}' in budget row"


@pytest.mark.asyncio
async def test_budgets_식비_within_budget():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    rows = {row["category"]: row for row in r.json()}
    assert "식비" in rows
    assert rows["식비"]["budget"]     == 100_000
    assert rows["식비"]["actual"]     == 30_000
    assert rows["식비"]["over_budget"] is False


@pytest.mark.asyncio
async def test_budgets_unbudgeted_category_included():
    # 교통비 has no budget set but has actual spend → must appear as over_budget
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    rows = {row["category"]: row for row in r.json()}
    assert "교통비" in rows
    assert rows["교통비"]["budget"]     == 0
    assert rows["교통비"]["over_budget"] is True


# ── /api/members ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_members_response_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/members", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    body = r.json()
    assert "홍길동" in body
    assert "income"  in body["홍길동"]
    assert "expense" in body["홍길동"]


@pytest.mark.asyncio
async def test_members_correct_amounts():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/members", params={"year": 2024, "month": 6})
    member = r.json()["홍길동"]
    assert member["income"]  == 500_000
    assert member["expense"] == 45_000


# ── /api/transactions ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transactions_is_list():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_transactions_sorted_by_date_descending():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6})
    dates = [rec["date"] for rec in r.json()]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_transactions_limit_param(monkeypatch):
    import sheets as _sheets
    big_list = _SAMPLE_RECORDS * 10   # 30 records
    monkeypatch.setattr(_sheets, "get_records_for_month", lambda *a, **kw: list(big_list))
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6, "limit": 5})
    assert len(r.json()) == 5


# ── /api/cache/clear ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_cache_returns_ok():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.post("/api/cache/clear", headers={"X-Dashboard-Clear": "1"})
    assert r.status_code == 200
    assert r.json()["status"] == "cleared"


@pytest.mark.asyncio
async def test_clear_cache_requires_csrf_header():
    async with AsyncClient(transport=_transport(), base_url=BASE, auth=GOOD_AUTH) as client:
        r = await client.post("/api/cache/clear")
    assert r.status_code == 400
