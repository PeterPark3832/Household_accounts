"""
Tests for dashboard/app.py.

Part 1 — pure helper functions (build_budget_report, pct_change):
  No HTTP, no mocking needed.

Part 2 — FastAPI HTTP endpoints via httpx.AsyncClient:
  sheets.py functions are monkey-patched so no Google Sheets calls occur.
  Auth uses Bearer token injected directly into dashboard_app._tokens.

conftest.py handles all heavy-dep stubs and loads the real sheets module.
"""
import sys
import os
import time as _time

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
from httpx import AsyncClient  # noqa: E402
from httpx._transports.asgi import ASGITransport  # noqa: E402

_GOOD_TOKEN = "test_good_token_0123456789abcdef"
GOOD_HDRS   = {"Authorization": f"Bearer {_GOOD_TOKEN}"}

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
    monkeypatch.setattr(
        _sheets, "get_records_for_months",
        lambda months, user_id=None: {k: list(_SAMPLE_RECORDS) for k in months},
    )
    monkeypatch.setattr(_sheets, "get_all_users",          lambda *a, **kw: list(_SAMPLE_USERS))
    monkeypatch.setattr(_sheets, "get_all_budgets_for_month", lambda *a, **kw: {"식비": 100_000})
    monkeypatch.setattr(_sheets, "monthly_total",          _sheets.__dict__["monthly_total"])
    monkeypatch.setattr(_sheets, "monthly_breakdown",      _sheets.__dict__["monthly_breakdown"])
    monkeypatch.setattr(_sheets, "breakdown_by_user",      _sheets.__dict__["breakdown_by_user"])

    # Inject a valid Bearer token for the duration of each test
    dashboard_app._tokens[_GOOD_TOKEN] = _time.time() + 3600

    # Clear TTL caches between tests so cached responses don't leak
    dashboard_app._dash_cache.clear()
    dashboard_app._annual_cache.clear()
    dashboard_app._trend_cache.clear()

    yield

    dashboard_app._tokens.pop(_GOOD_TOKEN, None)


# ── authentication ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_no_credentials_returns_401():
    async with AsyncClient(transport=_transport(), base_url=BASE) as client:
        r = await client.get("/api/summary")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_auth_wrong_token_returns_401():
    async with AsyncClient(transport=_transport(), base_url=BASE,
                           headers={"Authorization": "Bearer wrong_token"}) as client:
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
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/summary", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    body = r.json()
    for key in ("year", "month", "income", "expense", "net", "transaction_count"):
        assert key in body, f"missing key: {key}"


@pytest.mark.asyncio
async def test_summary_correct_values():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/summary", params={"year": 2024, "month": 6})
    body = r.json()
    assert body["income"]  == 500_000
    assert body["expense"] == 45_000
    assert body["net"]     == 455_000
    assert body["transaction_count"] == 3


# ── /api/breakdown ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_breakdown_expense_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/breakdown", params={"year": 2024, "month": 6, "record_type": "expense"})
    assert r.status_code == 200
    body = r.json()
    assert "breakdown" in body
    assert "type" in body
    assert body["type"] == "expense"


@pytest.mark.asyncio
async def test_breakdown_expense_values():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/breakdown", params={"year": 2024, "month": 6, "record_type": "expense"})
    breakdown = r.json()["breakdown"]
    assert breakdown.get("식비")   == 30_000
    assert breakdown.get("교통비") == 15_000


# ── /api/budgets ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_budgets_response_is_list():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_budgets_row_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    for row in r.json():
        for key in ("category", "budget", "actual", "percentage", "over_budget"):
            assert key in row, f"missing key '{key}' in budget row"


@pytest.mark.asyncio
async def test_budgets_식비_within_budget():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    rows = {row["category"]: row for row in r.json()}
    assert "식비" in rows
    assert rows["식비"]["budget"]     == 100_000
    assert rows["식비"]["actual"]     == 30_000
    assert rows["식비"]["over_budget"] is False


@pytest.mark.asyncio
async def test_budgets_unbudgeted_category_included():
    # 교통비 has no budget set but has actual spend → must appear as over_budget
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/budgets", params={"year": 2024, "month": 6})
    rows = {row["category"]: row for row in r.json()}
    assert "교통비" in rows
    assert rows["교통비"]["budget"]     == 0
    assert rows["교통비"]["over_budget"] is True


# ── /api/members ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_members_response_shape():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/members", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    body = r.json()
    assert "홍길동" in body
    assert "income"  in body["홍길동"]
    assert "expense" in body["홍길동"]


@pytest.mark.asyncio
async def test_members_correct_amounts():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/members", params={"year": 2024, "month": 6})
    member = r.json()["홍길동"]
    assert member["income"]  == 500_000
    assert member["expense"] == 45_000


# ── /api/transactions ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transactions_is_list():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_transactions_sorted_by_date_descending():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6})
    dates = [rec["date"] for rec in r.json()]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_transactions_limit_param(monkeypatch):
    import sheets as _sheets
    big_list = _SAMPLE_RECORDS * 10   # 30 records
    monkeypatch.setattr(_sheets, "get_records_for_month", lambda *a, **kw: list(big_list))
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6, "limit": 5})
    assert len(r.json()) == 5


# ── /api/cache/clear ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_cache_returns_ok():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.post("/api/cache/clear",
                              headers={**GOOD_HDRS, "X-Dashboard-Clear": "1"})
    assert r.status_code == 200
    assert r.json()["status"] == "cleared"


@pytest.mark.asyncio
async def test_clear_cache_requires_csrf_header():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.post("/api/cache/clear")
    assert r.status_code == 400


# ── DELETE /api/transactions/{rec_id} ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_transaction_success(monkeypatch):
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "delete_record_by_id", lambda rec_id: True)
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.delete("/api/transactions/AABBCCDD")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "deleted"
    assert body["id"] == "AABBCCDD"


@pytest.mark.asyncio
async def test_delete_transaction_not_found(monkeypatch):
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "delete_record_by_id", lambda rec_id: False)
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.delete("/api/transactions/AABBCCDD")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_transaction_invalid_id():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.delete("/api/transactions/INVALID!!")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_transaction_requires_auth():
    async with AsyncClient(transport=_transport(), base_url=BASE) as client:
        r = await client.delete("/api/transactions/AABBCCDD")
    assert r.status_code == 401


# ── PATCH /api/transactions/{rec_id} ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_transaction_success(monkeypatch):
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "update_record_by_id", lambda rec_id, fields: True)
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.patch(
            "/api/transactions/AABBCCDD",
            json={"category": "식비", "amount": 50000, "memo": "테스트"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "updated"
    assert body["id"] == "AABBCCDD"


@pytest.mark.asyncio
async def test_update_transaction_not_found(monkeypatch):
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "update_record_by_id", lambda rec_id, fields: False)
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.patch("/api/transactions/AABBCCDD", json={"memo": "변경"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_transaction_empty_body():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.patch("/api/transactions/AABBCCDD", json={})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_transaction_invalid_id():
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.patch("/api/transactions/TOOSHORT", json={"memo": "변경"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_transaction_requires_auth():
    async with AsyncClient(transport=_transport(), base_url=BASE) as client:
        r = await client.patch("/api/transactions/AABBCCDD", json={"memo": "변경"})
    assert r.status_code == 401


# ── 연간/트렌드: 월별 조회가 일괄 1회로 묶였는지 ───────────────────────────────

@pytest.mark.asyncio
async def test_annual_uses_single_grouped_fetch(monkeypatch):
    """연간 요약이 월별 개별 조회가 아니라 일괄 조회 1회를 써야 한다."""
    import sheets as _sheets
    calls = {"grouped": 0, "per_month": 0}

    def _grouped(months, user_id=None):
        calls["grouped"] += 1
        return {k: list(_SAMPLE_RECORDS) for k in months}

    def _per_month(*a, **kw):
        calls["per_month"] += 1
        return list(_SAMPLE_RECORDS)

    monkeypatch.setattr(_sheets, "get_records_for_months", _grouped)
    monkeypatch.setattr(_sheets, "get_records_for_month", _per_month)

    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/annual", params={"year": 2024})

    assert r.status_code == 200
    assert len(r.json()) == 12
    assert calls["grouped"] == 1
    assert calls["per_month"] == 0, "연간 요약이 아직 월별로 개별 조회하고 있습니다"


@pytest.mark.asyncio
async def test_annual_values(monkeypatch):
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/annual", params={"year": 2024})
    body = r.json()
    assert body[0]["month"] == 1 and body[0]["label"] == "1월"
    assert body[0]["income"] == 500_000
    assert body[0]["expense"] == 45_000
    assert body[0]["net"] == 455_000


@pytest.mark.asyncio
async def test_trend_uses_single_grouped_fetch(monkeypatch):
    import sheets as _sheets
    calls = {"grouped": 0, "per_month": 0}
    monkeypatch.setattr(_sheets, "get_records_for_months",
                        lambda months, user_id=None: (calls.__setitem__("grouped", calls["grouped"] + 1),
                                                      {k: list(_SAMPLE_RECORDS) for k in months})[1])
    monkeypatch.setattr(_sheets, "get_records_for_month",
                        lambda *a, **kw: (calls.__setitem__("per_month", calls["per_month"] + 1),
                                          list(_SAMPLE_RECORDS))[1])

    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/trend", params={"months": 6})

    assert r.status_code == 200
    assert len(r.json()) == 6
    assert calls["grouped"] == 1
    assert calls["per_month"] == 0


# ── 시트에 이상 데이터가 있어도 대시보드가 죽지 않아야 한다 ────────────────────

_DIRTY_RECORDS = [
    {"type": "income",  "category": "급여", "amount": 500_000, "display_name": "홍길동", "date": "2024-06-01"},
    {"type": "expense", "category": "식비", "amount": "",       "display_name": "홍길동", "date": "2024-06-02"},
    {"type": "expense", "category": "쇼핑", "amount": "삼만원",  "display_name": "홍길동", "date": "2024-06-03"},
    {"type": "expense", "category": "교통비", "amount": "15,000", "display_name": "홍길동", "date": "2024-06-04"},
]


@pytest.mark.asyncio
async def test_summary_survives_malformed_amounts(monkeypatch):
    """금액 칸이 비었거나 문자인 행이 있어도 500이 나면 안 된다."""
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "get_records_for_month", lambda *a, **kw: list(_DIRTY_RECORDS))
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/summary", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    body = r.json()
    assert body["income"] == 500_000
    assert body["expense"] == 15_000        # 빈칸·문자는 0 으로 처리
    assert body["transaction_count"] == 4


@pytest.mark.asyncio
async def test_dashboard_survives_malformed_amounts(monkeypatch):
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "get_records_for_month", lambda *a, **kw: list(_DIRTY_RECORDS))
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/dashboard", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert r.json()["summary"]["expense"] == 15_000


@pytest.mark.asyncio
async def test_dashboard_survives_non_numeric_user_id(monkeypatch):
    """users 시트의 user_id 가 숫자가 아니어도 대시보드는 떠야 한다."""
    import sheets as _sheets
    monkeypatch.setattr(_sheets, "get_all_users",
                        lambda *a, **kw: [{"user_id": "관리자", "display_name": "홍길동", "role": "admin"}])
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/dashboard", params={"year": 2024, "month": 6})
    assert r.status_code == 200
    assert r.json()["summary"]["income"] == 500_000


# ── 입력 검증 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_budgets_rejects_non_numeric_user_id():
    """잘못된 user_id 는 500(서버 오류)이 아니라 400(잘못된 요청)이어야 한다."""
    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/budgets", params={"user_id": "abc", "year": 2024, "month": 6})
    assert r.status_code == 400


# ── /api/transactions 가 공유 캐시를 변형하지 않아야 한다 ──────────────────────

@pytest.mark.asyncio
async def test_transactions_does_not_mutate_shared_records(monkeypatch):
    import sheets as _sheets
    shared = list(_SAMPLE_RECORDS)          # 캐시가 돌려주는 바로 그 리스트를 흉내
    original = [r["date"] for r in shared]
    monkeypatch.setattr(_sheets, "get_records_for_month", lambda *a, **kw: shared)

    async with AsyncClient(transport=_transport(), base_url=BASE, headers=GOOD_HDRS) as client:
        r = await client.get("/api/transactions", params={"year": 2024, "month": 6})

    assert r.status_code == 200
    assert [rec["date"] for rec in r.json()] == sorted(original, reverse=True)
    assert [rec["date"] for rec in shared] == original, "공유 리스트가 정렬돼 버렸습니다"


# ── 인증 실패 로그가 무한히 쌓이지 않아야 한다 ────────────────────────────────

@pytest.mark.asyncio
async def test_fail_log_does_not_grow_unbounded():
    """IP 를 바꿔가며 실패해도 추적 항목 수에 상한이 있어야 한다."""
    cap = dashboard_app._AUTH_MAX_TRACKED_IPS
    dashboard_app._fail_log.clear()
    stale = _time.time() - dashboard_app._AUTH_WINDOW_SEC - 10
    for i in range(cap + 500):
        dashboard_app._fail_log[f"10.0.{i // 256}.{i % 256}"].append(stale)
    dashboard_app._record_fail("10.9.9.9")          # 정리 트리거
    assert len(dashboard_app._fail_log) <= cap
    dashboard_app._fail_log.clear()


@pytest.mark.asyncio
async def test_expired_ip_entry_is_dropped():
    dashboard_app._fail_log.clear()
    dashboard_app._fail_log["1.2.3.4"].append(
        _time.time() - dashboard_app._AUTH_WINDOW_SEC - 5
    )
    assert dashboard_app._is_rate_limited("1.2.3.4") is False
    assert "1.2.3.4" not in dashboard_app._fail_log
    dashboard_app._fail_log.clear()
