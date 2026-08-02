"""
sheets.py 쓰기 경로(CRUD) 테스트.

이 함수들은 실제 가계부 데이터를 바꾸므로 조용한 버그의 대가가 큽니다.
특히 검증하는 것:
  - 소유권: 다른 가족 구성원의 기록을 수정·삭제할 수 없는가
  - 캐시 무효화: 쓰기 후 낡은 값이 남지 않는가
  - 재시도: Sheets 가 429/503 을 줄 때 포기하지 않는가
"""
import pytest

import sheets

from tests.test_sheets_cache import FakeWorksheet, _rec


@pytest.fixture(autouse=True)
def clean_caches():
    sheets._invalidate_records_cache()
    sheets._invalidate_users_cache()
    sheets._invalidate_budgets_cache()
    yield
    sheets._invalidate_records_cache()
    sheets._invalidate_users_cache()
    sheets._invalidate_budgets_cache()


class RecordingWorksheet(FakeWorksheet):
    """append_row / col_values / add_rows 까지 기록하는 확장 가짜 시트."""

    def __init__(self, rows):
        super().__init__(rows)
        self.appended = []
        self.added_rows = 0
        self.cleared = False

    def append_row(self, values, value_input_option=None):
        self.appended.append(values)

    def col_values(self, col):
        return ["header"] + [r.get("id", "") for r in self.rows]

    def add_rows(self, n):
        self.added_rows += n
        self.row_count += n

    def row_values(self, n):
        return list(sheets.RECORDS_HEADERS)

    def clear(self):
        self.cleared = True


@pytest.fixture
def ws(monkeypatch):
    sheet = RecordingWorksheet([
        _rec("REC00001", "2026-01-05 10:00", 10_000, user_id="1"),
        _rec("REC00002", "2026-01-06 10:00", 20_000, user_id="2", name="엄마"),
    ])
    monkeypatch.setattr(sheets, "get_sheet", lambda name: sheet)
    return sheet


# ─────────────────────────────────────────────────────────────────────────────
# 소유권 — 남의 기록을 건드릴 수 없어야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestOwnership:
    def test_cannot_delete_another_members_record(self, ws):
        """유저 1이 유저 2의 기록을 지우려 하면 실패해야 한다."""
        assert sheets.delete_record(1, "REC00002") is False
        assert ws.deleted_rows == []

    def test_can_delete_own_record(self, ws):
        assert sheets.delete_record(1, "REC00001") is True
        assert ws.deleted_rows == [2]          # 헤더 다음 첫 행

    def test_cannot_update_another_members_record(self, ws):
        assert sheets.update_record(1, "REC00002", "amount", "999") is False
        assert ws.cell_calls == []

    def test_can_update_own_record(self, ws):
        assert sheets.update_record(1, "REC00001", "amount", "999") is True
        assert ws.cell_calls == [(2, sheets.EDITABLE_RECORD_FIELDS["amount"], "999")]

    def test_update_rejects_non_editable_field(self, ws):
        assert sheets.update_record(1, "REC00001", "user_id", "9") is False
        assert ws.cell_calls == []

    def test_admin_delete_ignores_ownership(self, ws):
        """관리자용 경로는 소유자와 무관하게 지운다 (대시보드 전용)."""
        assert sheets.delete_record_by_id("REC00002") is True
        assert ws.deleted_rows == [3]

    def test_delete_missing_record_returns_false(self, ws):
        assert sheets.delete_record(1, "NOPE0000") is False
        assert sheets.delete_record_by_id("NOPE0000") is False


# ─────────────────────────────────────────────────────────────────────────────
# 기록 추가
# ─────────────────────────────────────────────────────────────────────────────
class TestInsertRecord:
    def test_appends_row_in_header_order(self, ws):
        from datetime import datetime
        rec_id = sheets.insert_record(
            7, "첫째", "expense", "식비", 12_345, "점심",
            datetime(2026, 3, 4, 13, 5),
        )
        assert len(ws.appended) == 1
        row = ws.appended[0]
        assert row[sheets.RECORDS_HEADERS.index("id")] == rec_id
        assert row[sheets.RECORDS_HEADERS.index("user_id")] == "7"
        assert row[sheets.RECORDS_HEADERS.index("display_name")] == "첫째"
        assert row[sheets.RECORDS_HEADERS.index("type")] == "expense"
        assert row[sheets.RECORDS_HEADERS.index("category")] == "식비"
        assert row[sheets.RECORDS_HEADERS.index("amount")] == 12_345
        assert row[sheets.RECORDS_HEADERS.index("memo")] == "점심"
        assert row[sheets.RECORDS_HEADERS.index("date")] == "2026-03-04 13:05"

    def test_generates_unique_ids(self, ws):
        from datetime import datetime
        dt = datetime(2026, 3, 4, 13, 5)
        ids = {sheets.insert_record(1, "n", "expense", "식비", 1, "", dt) for _ in range(30)}
        assert len(ids) == 30
        # 대시보드가 ^[A-Fa-f0-9]{8}$ 로 검증하므로 그 형식을 지켜야 한다
        assert all(len(i) == 8 and set(i) <= set("0123456789ABCDEF") for i in ids)

    def test_invalidates_month_cache(self, ws):
        from datetime import datetime
        sheets.get_records_for_month(2026, 1)
        before = ws.fetch_count
        sheets.insert_record(1, "n", "expense", "식비", 1, "", datetime(2026, 1, 9, 9, 0))
        sheets.get_records_for_month(2026, 1)
        assert ws.fetch_count > before, "추가 후에도 낡은 캐시를 반환했습니다"


# ─────────────────────────────────────────────────────────────────────────────
# 시트 자동 확장
# ─────────────────────────────────────────────────────────────────────────────
class TestSheetExpansion:
    def test_expands_when_nearly_full(self, ws, monkeypatch):
        monkeypatch.setattr(sheets, "_insert_call_count", sheets._EXPAND_CHECK_EVERY - 1)
        ws.row_count = 3                      # 사용 3행 / 총 3행 → 여유 0
        sheets._maybe_expand_sheet(ws)
        assert ws.added_rows == sheets._EXPAND_ADD_ROWS

    def test_does_not_expand_when_roomy(self, ws, monkeypatch):
        monkeypatch.setattr(sheets, "_insert_call_count", sheets._EXPAND_CHECK_EVERY - 1)
        ws.row_count = 5000
        sheets._maybe_expand_sheet(ws)
        assert ws.added_rows == 0

    def test_only_checks_periodically(self, ws, monkeypatch):
        monkeypatch.setattr(sheets, "_insert_call_count", 0)
        ws.row_count = 3
        sheets._maybe_expand_sheet(ws)         # 1회차 — 확인 안 함
        assert ws.added_rows == 0

    def test_expansion_failure_is_swallowed(self, ws, monkeypatch):
        """용량 확인이 실패해도 기록 추가 자체를 막으면 안 된다."""
        monkeypatch.setattr(sheets, "_insert_call_count", sheets._EXPAND_CHECK_EVERY - 1)
        def boom(col):
            raise RuntimeError("API down")
        ws.col_values = boom
        sheets._maybe_expand_sheet(ws)         # 예외가 밖으로 새면 실패
        assert ws.added_rows == 0


# ─────────────────────────────────────────────────────────────────────────────
# 유저 관리
# ─────────────────────────────────────────────────────────────────────────────
class TestUsers:
    @pytest.fixture
    def users_ws(self, monkeypatch):
        sheet = RecordingWorksheet([
            {"user_id": "1", "display_name": "아빠", "role": "admin",   "joined_at": ""},
            {"user_id": "2", "display_name": "엄마", "role": "member",  "joined_at": ""},
            {"user_id": "3", "display_name": "손님", "role": "pending", "joined_at": ""},
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: sheet)
        return sheet

    def test_find_user(self, users_ws):
        assert sheets.find_user(2)["display_name"] == "엄마"
        assert sheets.find_user(1)["display_name"] == "아빠"
        assert sheets.find_user(99) is None

    def test_find_user_accepts_string_or_int(self, users_ws):
        assert sheets.find_user("2")["display_name"] == "엄마"

    def test_roles(self, users_ws):
        assert sheets.is_admin(1) is True
        assert sheets.is_admin(2) is False
        assert sheets.is_approved(1) is True
        assert sheets.is_approved(2) is True
        assert sheets.is_approved(3) is False     # 승인 대기중
        assert sheets.is_approved(99) is False    # 미등록

    def test_register_user_appends_and_invalidates(self, users_ws):
        sheets.get_all_users()
        sheets.register_user(9, "막내")
        assert len(users_ws.appended) == 1
        assert users_ws.appended[0][:3] == ["9", "막내", "member"]
        users_ws.rows.append({"user_id": "9", "display_name": "막내",
                              "role": "member", "joined_at": ""})
        assert sheets.find_user(9) is not None, "등록 후 캐시가 갱신되지 않았습니다"

    def test_set_user_role(self, users_ws):
        assert sheets.set_user_role(3, "member") is True
        row, col, val = users_ws.cell_calls[0]
        assert row == 4                                    # 3번째 데이터 행
        assert col == sheets.USERS_HEADERS.index("role") + 1
        assert val == "member"

    def test_update_display_name(self, users_ws):
        assert sheets.update_display_name(2, "어머니") is True
        _, col, val = users_ws.cell_calls[0]
        assert col == sheets.USERS_HEADERS.index("display_name") + 1
        assert val == "어머니"

    def test_update_unknown_user_returns_false(self, users_ws):
        assert sheets.set_user_role(99, "admin") is False
        assert users_ws.cell_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# 예산 쓰기
# ─────────────────────────────────────────────────────────────────────────────
class TestBudgetWrites:
    @pytest.fixture
    def budget_ws(self, monkeypatch):
        sheet = RecordingWorksheet([
            {"user_id": "1", "display_name": "아빠", "category": "식비",
             "amount": 300000, "year": 2026, "month": 1},
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: sheet)
        return sheet

    def test_existing_budget_is_updated_not_duplicated(self, budget_ws):
        sheets.set_budget(1, "아빠", "식비", 400000, 2026, 1)
        assert budget_ws.appended == [], "기존 예산을 새 행으로 중복 추가했습니다"
        row, col, val = budget_ws.cell_calls[0]
        assert row == 2
        assert col == sheets.BUDGETS_HEADERS.index("amount") + 1
        assert val == 400000

    def test_new_budget_is_appended(self, budget_ws):
        sheets.set_budget(1, "아빠", "쇼핑", 150000, 2026, 1)
        assert budget_ws.cell_calls == []
        assert budget_ws.appended[0] == ["1", "아빠", "쇼핑", 150000, 2026, 1]

    def test_same_category_different_month_is_new_row(self, budget_ws):
        sheets.set_budget(1, "아빠", "식비", 350000, 2026, 2)
        assert len(budget_ws.appended) == 1

    def test_get_budget_single_category(self, budget_ws):
        assert sheets.get_budget(1, "식비", 2026, 1) == 300000.0
        assert sheets.get_budget(1, "없는카테고리", 2026, 1) is None

    def test_copy_budgets_skips_existing(self, budget_ws):
        budget_ws.rows.extend([
            {"user_id": "1", "display_name": "아빠", "category": "쇼핑",
             "amount": 100000, "year": 2026, "month": 1},
            {"user_id": "1", "display_name": "아빠", "category": "식비",
             "amount": 999, "year": 2026, "month": 2},      # 2월에 이미 있음
        ])
        copied, skipped = sheets.copy_budgets_from_month(1, "아빠", 2026, 1, 2026, 2)
        assert skipped == ["식비"]
        assert copied == ["쇼핑"]
        assert len(budget_ws.appended) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 조회 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
class TestQueryHelpers:
    @pytest.fixture
    def many(self, monkeypatch):
        rows = [
            _rec(f"R{i:07d}", f"2026-01-{(i % 28) + 1:02d} 10:00", 1000 * i,
                 user_id="1" if i % 2 else "2",
                 category="식비" if i % 3 else "쇼핑")
            for i in range(1, 21)
        ]
        rows[0]["memo"] = "생일선물"
        sheet = RecordingWorksheet(rows)
        monkeypatch.setattr(sheets, "get_sheet", lambda name: sheet)
        return sheet

    def test_recent_records_newest_first(self, many):
        got = sheets.get_recent_records(limit=3)
        assert [r["id"] for r in got] == ["R0000020", "R0000019", "R0000018"]

    def test_recent_records_filtered_by_user(self, many):
        got = sheets.get_recent_records(user_id=1, limit=3)
        assert all(r["user_id"] == "1" for r in got)

    def test_all_records_for_user(self, many):
        got = sheets.get_all_records_for_user(2)
        assert got and all(r["user_id"] == "2" for r in got)

    def test_search_by_memo(self, many):
        got = sheets.search_records(1, "생일")
        assert [r["id"] for r in got] == ["R0000001"]

    def test_search_by_category_case_insensitive(self, many):
        got = sheets.search_records(1, "쇼핑")
        assert got and all(r["category"] == "쇼핑" for r in got)

    def test_search_does_not_leak_other_users(self, many):
        got = sheets.search_records(1, "식비")
        assert all(r["user_id"] == "1" for r in got)

    def test_search_no_match(self, many):
        assert sheets.search_records(1, "존재하지않는키워드") == []


# ─────────────────────────────────────────────────────────────────────────────
# API 재시도 — 일시적 오류에 포기하지 않아야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestRetry:
    def _api_error(self, code):
        from gspread.exceptions import APIError
        err = APIError("boom")
        err.response = type("R", (), {"status_code": code})()
        return err

    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(sheets.time, "sleep", lambda s: None)

    @pytest.mark.parametrize("code", [429, 500, 503])
    def test_retries_transient_errors(self, code):
        calls = {"n": 0}

        class Flaky:
            def get_all_records(_self):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise self._api_error(code)
                return [{"ok": 1}]

        assert sheets._safe_get_records(Flaky()) == [{"ok": 1}]
        assert calls["n"] == 3

    def test_gives_up_after_three_attempts(self):
        calls = {"n": 0}
        err = self._api_error(429)

        class AlwaysDown:
            def get_all_records(_self):
                calls["n"] += 1
                raise err

        from gspread.exceptions import APIError
        with pytest.raises(APIError):
            sheets._safe_get_records(AlwaysDown())
        assert calls["n"] == 3, "재시도 횟수가 3회가 아닙니다"

    def test_does_not_retry_permanent_errors(self):
        """403(권한 없음) 같은 오류는 재시도해도 소용없으니 즉시 올린다."""
        calls = {"n": 0}
        err = self._api_error(403)

        class Forbidden:
            def get_all_records(_self):
                calls["n"] += 1
                raise err

        from gspread.exceptions import APIError
        with pytest.raises(APIError):
            sheets._safe_get_records(Forbidden())
        assert calls["n"] == 1
