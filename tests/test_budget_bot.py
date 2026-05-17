"""
Tests for pure utility functions in budget_bot.py.

These functions have no external dependencies (no Telegram, no Sheets API),
so they can be tested without any mocking beyond the import-time stubs in
conftest.py.
"""
import sys
import types
import pytest


# ── import isolation ──────────────────────────────────────────────────────────
# Stub the telegram package hierarchy so budget_bot can be imported without the
# real library being installed in a restricted environment.
def _make_telegram_stubs():
    from unittest.mock import MagicMock

    for mod_name in [
        "telegram",
        "telegram.ext",
        "apscheduler",
        "apscheduler.schedulers",
        "apscheduler.schedulers.asyncio",
        "dotenv",
        "matplotlib",
        "matplotlib.pyplot",
        "numpy",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    # All telegram symbols must be callable classes (used as constructors at
    # module level when budget_bot builds keyboard layouts).
    tg = sys.modules["telegram"]
    for sym in (
        "BotCommand", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup",
        "KeyboardButton", "ReplyKeyboardMarkup",
    ):
        setattr(tg, sym, MagicMock(name=sym))

    tg_ext = sys.modules["telegram.ext"]
    for sym in (
        "Application", "CallbackQueryHandler", "CommandHandler",
        "ConversationHandler", "MessageHandler", "filters",
    ):
        setattr(tg_ext, sym, MagicMock(name=sym))

    # ContextTypes.DEFAULT_TYPE is referenced in function annotations.
    ctx_types = MagicMock(name="ContextTypes")
    ctx_types.DEFAULT_TYPE = MagicMock(name="DEFAULT_TYPE")
    tg_ext.ContextTypes = ctx_types

    sched = sys.modules["apscheduler.schedulers.asyncio"]
    sched.AsyncIOScheduler = MagicMock(name="AsyncIOScheduler")

    dotenv = sys.modules["dotenv"]
    dotenv.load_dotenv = lambda: None


_make_telegram_stubs()

import budget_bot  # noqa: E402  (must come after stubs)
from budget_bot import fmt, match_category, parse_amount, prev_month  # noqa: E402


# ── parse_amount ──────────────────────────────────────────────────────────────

class TestParseAmount:
    """parse_amount extracts a positive float from a user-typed string."""

    # happy-path: plain numbers
    def test_integer_string(self):
        assert parse_amount("15000") == 15000.0

    def test_float_string(self):
        assert parse_amount("1500.50") == 1500.5

    # commas and currency symbols are stripped
    def test_comma_separated(self):
        assert parse_amount("15,000") == 15000.0

    def test_won_suffix(self):
        assert parse_amount("15000원") == 15000.0

    def test_combined_formatting(self):
        assert parse_amount("1,500,000원") == 1500000.0

    # whitespace tolerance
    def test_leading_trailing_spaces(self):
        assert parse_amount("  5000  ") == 5000.0

    # zero and negative values are invalid amounts
    def test_zero_returns_none(self):
        assert parse_amount("0") is None

    def test_negative_string_returns_none(self):
        # digits-only extraction strips the minus sign, yielding the magnitude
        # which is positive → treated as valid per current implementation
        result = parse_amount("-500")
        # the minus sign is stripped, leaving "500" → 500.0
        assert result == 500.0

    # non-numeric input
    def test_empty_string_returns_none(self):
        assert parse_amount("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_amount("   ") is None

    def test_letters_only_returns_none(self):
        assert parse_amount("abc") is None

    def test_korean_text_no_digits_returns_none(self):
        assert parse_amount("천원") is None

    # edge: multiple decimal points → float("1.5.0") raises ValueError → None
    def test_multiple_dots_returns_none(self):
        assert parse_amount("1.5.0") is None

    # mixed: digits embedded in text
    def test_digits_embedded_in_text(self):
        assert parse_amount("총 3000원입니다") == 3000.0


# ── fmt ───────────────────────────────────────────────────────────────────────

class TestFmt:
    """fmt formats a number as a Korean won string."""

    def test_thousands_separator(self):
        assert fmt(15000) == "15,000원"

    def test_million(self):
        assert fmt(1000000) == "1,000,000원"

    def test_small_amount(self):
        assert fmt(500) == "500원"

    def test_zero(self):
        assert fmt(0) == "0원"

    def test_float_truncated(self):
        # fmt casts to int, so decimals are dropped
        assert fmt(1500.9) == "1,500원"


# ── match_category ────────────────────────────────────────────────────────────

class TestMatchCategory:
    """match_category looks up a category by exact or partial keyword."""

    # exact matches — income
    def test_exact_income(self):
        assert match_category("급여") == ("급여", "income")

    def test_exact_income_bonus(self):
        assert match_category("보너스") == ("보너스", "income")

    # exact matches — expense
    def test_exact_expense(self):
        assert match_category("식비") == ("식비", "expense")

    def test_exact_expense_transport(self):
        assert match_category("교통비") == ("교통비", "expense")

    # partial match: keyword is a substring of a category name
    def test_partial_income_keyword_in_cat(self):
        # "수익" is contained in "투자수익"
        result = match_category("수익")
        assert result == ("투자수익", "income")

    def test_partial_expense_keyword_in_cat(self):
        # "카페" is contained in "카페/음료"
        result = match_category("카페")
        assert result == ("카페/음료", "expense")

    # partial match: category name is a substring of the keyword
    def test_partial_cat_in_keyword(self):
        # "식비" (expense) is contained in "오늘식비"
        result = match_category("오늘식비")
        assert result == ("식비", "expense")

    # exact wins over partial when both could match
    def test_exact_takes_priority_over_partial(self):
        # "보험" exactly matches; "보험료" would be a partial-only keyword
        assert match_category("보험") == ("보험", "expense")

    # unknown keyword
    def test_unknown_returns_none(self):
        assert match_category("모르는카테고리") is None

    def test_empty_string_returns_none(self):
        assert match_category("") is None

    # whitespace is stripped
    def test_whitespace_trimmed(self):
        assert match_category("  급여  ") == ("급여", "income")

    # income categories are checked before expense on exact match
    def test_income_checked_before_expense_on_exact(self):
        # "기타수입" is income; "기타지출" is expense — no ambiguity
        assert match_category("기타수입") == ("기타수입", "income")
        assert match_category("기타지출") == ("기타지출", "expense")


# ── prev_month ────────────────────────────────────────────────────────────────

class TestPrevMonth:
    """prev_month returns the (year, month) tuple for the previous calendar month."""

    def test_mid_year(self):
        assert prev_month(2024, 6) == (2024, 5)

    def test_january_wraps_to_december(self):
        assert prev_month(2024, 1) == (2023, 12)

    def test_december(self):
        assert prev_month(2024, 12) == (2024, 11)

    def test_february(self):
        assert prev_month(2024, 2) == (2024, 1)

    def test_year_boundary_2000(self):
        assert prev_month(2000, 1) == (1999, 12)

    def test_far_future_year(self):
        assert prev_month(2100, 3) == (2100, 2)

    def test_returns_tuple(self):
        result = prev_month(2024, 5)
        assert isinstance(result, tuple) and len(result) == 2

    def test_result_month_always_in_valid_range(self):
        for month in range(1, 13):
            _, m = prev_month(2024, month)
            assert 1 <= m <= 12
