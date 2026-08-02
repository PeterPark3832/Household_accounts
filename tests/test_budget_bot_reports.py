"""
budget_bot.py 의 '금액이 걸린' 로직 테스트.

기존 봇 테스트는 순수 유틸 4개(parse_amount/fmt/match_category/prev_month)만
덮고 있었습니다. 여기서는 사용자에게 실제로 숫자를 보여 주거나 파일로
내보내는 경로를 검증합니다 — 조용히 틀리면 알아채기 어려운 곳들입니다.

특히 주간 리포트는 달을 걸치는 주에 빈 값을 보내는 버그가 있었으므로
회귀 테스트를 둡니다.
"""
import asyncio
import csv
import io
from datetime import datetime, timedelta

import pytest

import budget_bot
import sheets


def _rec(date, amount, rtype="expense", category="식비", uid="1", name="아빠", memo=""):
    return {
        "id": "REC00001", "user_id": uid, "display_name": name,
        "type": rtype, "category": category, "amount": amount,
        "memo": memo, "date": date,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 예산 경고 임계값
# ─────────────────────────────────────────────────────────────────────────────
class TestBudgetBar:
    @pytest.mark.parametrize("pct,expected", [
        (0, "🟩"), (79.9, "🟩"), (80, "🟨"), (99.9, "🟨"), (100, "🟥"), (150, "🟥"),
    ])
    def test_thresholds(self, pct, expected):
        assert budget_bot._budget_bar(pct) == expected


class TestBudgetWarning:
    def _run(self, budget, records):
        async def go():
            return await budget_bot._build_budget_warn(1, "식비", "🍚", 2026, 1)
        budget_bot.sheets = sheets
        orig_budget = sheets.get_budget
        orig_recs   = sheets.get_records_for_month
        sheets.get_budget = lambda *a, **kw: budget
        sheets.get_records_for_month = lambda *a, **kw: records
        try:
            return asyncio.run(go())
        finally:
            sheets.get_budget = orig_budget
            sheets.get_records_for_month = orig_recs

    def test_no_budget_means_no_warning(self):
        assert self._run(None, [_rec("2026-01-05 09:00", 999_999)]) == ""

    def test_under_80_percent_is_quiet(self):
        out = self._run(100_000, [_rec("2026-01-05 09:00", 50_000)])
        assert out == ""

    def test_at_80_percent_warns(self):
        out = self._run(100_000, [_rec("2026-01-05 09:00", 80_000)])
        assert "80%" in out and "남은 예산" in out
        assert "20,000원" in out          # 남은 금액

    def test_over_budget_reports_overage(self):
        out = self._run(100_000, [_rec("2026-01-05 09:00", 130_000)])
        assert "예산 초과" in out
        assert "30,000원" in out          # 초과 금액
        assert "130%" in out

    def test_ignores_other_categories(self):
        out = self._run(100_000, [_rec("2026-01-05 09:00", 90_000, category="쇼핑")])
        assert out == ""

    def test_malformed_amount_does_not_crash(self):
        out = self._run(100_000, [
            _rec("2026-01-05 09:00", "삼만원"),
            _rec("2026-01-06 09:00", 90_000),
        ])
        assert "80%" in out


# ─────────────────────────────────────────────────────────────────────────────
# CSV 내보내기
# ─────────────────────────────────────────────────────────────────────────────
class TestCsvExport:
    def _parse(self, blob: bytes):
        assert blob.startswith(b"\xef\xbb\xbf"), "엑셀 한글 깨짐 방지용 BOM 이 없습니다"
        text = blob.decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_header_and_row_shape(self):
        rows = self._parse(budget_bot._build_csv_bytes([
            _rec("2026-01-05 09:00", 12_345, memo="점심"),
        ]))
        assert rows[0] == ["번호", "유형", "카테고리", "금액", "메모", "날짜"]
        assert rows[1][1] == "지출"
        assert rows[1][2] == "식비"
        assert rows[1][3] == "12345"
        assert rows[1][4] == "점심"

    def test_income_label(self):
        rows = self._parse(budget_bot._build_csv_bytes([
            _rec("2026-01-05 09:00", 500_000, rtype="income", category="급여"),
        ]))
        assert rows[1][1] == "수입"

    def test_malformed_amount_exports_as_zero(self):
        """시트에 문자가 들어 있어도 내보내기가 실패하면 안 된다."""
        rows = self._parse(budget_bot._build_csv_bytes([
            _rec("2026-01-05 09:00", "삼만원"),
            _rec("2026-01-06 09:00", ""),
            _rec("2026-01-07 09:00", "20,000"),
        ]))
        assert [r[3] for r in rows[1:]] == ["0", "0", "20000"]

    def test_empty_export_still_has_header(self):
        rows = self._parse(budget_bot._build_csv_bytes([]))
        assert len(rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 자동 리포트 — 주간(달 경계 회귀) / 월간
# ─────────────────────────────────────────────────────────────────────────────
class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append({"chat_id": chat_id, "text": text})


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()


@pytest.fixture
def bot_env(monkeypatch):
    """users/records/budgets 를 결정적인 값으로 고정."""
    users = [
        {"user_id": "1", "display_name": "아빠", "role": "admin"},
        {"user_id": "2", "display_name": "엄마", "role": "member"},
        {"user_id": "3", "display_name": "손님", "role": "pending"},   # 제외 대상
    ]
    monkeypatch.setattr(sheets, "get_all_users", lambda *a, **kw: list(users))
    monkeypatch.setattr(sheets, "get_all_budgets_for_month", lambda *a, **kw: {"식비": 100_000})
    return users


class TestWeeklyReport:
    def test_week_spanning_month_boundary_is_not_empty(self, bot_env, monkeypatch):
        """1/28~2/3 처럼 달을 걸치는 주에도 실제 금액이 나가야 한다.

        예전에는 get_records_for_week(month=1, day_start=28, day_end=3) 이라
        조건이 성립하지 않아 모두에게 0원 리포트가 발송됐다.
        """
        # 2월 4일 월요일이라고 가정 → 집계 구간은 1/28 ~ 2/3
        fake_now = datetime(2026, 2, 4, 9, 0, tzinfo=budget_bot.KST)
        monkeypatch.setattr(budget_bot, "now_kst", lambda: fake_now)

        captured = {}

        def fake_range(start, end, user_id=None):
            captured["start"], captured["end"] = start, end
            return [
                _rec("2026-01-28 09:00", 10_000),
                _rec("2026-02-01 09:00", 20_000),
                _rec("2026-02-03 09:00", 5_000, rtype="income", category="급여"),
            ]

        monkeypatch.setattr(sheets, "get_records_in_range", fake_range)

        app = FakeApp()
        asyncio.run(budget_bot.scheduled_weekly(app))

        assert captured["start"].isoformat() == "2026-01-28"
        assert captured["end"].isoformat() == "2026-02-03"
        assert len(app.bot.sent) == 2                 # 승인된 구성원에게만
        body = app.bot.sent[0]["text"]
        assert "💸 지출: 30,000원" in body             # 1/28 + 2/1 (달을 걸쳐 합산)
        assert "💰 수입: 5,000원" in body
        assert "💰 수입: 0원" not in body, "빈 리포트가 발송됐습니다"
        assert "💸 지출: 0원" not in body, "빈 리포트가 발송됐습니다"

    def test_pending_member_is_skipped(self, bot_env, monkeypatch):
        monkeypatch.setattr(budget_bot, "now_kst",
                            lambda: datetime(2026, 2, 4, 9, 0, tzinfo=budget_bot.KST))
        monkeypatch.setattr(sheets, "get_records_in_range", lambda *a, **kw: [])
        app = FakeApp()
        asyncio.run(budget_bot.scheduled_weekly(app))
        assert [m["chat_id"] for m in app.bot.sent] == [1, 2]

    def test_one_member_failing_does_not_block_others(self, bot_env, monkeypatch):
        monkeypatch.setattr(budget_bot, "now_kst",
                            lambda: datetime(2026, 2, 4, 9, 0, tzinfo=budget_bot.KST))
        monkeypatch.setattr(sheets, "get_records_in_range", lambda *a, **kw: [])
        app = FakeApp()
        orig = app.bot.send_message

        async def flaky(chat_id, text, parse_mode=None):
            if chat_id == 1:
                raise RuntimeError("텔레그램 오류")
            await orig(chat_id, text, parse_mode)

        app.bot.send_message = flaky
        asyncio.run(budget_bot.scheduled_weekly(app))
        assert [m["chat_id"] for m in app.bot.sent] == [2], "한 명 실패가 전체를 막았습니다"


class TestMonthlyReport:
    def test_totals_and_budget_flag(self, bot_env, monkeypatch):
        monkeypatch.setattr(
            sheets, "get_records_for_month",
            lambda *a, **kw: [
                _rec("2026-01-05 09:00", 120_000),                      # 식비, 예산 초과
                _rec("2026-01-06 09:00", 300_000, rtype="income", category="급여"),
            ],
        )
        app = FakeApp()
        asyncio.run(budget_bot._auto_report(app, 2026, 1, "1월 월간"))

        assert len(app.bot.sent) == 2
        text = app.bot.sent[0]["text"]
        assert "300,000원" in text          # 수입
        assert "120,000원" in text          # 지출
        assert "180,000원" in text          # 순수지
        assert "🟥" in text                 # 예산 100% 초과 표시

    def test_negative_balance_uses_down_icon(self, bot_env, monkeypatch):
        monkeypatch.setattr(
            sheets, "get_records_for_month",
            lambda *a, **kw: [_rec("2026-01-05 09:00", 50_000)],
        )
        app = FakeApp()
        asyncio.run(budget_bot._auto_report(app, 2026, 1, "1월 월간"))
        assert "📉" in app.bot.sent[0]["text"]

    def test_malformed_amounts_do_not_break_report(self, bot_env, monkeypatch):
        monkeypatch.setattr(
            sheets, "get_records_for_month",
            lambda *a, **kw: [
                _rec("2026-01-05 09:00", "삼만원"),
                _rec("2026-01-06 09:00", 10_000),
            ],
        )
        app = FakeApp()
        asyncio.run(budget_bot._auto_report(app, 2026, 1, "1월 월간"))
        assert len(app.bot.sent) == 2
        assert "10,000원" in app.bot.sent[0]["text"]

    def test_no_records_still_sends(self, bot_env, monkeypatch):
        monkeypatch.setattr(sheets, "get_records_for_month", lambda *a, **kw: [])
        app = FakeApp()
        asyncio.run(budget_bot._auto_report(app, 2026, 1, "1월 월간"))
        assert len(app.bot.sent) == 2
        assert "(없음)" in app.bot.sent[0]["text"]


class TestScheduledMonthly:
    def test_reports_previous_month(self, bot_env, monkeypatch):
        """매월 1일에 돌면서 '전월'을 집계해야 한다."""
        monkeypatch.setattr(budget_bot, "now_kst",
                            lambda: datetime(2026, 3, 1, 9, 0, tzinfo=budget_bot.KST))
        seen = {}

        def fake_records(year, month, uid=None):
            seen["ym"] = (year, month)
            return []

        monkeypatch.setattr(sheets, "get_records_for_month", fake_records)
        asyncio.run(budget_bot.scheduled_monthly(FakeApp()))
        assert seen["ym"] == (2026, 2), "전월이 아닌 달을 집계했습니다"

    def test_january_rolls_back_to_previous_year(self, bot_env, monkeypatch):
        monkeypatch.setattr(budget_bot, "now_kst",
                            lambda: datetime(2026, 1, 1, 9, 0, tzinfo=budget_bot.KST))
        seen = {}
        monkeypatch.setattr(sheets, "get_records_for_month",
                            lambda y, m, uid=None: seen.setdefault("ym", (y, m)) and [])
        asyncio.run(budget_bot.scheduled_monthly(FakeApp()))
        assert seen["ym"] == (2025, 12)
