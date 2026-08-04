"""
코드 검토에서 발견해 고친 버그들에 대한 회귀 테스트.

대상:
- budget_bot.match_category : '기타' 같은 모호한 부분일치가 수입/지출을 뒤집던 문제
- budget_bot.parse_amount   : 부호·지수표기 몰래 제거, 초대형→inf 크래시
- sheets 캐시 쓰기-중-로드 경쟁 : 쓰기가 로드에 덮여 유실되던 문제
"""
import pytest

import budget_bot
import sheets


# ── match_category : 모호한 부분일치는 타입을 단정하지 않는다 ──────────────────
class TestMatchCategory:
    def test_exact_match_wins(self):
        assert budget_bot.match_category("식비") == ("식비", "expense")
        assert budget_bot.match_category("급여") == ("급여", "income")

    def test_ambiguous_keyword_is_rejected(self):
        # '기타' 는 기타수입(income)·기타지출(expense) 양쪽에 걸린다 → 단정 금지
        assert budget_bot.match_category("기타") is None
        # '보' 는 보너스(income)·보험(expense) 양쪽에 걸린다
        assert budget_bot.match_category("보") is None

    def test_unambiguous_partial_still_works(self):
        cat, rtype = budget_bot.match_category("카페")
        assert rtype == "expense"

    def test_unknown_returns_none(self):
        assert budget_bot.match_category("존재하지않는카테고리") is None


# ── parse_amount : 잘못된 값은 왜곡하지 않고 거부 ─────────────────────────────
class TestParseAmount:
    @pytest.mark.parametrize("raw,expected", [
        ("15000", 15000.0),
        ("1,234,000", 1_234_000.0),
        ("₩5,000", 5000.0),
        ("3000원", 3000.0),
        ("1234.5", 1234.5),
    ])
    def test_valid(self, raw, expected):
        assert budget_bot.parse_amount(raw) == expected

    @pytest.mark.parametrize("raw", [
        "-5000",         # 부호를 몰래 없애 양수로 만들면 안 된다
        "1e9",           # 지수표기 → '19' 로 왜곡되면 안 된다
        "9" * 400,       # 초대형 → float('inf') → int(inf) 크래시 유발
        "0",             # 0 이하 거부
        "삼만원",         # 숫자 아님
        "",              # 빈 값
        "1.000.000",     # 잘못된 소수
    ])
    def test_rejected(self, raw):
        assert budget_bot.parse_amount(raw) is None

    def test_huge_is_finite_guarded(self):
        # 거부되어야 하며, 어떤 경우에도 inf 를 반환하지 않는다
        v = budget_bot.parse_amount("9" * 400)
        assert v is None


# ── sheets 캐시 : 쓰기-중-로드 경쟁에서 쓰기가 유실되지 않는다 ────────────────
def _rec(rec_id, date, amount, user_id="1"):
    return {
        "id": rec_id, "user_id": user_id, "display_name": "아빠",
        "type": "expense", "category": "식비", "amount": amount,
        "memo": "", "date": date,
    }


class TestWriteDuringLoadRace:
    def setup_method(self):
        sheets._invalidate_records_cache()
        sheets._invalidate_budgets_cache()

    def teardown_method(self):
        sheets._invalidate_records_cache()
        sheets._invalidate_budgets_cache()

    def test_record_write_during_full_load_is_not_lost(self, monkeypatch):
        calls = {"n": 0}
        v1 = [_rec("A", "2026-08-01 09:00", 1000)]
        v2 = [_rec("A", "2026-08-01 09:00", 1000), _rec("B", "2026-08-02 09:00", 2000)]

        class WS:
            title = "records"; row_count = 1000

            def get_all_records(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    # 첫 읽기 도중 쓰기가 끼어들어 캐시를 무효화한다
                    sheets._invalidate_records_cache()
                    return [dict(r) for r in v1]
                return [dict(r) for r in v2]

        monkeypatch.setattr(sheets, "get_sheet", lambda name: WS())
        grouped = sheets.get_records_for_months([(2026, 8)])
        ids = sorted(r["id"] for r in grouped[(2026, 8)])
        assert ids == ["A", "B"], "경쟁으로 방금 쓴 레코드가 유실됨"
        assert calls["n"] == 2, "세대가 바뀌었는데 재조회하지 않음"

    def test_budget_write_during_load_not_cached_stale(self, monkeypatch):
        calls = {"n": 0}

        def rows(amount):
            return [{"user_id": "1", "display_name": "아빠", "category": "식비",
                     "amount": amount, "year": 2026, "month": 8}]

        class WS:
            title = "budgets"; row_count = 1000

            def get_all_records(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    sheets._invalidate_budgets_cache(1)
                    return rows(100)
                return rows(200)

        monkeypatch.setattr(sheets, "get_sheet", lambda name: WS())
        # 첫 호출: v1(100)을 읽지만 읽는 사이 무효화 → 캐시에 넣지 않음
        sheets.get_all_budgets_for_month(1, 2026, 8)
        # 둘째 호출: 캐시가 신선하지 않으므로 재조회 → v2(200)
        second = sheets.get_all_budgets_for_month(1, 2026, 8)
        assert second == {"식비": 200.0}, "낡은 예산이 캐시에 남아 재조회되지 않음"
        assert calls["n"] == 2
