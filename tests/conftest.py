"""
Session-wide stubs installed before any test module is imported.

Strategy:
- Stub *external* heavy deps (gspread, google-auth, telegram, matplotlib)
  so that budget_bot.py, sheets.py, and dashboard/app.py can all be imported
  without real credentials or installed libraries.
- Import the REAL sheets module (with gspread stubbed) so tests that need its
  pure functions work correctly.
- Stub only charts (matplotlib side-effects aren't needed in the test suite).
"""
import os
import sys
import types
from unittest.mock import MagicMock

# ── minimum env vars required at import time ──────────────────────────────────
os.environ.setdefault("BUDGET_BOT_TOKEN", "123456789:AABBCCDDEEFFaabbccddeeff-stub")
os.environ.setdefault("ADMIN_USER_ID",    "999")
os.environ.setdefault("SPREADSHEET_ID",   "stub-sheet-id")
os.environ.setdefault("GOOGLE_CREDS_PATH", "/nonexistent/stub_creds.json")
os.environ.setdefault("DASHBOARD_USER",   "testuser")
os.environ.setdefault("DASHBOARD_PASS",   "testpass")

# ── gspread + google-auth stubs ───────────────────────────────────────────────
for _mod in (
    "gspread", "gspread.exceptions",
    "google", "google.oauth2", "google.oauth2.service_account",
    "google.auth", "google.auth.exceptions",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

_gspread = sys.modules["gspread"]
for _cls in ("Client", "Spreadsheet", "Worksheet"):
    if not hasattr(_gspread, _cls):
        setattr(_gspread, _cls, type(_cls, (), {}))

_gspread_exc = sys.modules["gspread.exceptions"]
for _exc in ("APIError", "WorksheetNotFound"):
    if not hasattr(_gspread_exc, _exc):
        setattr(_gspread_exc, _exc, type(_exc, (Exception,), {}))

_sa = sys.modules["google.oauth2.service_account"]
if not hasattr(_sa, "Credentials"):
    _sa.Credentials = type("Credentials", (), {})

# ── other heavy deps ──────────────────────────────────────────────────────────
for _mod in ("uvicorn",):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# ── telegram stubs (needed by budget_bot.py) ──────────────────────────────────
for _mod in (
    "telegram", "telegram.ext",
    "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio",
    "dotenv",
    "matplotlib", "matplotlib.pyplot", "numpy",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

_tg = sys.modules["telegram"]
for _sym in (
    "BotCommand", "Update", "InlineKeyboardButton", "InlineKeyboardMarkup",
    "KeyboardButton", "ReplyKeyboardMarkup",
):
    if not hasattr(_tg, _sym):
        setattr(_tg, _sym, MagicMock(name=_sym))

_tg_ext = sys.modules["telegram.ext"]
for _sym in (
    "Application", "CallbackQueryHandler", "CommandHandler",
    "ConversationHandler", "MessageHandler", "filters",
):
    if not hasattr(_tg_ext, _sym):
        setattr(_tg_ext, _sym, MagicMock(name=_sym))

_ctx = MagicMock(name="ContextTypes")
_ctx.DEFAULT_TYPE = MagicMock(name="DEFAULT_TYPE")
_tg_ext.ContextTypes = _ctx

_sched = sys.modules["apscheduler.schedulers.asyncio"]
_sched.AsyncIOScheduler = MagicMock(name="AsyncIOScheduler")

_dotenv = sys.modules["dotenv"]
_dotenv.load_dotenv = lambda *a, **kw: None

# ── load the REAL sheets module ───────────────────────────────────────────────
# (gspread is already stubbed above, so this import won't open any network)
if "sheets" in sys.modules:
    del sys.modules["sheets"]
import sheets  # noqa: F401  real module, needed by test_sheets_pure & test_dashboard

# ── stub charts (matplotlib not needed in tests) ──────────────────────────────
_charts = types.ModuleType("charts")
sys.modules["charts"] = _charts
