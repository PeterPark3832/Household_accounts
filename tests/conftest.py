"""
Shared fixtures for the test suite.

budget_bot.py calls load_dotenv() and reads env vars at import time, so we
patch the minimum required values before the module is imported in each test
session.
"""
import os
import sys

import pytest

# Provide stub env vars so budget_bot can be imported without a real .env
os.environ.setdefault("BUDGET_BOT_TOKEN", "123456789:AABBCCDDEEFFaabbccddeeff-stub")
os.environ.setdefault("ADMIN_USER_ID", "999")
os.environ.setdefault("SPREADSHEET_ID", "stub-sheet-id")
os.environ.setdefault("GOOGLE_CREDS_PATH", "/nonexistent/stub_creds.json")

# Stub heavy optional dependencies so import doesn't fail in CI
import types

for _mod in ("gspread", "google.auth", "google.oauth2", "google.oauth2.service_account"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# sheets & charts are imported by budget_bot; stub them before budget_bot loads
sheets_stub = types.ModuleType("sheets")
charts_stub = types.ModuleType("charts")
sys.modules.setdefault("sheets", sheets_stub)
sys.modules.setdefault("charts", charts_stub)
