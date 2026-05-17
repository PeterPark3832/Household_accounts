"""
Tests for pure aggregation functions in sheets.py.

These functions accept plain dicts and return plain dicts/floats — no gspread
calls, no caching, no threading. Imported via a lightweight stub path so the
Google Sheets client is never initialised.
"""
import sys
import types
import pytest

# ── stub gspread before sheets.py is imported ─────────────────────────────────
# conftest.py already stubs "sheets" itself, but here we want to import the
# *real* sheets module, so we register the gspread stubs and then force-import.

for _mod in (
    "gspread",
    "gspread.exceptions",
    "google",
    "google.oauth2",
    "google.oauth2.service_account",
    "google.auth",
    "google.auth.exceptions",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# gspread exception classes
import gspread  # noqa: E402
gspread_exc = sys.modules["gspread.exceptions"]
for _exc in ("APIError", "WorksheetNotFound"):
    if not hasattr(gspread_exc, _exc):
        setattr(gspread_exc, _exc, type(_exc, (Exception,), {}))

# gspread type stubs referenced at module level in sheets.py
for _cls in ("Client", "Spreadsheet", "Worksheet"):
    if not hasattr(gspread, _cls):
        setattr(gspread, _cls, type(_cls, (), {}))

# google.oauth2.service_account.Credentials
sa_mod = sys.modules["google.oauth2.service_account"]
if not hasattr(sa_mod, "Credentials"):
    sa_mod.Credentials = type("Credentials", (), {})

# Force a fresh import of the real sheets module (bypass the conftest stub)
import importlib  # noqa: E402
if "sheets" in sys.modules:
    # Remove the conftest stub so we load the real file
    del sys.modules["sheets"]

import sheets as _sheets_mod  # noqa: E402

monthly_total     = _sheets_mod.monthly_total
monthly_breakdown = _sheets_mod.monthly_breakdown
breakdown_by_user = _sheets_mod.breakdown_by_user


# ── shared fixtures ───────────────────────────────────────────────────────────

def _rec(record_type: str, category: str, amount, display_name: str = "홍길동") -> dict:
    """Build a minimal record dict matching the sheets schema."""
    return {
        "type": record_type,
        "category": category,
        "amount": amount,
        "display_name": display_name,
    }


MIXED_RECORDS = [
    _rec("income",  "급여",     500_000, "홍길동"),
    _rec("income",  "부업",     100_000, "홍길동"),
    _rec("expense", "식비",      30_000, "홍길동"),
    _rec("expense", "식비",      20_000, "홍길동"),
    _rec("expense", "교통비",    15_000, "홍길동"),
    _rec("expense", "식비",      10_000, "김영희"),
    _rec("income",  "급여",     300_000, "김영희"),
]


# ── monthly_total ─────────────────────────────────────────────────────────────

class TestMonthlyTotal:
    def test_sums_expense_records(self):
        assert monthly_total(MIXED_RECORDS, "expense") == 75_000.0

    def test_sums_income_records(self):
        assert monthly_total(MIXED_RECORDS, "income") == 900_000.0

    def test_empty_list_returns_zero(self):
        assert monthly_total([], "expense") == 0.0

    def test_no_matching_type_returns_zero(self):
        records = [_rec("income", "급여", 100_000)]
        assert monthly_total(records, "expense") == 0.0

    def test_single_record(self):
        assert monthly_total([_rec("expense", "식비", 12_345)], "expense") == 12_345.0

    def test_amount_as_string_coerced_to_float(self):
        # Google Sheets returns everything as strings
        records = [_rec("expense", "식비", "50000")]
        assert monthly_total(records, "expense") == 50_000.0

    def test_amount_as_float_string_with_decimal(self):
        records = [_rec("income", "배당금", "1500.50")]
        assert monthly_total(records, "income") == 1_500.5

    def test_only_matches_exact_type_string(self):
        # "Income" (capitalised) must NOT match "income"
        records = [_rec("Income", "급여", 100_000)]
        assert monthly_total(records, "income") == 0.0

    def test_multiple_records_same_type(self):
        records = [_rec("expense", "식비", i * 1_000) for i in range(1, 6)]
        assert monthly_total(records, "expense") == 15_000.0


# ── monthly_breakdown ─────────────────────────────────────────────────────────

class TestMonthlyBreakdown:
    def test_aggregates_by_category(self):
        result = monthly_breakdown(MIXED_RECORDS, "expense")
        assert result["식비"] == 60_000.0   # 30k + 20k (홍길동) + 10k (김영희)
        assert result["교통비"] == 15_000.0

    def test_sorted_descending_by_amount(self):
        result = monthly_breakdown(MIXED_RECORDS, "expense")
        amounts = list(result.values())
        assert amounts == sorted(amounts, reverse=True)

    def test_income_breakdown(self):
        result = monthly_breakdown(MIXED_RECORDS, "income")
        assert result["급여"] == 800_000.0   # 500k + 300k
        assert result["부업"] == 100_000.0

    def test_empty_list_returns_empty_dict(self):
        assert monthly_breakdown([], "expense") == {}

    def test_no_matching_type_returns_empty_dict(self):
        records = [_rec("income", "급여", 100_000)]
        assert monthly_breakdown(records, "expense") == {}

    def test_single_category_single_record(self):
        records = [_rec("expense", "주거비", 400_000)]
        assert monthly_breakdown(records, "expense") == {"주거비": 400_000.0}

    def test_multiple_categories_no_collision(self):
        records = [
            _rec("expense", "식비",   10_000),
            _rec("expense", "교통비", 20_000),
            _rec("expense", "통신비", 30_000),
        ]
        result = monthly_breakdown(records, "expense")
        assert set(result.keys()) == {"식비", "교통비", "통신비"}

    def test_returns_dict(self):
        result = monthly_breakdown(MIXED_RECORDS, "expense")
        assert isinstance(result, dict)

    def test_ignores_other_type(self):
        # income records must not bleed into expense breakdown
        result = monthly_breakdown(MIXED_RECORDS, "expense")
        assert "급여" not in result
        assert "부업" not in result

    def test_amount_as_string_coerced(self):
        records = [
            _rec("expense", "식비", "15000"),
            _rec("expense", "식비", "5000"),
        ]
        assert monthly_breakdown(records, "expense") == {"식비": 20_000.0}


# ── breakdown_by_user ─────────────────────────────────────────────────────────

class TestBreakdownByUser:
    def test_aggregates_by_display_name(self):
        result = breakdown_by_user(MIXED_RECORDS, "expense")
        assert result["홍길동"] == 65_000.0   # 30k + 20k + 15k
        assert result["김영희"] == 10_000.0

    def test_sorted_descending_by_amount(self):
        result = breakdown_by_user(MIXED_RECORDS, "expense")
        amounts = list(result.values())
        assert amounts == sorted(amounts, reverse=True)

    def test_income_breakdown_by_user(self):
        result = breakdown_by_user(MIXED_RECORDS, "income")
        assert result["홍길동"] == 600_000.0   # 500k + 100k
        assert result["김영희"] == 300_000.0

    def test_empty_list_returns_empty_dict(self):
        assert breakdown_by_user([], "expense") == {}

    def test_no_matching_type_returns_empty_dict(self):
        records = [_rec("income", "급여", 100_000, "홍길동")]
        assert breakdown_by_user(records, "expense") == {}

    def test_single_user_single_record(self):
        records = [_rec("expense", "식비", 50_000, "박철수")]
        assert breakdown_by_user(records, "expense") == {"박철수": 50_000.0}

    def test_three_users(self):
        records = [
            _rec("expense", "식비",   10_000, "가"),
            _rec("expense", "식비",   30_000, "나"),
            _rec("expense", "식비",   20_000, "다"),
        ]
        result = breakdown_by_user(records, "expense")
        assert list(result.keys()) == ["나", "다", "가"]   # descending

    def test_same_user_multiple_categories_summed(self):
        records = [
            _rec("expense", "식비",   10_000, "홍길동"),
            _rec("expense", "교통비", 20_000, "홍길동"),
            _rec("expense", "통신비", 30_000, "홍길동"),
        ]
        result = breakdown_by_user(records, "expense")
        assert result == {"홍길동": 60_000.0}

    def test_amount_as_string_coerced(self):
        records = [
            _rec("income", "급여", "200000", "홍길동"),
            _rec("income", "부업", "50000",  "홍길동"),
        ]
        result = breakdown_by_user(records, "income")
        assert result == {"홍길동": 250_000.0}
