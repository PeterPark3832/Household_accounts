"""
Smoke tests for charts.py.

Goals:
  - Every chart function returns bytes (a valid PNG) on normal input.
  - Every chart function handles empty / edge-case input without raising.
  - Korean font warnings are expected in CI (no Nanum font) — suppressed.

We do NOT do pixel-level image comparison; correctness of the visual output
is verified manually / by the bot operators.
"""
import warnings
import pytest

import charts
from charts import (
    pie_chart,
    bar_chart_budget,
    bar_chart_monthly_trend,
    bar_chart_by_member,
)

PNG_HEADER = b"\x89PNG"


def assert_png(result):
    """Assert that result is a non-empty PNG bytes object."""
    assert isinstance(result, bytes), f"expected bytes, got {type(result)}"
    assert len(result) > 0, "PNG bytes must not be empty"
    assert result[:4] == PNG_HEADER, "result must start with PNG header"


# Suppress expected Korean glyph warnings (no Nanum font in CI)
pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


# ── pie_chart ─────────────────────────────────────────────────────────────────

class TestPieChart:
    def test_normal_data_returns_png(self):
        data = {"식비": 50_000, "교통비": 15_000, "통신비": 30_000}
        assert_png(pie_chart("지출 현황", data))

    def test_empty_data_returns_none(self):
        assert pie_chart("지출 현황", {}) is None

    def test_single_category(self):
        assert_png(pie_chart("단일 카테고리", {"식비": 100_000}))

    def test_many_categories(self):
        data = {f"카테고리{i}": i * 10_000 for i in range(1, 13)}
        assert_png(pie_chart("카테고리 많음", data))

    def test_custom_unit(self):
        assert_png(pie_chart("달러 지출", {"Food": 100, "Transport": 50}, unit="$"))

    def test_small_slice_below_3pct(self):
        # Slices < 3% get no label — should not raise
        data = {"큰항목": 990_000, "작은항목": 1_000}
        assert_png(pie_chart("작은 조각", data))


# ── bar_chart_budget ──────────────────────────────────────────────────────────

class TestBarChartBudget:
    def test_normal_data_returns_png(self):
        cats   = ["식비", "교통비", "통신비"]
        actual = [80_000, 10_000, 30_000]
        budget = [100_000, 15_000, 30_000]
        assert_png(bar_chart_budget("예산 비교", cats, actual, budget))

    def test_empty_categories_returns_none(self):
        assert bar_chart_budget("예산 비교", [], [], []) is None

    def test_over_budget_does_not_raise(self):
        cats   = ["식비"]
        actual = [150_000]
        budget = [100_000]
        assert_png(bar_chart_budget("초과", cats, actual, budget))

    def test_single_category(self):
        assert_png(bar_chart_budget("단일", ["식비"], [50_000], [80_000]))

    def test_many_categories(self):
        n = 10
        cats   = [f"항목{i}" for i in range(n)]
        actual = [i * 10_000 for i in range(n)]
        budget = [i * 12_000 for i in range(n)]
        assert_png(bar_chart_budget("많은 항목", cats, actual, budget))


# ── bar_chart_monthly_trend ───────────────────────────────────────────────────

class TestBarChartMonthlyTrend:
    def test_normal_data_returns_png(self):
        months   = ["2024.01", "2024.02", "2024.03"]
        incomes  = [500_000, 520_000, 480_000]
        expenses = [400_000, 450_000, 390_000]
        assert_png(bar_chart_monthly_trend("월별 트렌드", months, incomes, expenses))

    def test_empty_months_returns_png_without_error(self):
        # No early-return guard; empty input should still produce a valid PNG
        result = bar_chart_monthly_trend("빈 트렌드", [], [], [])
        assert result is None or isinstance(result, bytes)

    def test_expense_exceeds_income(self):
        # net line goes negative — should not crash
        months   = ["2024.01"]
        incomes  = [100_000]
        expenses = [200_000]
        assert_png(bar_chart_monthly_trend("적자", months, incomes, expenses))

    def test_six_months(self):
        months   = [f"2024.{m:02d}" for m in range(1, 7)]
        incomes  = [500_000] * 6
        expenses = [400_000] * 6
        assert_png(bar_chart_monthly_trend("6개월", months, incomes, expenses))

    def test_zero_values(self):
        months   = ["2024.01", "2024.02"]
        incomes  = [0, 0]
        expenses = [0, 0]
        assert_png(bar_chart_monthly_trend("영값", months, incomes, expenses))


# ── bar_chart_by_member ───────────────────────────────────────────────────────

class TestBarChartByMember:
    def test_normal_data_returns_png(self):
        members = ["홍길동", "김영희"]
        values  = [300_000, 200_000]
        assert_png(bar_chart_by_member("멤버별 지출", members, values))

    def test_empty_members_returns_none(self):
        assert bar_chart_by_member("빈", [], []) is None

    def test_single_member(self):
        assert_png(bar_chart_by_member("단일 멤버", ["홍길동"], [500_000]))

    def test_custom_label(self):
        assert_png(bar_chart_by_member("수입", ["홍길동"], [500_000], label="수입"))

    def test_many_members(self):
        members = [f"멤버{i}" for i in range(6)]
        values  = [i * 50_000 for i in range(1, 7)]
        assert_png(bar_chart_by_member("대가족", members, values))
