"""
sheets.py 의 I/O·캐시 계층 테스트.

여기서 검증하는 것:
  1. to_number()  — 사람이 편집한 시트의 이상한 셀이 전체를 깨뜨리지 않는가
  2. 월별 일괄 조회 — 12개월을 요청해도 시트 조회가 '1회' 인가 (성능 회귀 방지)
  3. 캐시 격리    — 호출자가 결과를 변형해도 캐시가 오염되지 않는가
  4. 배치 수정    — 여러 필드 수정이 API 호출 1회로 묶이는가

conftest.py 가 gspread 를 스텁하므로 실제 네트워크는 발생하지 않습니다.
"""
import time

import pytest

import sheets


# ── 테스트용 가짜 워크시트 ────────────────────────────────────────────────────
class FakeWorksheet:
    """get_all_records / batch_update / update_cell 호출을 기록하는 가짜 시트."""

    def __init__(self, rows):
        self.rows = rows
        self.fetch_count = 0
        self.batch_calls = []
        self.cell_calls = []
        self.deleted_rows = []
        self.title = "records"
        self.row_count = 1000

    def get_all_records(self):
        self.fetch_count += 1
        return [dict(r) for r in self.rows]

    def batch_update(self, data, value_input_option=None):
        self.batch_calls.append((data, value_input_option))

    def update_cell(self, row, col, value):
        self.cell_calls.append((row, col, value))

    def delete_rows(self, i):
        self.deleted_rows.append(i)


def _rec(rec_id, date, amount, rtype="expense", user_id="1", category="식비", name="아빠"):
    return {
        "id": rec_id, "user_id": user_id, "display_name": name,
        "type": rtype, "category": category, "amount": amount,
        "memo": "", "date": date,
    }


@pytest.fixture(autouse=True)
def clean_caches():
    """각 테스트가 깨끗한 캐시 상태에서 시작하도록 보장."""
    sheets._invalidate_records_cache()
    sheets._invalidate_users_cache()
    sheets._invalidate_budgets_cache()
    yield
    sheets._invalidate_records_cache()
    sheets._invalidate_users_cache()
    sheets._invalidate_budgets_cache()


@pytest.fixture
def sheet(monkeypatch):
    """sheets.get_sheet 를 가짜 워크시트로 교체하고 그 인스턴스를 돌려준다."""
    ws = FakeWorksheet([
        _rec("AAAA0001", "2026-01-05 10:00", 10_000),
        _rec("AAAA0002", "2026-01-20 10:00", 20_000),
        _rec("AAAA0003", "2026-02-11 10:00", 30_000),
        _rec("AAAA0004", "2026-03-02 10:00", 5_000, rtype="income"),
        _rec("AAAA0005", "2026-12-31 10:00", 7_000, user_id="2", name="엄마"),
    ])
    monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# 1. to_number — 사람이 편집한 시트에 대한 내성
# ─────────────────────────────────────────────────────────────────────────────
class TestToNumber:
    @pytest.mark.parametrize("raw,expected", [
        (1000, 1000.0),
        (1234.5, 1234.5),
        ("1000", 1000.0),
        ("1,234,000", 1_234_000.0),      # 천 단위 콤마
        ("₩5,000", 5000.0),              # 통화 기호
        ("3000원", 3000.0),               # 한글 단위
        ("  42  ", 42.0),                # 공백
        ("", 0.0),                       # 빈 칸
        (None, 0.0),
        ("삼만원", 0.0),                  # 숫자로 볼 수 없음 → 0
        ("N/A", 0.0),
        (True, 0.0),                     # bool 은 금액이 아님
    ])
    def test_coercion(self, raw, expected):
        assert sheets.to_number(raw) == expected

    def test_bad_row_does_not_break_totals(self):
        """금액 칸이 깨진 행이 있어도 나머지 합계는 정상이어야 한다."""
        records = [
            _rec("A", "2026-01-01 09:00", 10_000),
            _rec("B", "2026-01-02 09:00", ""),        # 빈 칸
            _rec("C", "2026-01-03 09:00", "삼만원"),   # 문자
            _rec("D", "2026-01-04 09:00", "20,000"),  # 콤마
        ]
        assert sheets.monthly_total(records, "expense") == 30_000.0
        assert sheets.monthly_breakdown(records, "expense") == {"식비": 30_000.0}
        assert sheets.breakdown_by_user(records, "expense") == {"아빠": 30_000.0}

    def test_missing_category_and_name_are_labelled(self):
        records = [{"type": "expense", "amount": 100, "category": "", "display_name": ""}]
        assert sheets.monthly_breakdown(records, "expense") == {"미분류": 100.0}
        assert sheets.breakdown_by_user(records, "expense") == {"이름없음": 100.0}


# ─────────────────────────────────────────────────────────────────────────────
# 2. 월별 일괄 조회 — 성능 회귀 방지의 핵심
# ─────────────────────────────────────────────────────────────────────────────
class TestGroupedMonthFetch:
    def test_twelve_months_costs_one_fetch(self, sheet):
        """연간 요약(12개월)이 시트 조회 1회로 끝나야 한다. (기존 12회)"""
        months = [(2026, m) for m in range(1, 13)]
        grouped = sheets.get_records_for_months(months)
        assert sheet.fetch_count == 1, "12개월 요청에 시트를 두 번 이상 읽었습니다"
        assert len(grouped) == 12                     # 빈 달도 키가 있어야 함
        assert len(grouped[(2026, 1)]) == 2
        assert len(grouped[(2026, 2)]) == 1
        assert grouped[(2026, 7)] == []               # 데이터 없는 달

    def test_second_call_uses_cache_zero_fetches(self, sheet):
        months = [(2026, m) for m in range(1, 13)]
        sheets.get_records_for_months(months)
        assert sheet.fetch_count == 1
        sheets.get_records_for_months(months)
        assert sheet.fetch_count == 1, "캐시가 있는데도 시트를 다시 읽었습니다"

    def test_empty_month_is_cached_not_refetched(self, sheet):
        """데이터 없는 달도 캐시에 남아 매번 재조회하지 않아야 한다."""
        sheets.get_records_for_months([(2026, 7)])
        assert sheet.fetch_count == 1
        sheets.get_records_for_months([(2026, 7)])
        assert sheet.fetch_count == 1

    def test_single_month_fetch_populates_other_months(self, sheet):
        """한 달만 읽어도 어차피 전체를 받으므로 다른 달 캐시도 채워진다."""
        sheets.get_records_for_month(2026, 1)
        assert sheet.fetch_count == 1
        sheets.get_records_for_month(2026, 2)     # 추가 조회 없이 캐시 히트
        assert sheet.fetch_count == 1

    def test_user_filter(self, sheet):
        grouped = sheets.get_records_for_months([(2026, 12)], user_id=2)
        assert [r["id"] for r in grouped[(2026, 12)]] == ["AAAA0005"]
        grouped = sheets.get_records_for_months([(2026, 12)], user_id=1)
        assert grouped[(2026, 12)] == []

    def test_malformed_date_rows_are_skipped(self, monkeypatch):
        ws = FakeWorksheet([
            _rec("OK", "2026-01-05 10:00", 1000),
            _rec("BAD1", "", 1000),
            _rec("BAD2", "not-a-date", 1000),
            _rec("BAD3", "20260105", 1000),
            _rec("BAD4", "2026-13-01 10:00", 1000),   # 13월은 없음
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        grouped = sheets.get_records_for_months([(2026, 1)])
        assert [r["id"] for r in grouped[(2026, 1)]] == ["OK"]

    @pytest.mark.parametrize("date_str", [
        "2026-01-05 10:00",   # 봇이 쓰는 표준 형식
        "2026-1-5 10:00",     # 사람이 손으로 적은 한 자리 월/일
        "2026/01/05",         # 슬래시
        "2026. 1. 5",         # 시트 한국 로케일 표시
        "  2026-01-05",       # 앞 공백
    ])
    def test_human_edited_date_formats_are_recognised(self, monkeypatch, date_str):
        """사람이 편집한 날짜 표기도 해당 월로 잡혀야 한다."""
        ws = FakeWorksheet([_rec("X", date_str, 1000)])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        grouped = sheets.get_records_for_months([(2026, 1)])
        assert [r["id"] for r in grouped[(2026, 1)]] == ["X"], f"인식 실패: {date_str!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 캐시 격리 — 호출자의 변형이 캐시를 오염시키지 않아야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestCacheIsolation:
    def test_sorting_result_does_not_corrupt_cache(self, sheet):
        first = sheets.get_records_for_month(2026, 1)
        original_order = [r["id"] for r in first]
        first.sort(key=lambda r: r["id"], reverse=True)   # 호출자가 in-place 정렬
        second = sheets.get_records_for_month(2026, 1)
        assert [r["id"] for r in second] == original_order, "캐시가 오염되었습니다"

    def test_returned_lists_are_distinct_objects(self, sheet):
        a = sheets.get_records_for_month(2026, 1)
        b = sheets.get_records_for_month(2026, 1)
        assert a is not b
        a.clear()
        assert len(sheets.get_records_for_month(2026, 1)) == 2

    def test_grouped_result_is_isolated(self, sheet):
        g = sheets.get_records_for_months([(2026, 1)])
        g[(2026, 1)].clear()
        assert len(sheets.get_records_for_months([(2026, 1)])[(2026, 1)]) == 2

    def test_users_cache_isolated(self, monkeypatch):
        ws = FakeWorksheet([{"user_id": "1", "display_name": "아빠", "role": "admin"}])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        users = sheets.get_all_users()
        users.clear()
        assert len(sheets.get_all_users()) == 1

    def test_budgets_cache_isolated(self, monkeypatch):
        ws = FakeWorksheet([
            {"user_id": "1", "display_name": "아빠", "category": "식비",
             "amount": 100000, "year": 2026, "month": 1},
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        b = sheets.get_all_budgets_for_month(1, 2026, 1)
        b["식비"] = 0
        assert sheets.get_all_budgets_for_month(1, 2026, 1)["식비"] == 100000

    def test_budget_amount_coerced_safely(self, monkeypatch):
        ws = FakeWorksheet([
            {"user_id": "1", "display_name": "아빠", "category": "식비",
             "amount": "10만원", "year": 2026, "month": 1},
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        assert sheets.get_all_budgets_for_month(1, 2026, 1) == {"식비": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# 4. 배치 수정 — 여러 필드가 API 호출 1회로 묶여야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestBatchUpdate:
    def test_three_fields_one_api_call(self, sheet):
        ok = sheets.update_record_by_id(
            "AAAA0002", {"category": "쇼핑", "amount": "50000", "memo": "수정"}
        )
        assert ok is True
        assert len(sheet.batch_calls) == 1, "필드마다 API를 호출하고 있습니다"
        assert sheet.cell_calls == []
        ranges = {d["range"] for d in sheet.batch_calls[0][0]}
        # AAAA0002 는 데이터 2번째 행 → 시트 3행. category=E, amount=F, memo=G
        assert ranges == {"E3", "F3", "G3"}

    def test_partial_update_only_touches_given_fields(self, sheet):
        sheets.update_record_by_id("AAAA0001", {"memo": "메모만"})
        data = sheet.batch_calls[0][0]
        assert len(data) == 1
        assert data[0]["range"] == "G2"
        assert data[0]["values"] == [["메모만"]]

    def test_unknown_field_rejected(self, sheet):
        with pytest.raises(ValueError):
            sheets.update_record_by_id("AAAA0001", {"date": "2026-01-01"})
        assert sheet.batch_calls == []

    def test_missing_record_returns_false(self, sheet):
        assert sheets.update_record_by_id("ZZZZ9999", {"memo": "x"}) is False
        assert sheet.batch_calls == []

    def test_empty_fields_is_noop(self, sheet):
        assert sheets.update_record_by_id("AAAA0001", {}) is False
        assert sheet.batch_calls == []

    def test_update_invalidates_records_cache(self, sheet):
        sheets.get_records_for_month(2026, 1)
        assert sheet.fetch_count == 1
        sheets.update_record_by_id("AAAA0001", {"memo": "변경"})
        sheets.get_records_for_month(2026, 1)
        assert sheet.fetch_count > 1, "수정 후에도 낡은 캐시를 반환했습니다"


# ─────────────────────────────────────────────────────────────────────────────
# 5. 동시성 — 캐시가 비었을 때 여러 스레드가 중복 다운로드하지 않아야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestSingleFlight:
    def test_concurrent_cold_reads_fetch_once(self, monkeypatch):
        """대시보드/연간/트렌드가 동시에 들어와도 시트는 한 번만 읽어야 한다."""
        import threading

        ws = FakeWorksheet([_rec("A1", "2026-01-05 10:00", 1000)])
        real_fetch = ws.get_all_records

        def slow_fetch():
            time.sleep(0.05)          # 다운로드 지연을 만들어 경쟁을 유도
            return real_fetch()

        ws.get_all_records = slow_fetch
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)

        barrier = threading.Barrier(3)
        errors = []

        def worker(months):
            try:
                barrier.wait()        # 세 스레드를 동시에 출발시킨다
                sheets.get_records_for_months(months)
            except Exception as e:    # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=([(2026, 1)],)),
            threading.Thread(target=worker, args=([(2026, m) for m in range(1, 13)],)),
            threading.Thread(target=worker, args=([(2026, 1), (2025, 12)],)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert ws.fetch_count == 1, (
            f"동시 요청이 시트를 {ws.fetch_count}번 내려받았습니다 (1회여야 함)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. 기간 조회 — 달을 걸치는 주간 리포트가 비지 않아야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestRangeQuery:
    @pytest.fixture
    def span_sheet(self, monkeypatch):
        ws = FakeWorksheet([
            _rec("JAN27", "2026-01-27 09:00", 1000),   # 범위 밖(이전)
            _rec("JAN28", "2026-01-28 09:00", 1000),   # 범위 시작
            _rec("JAN31", "2026-01-31 09:00", 1000),
            _rec("FEB01", "2026-02-01 09:00", 1000),   # 달을 넘어감
            _rec("FEB03", "2026-02-03 09:00", 1000),   # 범위 끝
            _rec("FEB04", "2026-02-04 09:00", 1000),   # 범위 밖(이후)
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        return ws

    def test_range_spanning_month_boundary(self, span_sheet):
        """1/28~2/3 주간이 이전에는 항상 빈 결과였다 (day_start 28 > day_end 3)."""
        from datetime import date
        got = sheets.get_records_in_range(date(2026, 1, 28), date(2026, 2, 3))
        assert [r["id"] for r in got] == ["JAN28", "JAN31", "FEB01", "FEB03"]

    def test_range_spanning_year_boundary(self, monkeypatch):
        from datetime import date
        ws = FakeWorksheet([
            _rec("DEC30", "2025-12-30 09:00", 1000),
            _rec("JAN02", "2026-01-02 09:00", 1000),
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        got = sheets.get_records_in_range(date(2025, 12, 29), date(2026, 1, 3))
        assert [r["id"] for r in got] == ["DEC30", "JAN02"]

    def test_range_uses_one_fetch(self, span_sheet):
        from datetime import date
        sheets.get_records_in_range(date(2026, 1, 28), date(2026, 2, 3))
        assert span_sheet.fetch_count == 1

    def test_range_filters_by_user(self, monkeypatch):
        from datetime import date
        ws = FakeWorksheet([
            _rec("A", "2026-01-28 09:00", 1000, user_id="1"),
            _rec("B", "2026-02-01 09:00", 1000, user_id="2"),
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        got = sheets.get_records_in_range(date(2026, 1, 28), date(2026, 2, 3), user_id=2)
        assert [r["id"] for r in got] == ["B"]

    def test_reversed_range_is_normalised(self, span_sheet):
        from datetime import date
        got = sheets.get_records_in_range(date(2026, 2, 3), date(2026, 1, 28))
        assert [r["id"] for r in got] == ["JAN28", "JAN31", "FEB01", "FEB03"]

    def test_week_helper_still_works_within_month(self, span_sheet):
        got = sheets.get_records_for_week(2026, 1, 27, 31)
        assert [r["id"] for r in got] == ["JAN27", "JAN28", "JAN31"]

    def test_week_helper_clamps_overflow_day(self, span_sheet):
        """2월 31일 같은 값이 들어와도 예외 없이 말일로 잘려야 한다."""
        got = sheets.get_records_for_week(2026, 2, 1, 31)
        assert [r["id"] for r in got] == ["FEB01", "FEB03", "FEB04"]

    def test_invalid_calendar_date_row_skipped(self, monkeypatch):
        from datetime import date
        ws = FakeWorksheet([
            _rec("GOOD", "2026-02-03 09:00", 1000),
            _rec("BAD", "2026-02-31 09:00", 1000),      # 존재하지 않는 날짜
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        got = sheets.get_records_in_range(date(2026, 2, 1), date(2026, 2, 28))
        assert [r["id"] for r in got] == ["GOOD"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. 예산 조회 — 사용자 수만큼 재조회하지 않아야 한다
# ─────────────────────────────────────────────────────────────────────────────
class TestBudgetsBatch:
    @pytest.fixture
    def budget_sheet(self, monkeypatch):
        ws = FakeWorksheet([
            {"user_id": "1", "display_name": "아빠", "category": "식비",
             "amount": 300000, "year": 2026, "month": 1},
            {"user_id": "2", "display_name": "엄마", "category": "쇼핑",
             "amount": 200000, "year": 2026, "month": 1},
            {"user_id": "1", "display_name": "아빠", "category": "교통비",
             "amount": 100000, "year": 2026, "month": 2},   # 다른 달
        ])
        monkeypatch.setattr(sheets, "get_sheet", lambda name: ws)
        return ws

    def test_multiple_users_cost_one_fetch(self, budget_sheet):
        """가족 3명에게 리포트를 보내도 예산 시트는 한 번만 읽어야 한다."""
        assert sheets.get_all_budgets_for_month(1, 2026, 1) == {"식비": 300000.0}
        assert sheets.get_all_budgets_for_month(2, 2026, 1) == {"쇼핑": 200000.0}
        assert sheets.get_all_budgets_for_month(3, 2026, 1) == {}     # 예산 없는 사용자
        assert budget_sheet.fetch_count == 1, (
            f"사용자마다 예산 시트를 다시 읽었습니다 ({budget_sheet.fetch_count}회)"
        )

    def test_user_without_budget_is_cached(self, budget_sheet):
        sheets.get_all_budgets_for_month(9, 2026, 1)
        sheets.get_all_budgets_for_month(9, 2026, 1)
        assert budget_sheet.fetch_count == 1

    def test_month_is_respected(self, budget_sheet):
        assert sheets.get_all_budgets_for_month(1, 2026, 2) == {"교통비": 100000.0}

    def test_setting_a_budget_invalidates_cache(self, budget_sheet):
        """예산을 바꾸면 낡은 값이 아니라 새 값을 읽어야 한다."""
        assert sheets.get_all_budgets_for_month(1, 2026, 1) == {"식비": 300000.0}
        budget_sheet.rows[0]["amount"] = 500000        # 시트가 바뀐 상황
        sheets._invalidate_budgets_cache(1)
        assert sheets.get_all_budgets_for_month(1, 2026, 1) == {"식비": 500000.0}

    def test_missing_user_not_cached_after_invalidation(self, budget_sheet):
        sheets.get_all_budgets_for_month(3, 2026, 1)
        sheets._invalidate_budgets_cache()
        budget_sheet.rows.append({
            "user_id": "3", "display_name": "첫째", "category": "교육",
            "amount": 50000, "year": 2026, "month": 1,
        })
        assert sheets.get_all_budgets_for_month(3, 2026, 1) == {"교육": 50000.0}


class TestColLetter:
    @pytest.mark.parametrize("idx,expected", [
        (1, "A"), (5, "E"), (6, "F"), (7, "G"), (26, "Z"), (27, "AA"), (28, "AB"),
    ])
    def test_col_letter(self, idx, expected):
        assert sheets._col_letter(idx) == expected
