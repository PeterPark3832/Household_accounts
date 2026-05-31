"""
sheets.py — Google Sheets CRUD 레이어
구조:
  - records  : 수입/지출 기록 로그
  - budgets  : 예산 설정 (유저 × 카테고리 × 연월)
  - users    : 등록된 가족 구성원
"""

import os
import time
import threading
import uuid
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import WorksheetNotFound, APIError
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# ── 인증 & 연결 ────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client: gspread.Client | None = None
_spreadsheet: gspread.Spreadsheet | None = None
_spreadsheet_lock = threading.Lock()
_cache_lock        = threading.Lock()

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")


def get_spreadsheet() -> gspread.Spreadsheet:
    global _client, _spreadsheet
    if _spreadsheet is None:
        with _spreadsheet_lock:
            if _spreadsheet is None:
                creds = Credentials.from_service_account_file(
                    os.getenv("GOOGLE_CREDS_PATH", "credentials.json"), scopes=SCOPES
                )
                _client = gspread.Client(auth=creds)
                _spreadsheet = _client.open_by_key(SPREADSHEET_ID)
    return _spreadsheet


def get_sheet(name: str) -> gspread.Worksheet:
    ss = get_spreadsheet()
    try:
        return ss.worksheet(name)
    except WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=1000, cols=20)
        return ws


# ── API 재시도 헬퍼 ────────────────────────────────────────────────
def _safe_get_records(ws: gspread.Worksheet) -> list[dict]:
    """Rate Limit(429) / 서버 오류(503) 시 최대 3회 지수 백오프 재시도."""
    for attempt in range(3):
        try:
            return ws.get_all_records()
        except APIError as e:
            status = getattr(e, "response", None)
            code   = getattr(status, "status_code", 0)
            if code in (429, 500, 503) and attempt < 2:
                wait = 2 ** attempt   # 1s, 2s (최대 3초)
                logger.warning(f"Sheets API {code}, {wait}초 후 재시도 ({attempt+1}/3)…")
                time.sleep(wait)
            else:
                raise


# ── 시트 초기화 ────────────────────────────────────────────────────
RECORDS_HEADERS = ["id", "user_id", "display_name", "type", "category", "amount", "memo", "date"]
BUDGETS_HEADERS = ["user_id", "display_name", "category", "amount", "year", "month"]
USERS_HEADERS   = ["user_id", "display_name", "role", "joined_at"]

# 수정 가능한 필드 → 컬럼 번호
EDITABLE_RECORD_FIELDS = {
    "amount":   RECORDS_HEADERS.index("amount") + 1,
    "memo":     RECORDS_HEADERS.index("memo") + 1,
    "category": RECORDS_HEADERS.index("category") + 1,
}


def init_sheets():
    """시트 헤더를 확인하고 필요시 초기화합니다."""
    for name, headers in [
        ("records", RECORDS_HEADERS),
        ("budgets", BUDGETS_HEADERS),
        ("users",   USERS_HEADERS),
    ]:
        try:
            ws = get_sheet(name)
            existing = ws.row_values(1)
            if existing != headers:
                ws.clear()
                ws.append_row(headers, value_input_option="RAW")
                logger.info(f"Sheet '{name}' initialized.")
        except Exception as e:
            logger.error(f"Sheet '{name}' 초기화 실패: {e}", exc_info=True)
            raise RuntimeError(
                f"Google Sheets '{name}' 초기화 실패. credentials.json 및 SPREADSHEET_ID를 확인하세요."
            ) from e


# ── Phase 6: Sheets 행 자동 확장 ─────────────────────────────────
_insert_call_count = 0
_EXPAND_CHECK_EVERY = 50   # N회 삽입마다 용량 체크
_MIN_FREE_ROWS      = 200  # 여유 행이 이 수치 미만이면 확장
_EXPAND_ADD_ROWS    = 1000


def _maybe_expand_sheet(ws: gspread.Worksheet):
    """50회 삽입마다 여유 행을 확인하고, 200행 미만이면 1000행 자동 추가합니다."""
    global _insert_call_count
    _insert_call_count += 1
    if _insert_call_count % _EXPAND_CHECK_EVERY != 0:
        return
    try:
        used = len(ws.col_values(1))          # 헤더 포함 사용 중인 행
        free = ws.row_count - used
        if free < _MIN_FREE_ROWS:
            ws.add_rows(_EXPAND_ADD_ROWS)
            logger.info(
                f"Sheet '{ws.title}': {_EXPAND_ADD_ROWS}행 자동 확장 "
                f"(사용 {used} / 총 {ws.row_count + _EXPAND_ADD_ROWS}행)"
            )
    except Exception as e:
        logger.warning(f"Sheet '{ws.title}' 용량 확인 실패: {e}")


# ── TTL 캐시 — users (30초) ───────────────────────────────────────
_users_cache: list[dict] | None = None
_users_cache_ts: float = 0.0
_USERS_TTL: float = 30.0


def _invalidate_users_cache():
    global _users_cache, _users_cache_ts
    with _cache_lock:
        _users_cache = None
        _users_cache_ts = 0.0


# ── TTL 캐시 — records (현재월 5분, 과거월 24시간) ───────────────
_records_cache: dict[tuple[int, int], tuple[list[dict], float]] = {}
_RECORDS_TTL: float = 300.0
_RECORDS_TTL_PAST: float = 86400.0


def _get_records_from_cache(year: int, month: int) -> list[dict] | None:
    now = datetime.now(KST)
    ttl = _RECORDS_TTL if (year == now.year and month == now.month) else _RECORDS_TTL_PAST
    with _cache_lock:
        entry = _records_cache.get((year, month))
        if entry and (time.monotonic() - entry[1]) < ttl:
            return entry[0]
    return None


def _set_records_cache(year: int, month: int, data: list[dict]):
    with _cache_lock:
        _records_cache[(year, month)] = (data, time.monotonic())


def _invalidate_records_cache():
    with _cache_lock:
        _records_cache.clear()


# ── TTL 캐시 — budgets (2분, 유저×연월 키) ────────────────────────
_budgets_cache: dict[tuple, tuple[dict, float]] = {}
_BUDGETS_TTL: float = 120.0


def _get_budgets_from_cache(user_id: int, year: int, month: int) -> dict[str, float] | None:
    with _cache_lock:
        entry = _budgets_cache.get((str(user_id), year, month))
        if entry and (time.monotonic() - entry[1]) < _BUDGETS_TTL:
            return entry[0]
    return None


def _set_budgets_cache(user_id: int, year: int, month: int, data: dict[str, float]):
    with _cache_lock:
        _budgets_cache[(str(user_id), year, month)] = (data, time.monotonic())


def _invalidate_budgets_cache(user_id: int | None = None):
    with _cache_lock:
        if user_id is None:
            _budgets_cache.clear()
        else:
            for k in list(_budgets_cache.keys()):
                if k[0] == str(user_id):
                    del _budgets_cache[k]


# ── 유저 관리 ──────────────────────────────────────────────────────
def get_all_users() -> list[dict]:
    global _users_cache, _users_cache_ts
    with _cache_lock:
        if _users_cache is not None and (time.monotonic() - _users_cache_ts) < _USERS_TTL:
            return _users_cache
    # I/O는 락 밖에서 실행
    rows = _safe_get_records(get_sheet("users"))
    with _cache_lock:
        _users_cache = rows
        _users_cache_ts = time.monotonic()
    return rows


def find_user(user_id: int) -> dict | None:
    for u in get_all_users():
        if str(u["user_id"]) == str(user_id):
            return u
    return None


def register_user(user_id: int, display_name: str, role: str = "member"):
    ws = get_sheet("users")
    now = datetime.now(KST).isoformat()
    ws.append_row([str(user_id), display_name, role, now], value_input_option="RAW")
    _invalidate_users_cache()
    logger.info(f"User registered: {display_name} ({user_id})")


def is_admin(user_id: int) -> bool:
    u = find_user(user_id)
    return u is not None and u.get("role") == "admin"


def is_approved(user_id: int) -> bool:
    u = find_user(user_id)
    return u is not None and u.get("role") in ("admin", "member")


def _update_user_field(user_id: int, field: str, value: str) -> bool:
    ws = get_sheet("users")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if str(row["user_id"]) == str(user_id):
            ws.update_cell(i, USERS_HEADERS.index(field) + 1, value)
            _invalidate_users_cache()
            return True
    return False


def set_user_role(user_id: int, role: str) -> bool:
    return _update_user_field(user_id, "role", role)


def update_display_name(user_id: int, name: str) -> bool:
    return _update_user_field(user_id, "display_name", name)


# ── 기록 CRUD ──────────────────────────────────────────────────────
def insert_record(
    user_id: int,
    display_name: str,
    record_type: str,
    category: str,
    amount: float,
    memo: str,
    recorded_at: datetime,
) -> str:
    ws = get_sheet("records")
    rec_id = uuid.uuid4().hex[:8].upper()
    ws.append_row(
        [rec_id, str(user_id), display_name, record_type, category, amount,
         memo, recorded_at.strftime("%Y-%m-%d %H:%M")],
        value_input_option="USER_ENTERED",
    )
    _invalidate_records_cache()
    _maybe_expand_sheet(ws)  # Phase 6: 용량 자동 확장
    return rec_id


def update_record(user_id: int, rec_id: str, field: str, value: str) -> bool:
    """기록의 특정 필드를 수정합니다. field: 'amount' | 'memo' | 'category'"""
    if field not in EDITABLE_RECORD_FIELDS:
        return False
    ws = get_sheet("records")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if row["id"] == rec_id and str(row["user_id"]) == str(user_id):
            ws.update_cell(i, EDITABLE_RECORD_FIELDS[field], value)
            _invalidate_records_cache()
            return True
    return False


def delete_record(user_id: int, rec_id: str) -> bool:
    ws = get_sheet("records")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if row["id"] == rec_id and str(row["user_id"]) == str(user_id):
            ws.delete_rows(i)
            _invalidate_records_cache()
            return True
    return False


def get_recent_records(user_id: int | None = None, limit: int = 10) -> list[dict]:
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws)
    if user_id is not None:
        all_rows = [r for r in all_rows if str(r["user_id"]) == str(user_id)]
    return all_rows[-limit:][::-1]


def get_records_for_month(year: int, month: int, user_id: int | None = None) -> list[dict]:
    prefix = f"{year}-{month:02d}"
    cached = _get_records_from_cache(year, month)
    if cached is None:
        ws = get_sheet("records")
        all_rows = _safe_get_records(ws)
        cached = [r for r in all_rows if str(r["date"]).startswith(prefix)]
        _set_records_cache(year, month, cached)
    if user_id is not None:
        return [r for r in cached if str(r["user_id"]) == str(user_id)]
    return cached


def get_records_for_week(
    year: int, month: int, day_start: int, day_end: int,
    user_id: int | None = None,
) -> list[dict]:
    prefix = f"{year}-{month:02d}"
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws)
    result = []
    for r in all_rows:
        d = str(r["date"])
        if not d.startswith(prefix):
            continue
        try:
            if day_start <= int(d[8:10]) <= day_end:
                result.append(r)
        except (ValueError, IndexError):
            continue
    if user_id is not None:
        result = [r for r in result if str(r["user_id"]) == str(user_id)]
    return result


def get_all_records_for_user(user_id: int) -> list[dict]:
    """특정 유저의 전체 기록을 반환합니다 (백업용)."""
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws)
    return [r for r in all_rows if str(r["user_id"]) == str(user_id)]


def search_records(user_id: int, keyword: str, limit: int = 20) -> list[dict]:
    """메모·카테고리에서 키워드를 포함하는 기록을 최신순으로 반환합니다."""
    kw = keyword.lower()
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws)
    matches = [
        r for r in all_rows
        if str(r["user_id"]) == str(user_id)
        and (kw in str(r.get("memo", "")).lower() or kw in str(r.get("category", "")).lower())
    ]
    return matches[-limit:][::-1]


# ── 집계 헬퍼 (순수 Python, I/O 없음) ──────────────────────────────
def monthly_total(records: list[dict], record_type: str) -> float:
    return sum(float(r["amount"]) for r in records if r["type"] == record_type)


def monthly_breakdown(records: list[dict], record_type: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for r in records:
        if r["type"] == record_type:
            cat = r["category"]
            result[cat] = result.get(cat, 0) + float(r["amount"])
    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


def breakdown_by_user(records: list[dict], record_type: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for r in records:
        if r["type"] == record_type:
            name = r["display_name"]
            result[name] = result.get(name, 0) + float(r["amount"])
    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


# ── 예산 CRUD ──────────────────────────────────────────────────────
def set_budget(user_id: int, display_name: str, category: str, amount: float, year: int, month: int):
    ws = get_sheet("budgets")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if (
            str(row["user_id"]) == str(user_id)
            and row["category"] == category
            and str(row["year"]) == str(year)
            and str(row["month"]) == str(month)
        ):
            ws.update_cell(i, BUDGETS_HEADERS.index("amount") + 1, amount)
            _invalidate_budgets_cache(user_id)
            return
    ws.append_row(
        [str(user_id), display_name, category, amount, year, month],
        value_input_option="USER_ENTERED",
    )
    _invalidate_budgets_cache(user_id)


def get_budget(user_id: int, category: str, year: int, month: int) -> float | None:
    budgets = get_all_budgets_for_month(user_id, year, month)
    return budgets.get(category)


def get_all_budgets_for_month(user_id: int, year: int, month: int) -> dict[str, float]:
    cached = _get_budgets_from_cache(user_id, year, month)
    if cached is not None:
        return cached
    ws = get_sheet("budgets")
    result = {}
    for row in _safe_get_records(ws):
        if (
            str(row["user_id"]) == str(user_id)
            and str(row["year"]) == str(year)
            and str(row["month"]) == str(month)
        ):
            result[row["category"]] = float(row["amount"])
    _set_budgets_cache(user_id, year, month, result)
    return result


def copy_budgets_from_month(
    user_id: int, display_name: str,
    src_year: int, src_month: int,
    dst_year: int, dst_month: int,
) -> tuple[list[str], list[str]]:
    """src 월 예산을 dst 월로 복사. 이미 설정된 항목은 건너뜀."""
    src_budgets = get_all_budgets_for_month(user_id, src_year, src_month)
    dst_budgets = get_all_budgets_for_month(user_id, dst_year, dst_month)
    copied, skipped = [], []
    for cat, amount in src_budgets.items():
        if cat in dst_budgets:
            skipped.append(cat)
        else:
            set_budget(user_id, display_name, cat, amount, dst_year, dst_month)
            copied.append(cat)
    return copied, skipped
