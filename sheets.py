"""
sheets.py — Google Sheets CRUD 레이어
구조:
  - records  : 수입/지출 기록 로그
  - budgets  : 예산 설정 (유저 × 카테고리 × 연월)
  - users    : 등록된 가족 구성원
"""

import os
import calendar
import re
import time
import threading
import uuid
import logging
from datetime import date, datetime
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
# 시트 전체 다운로드를 한 번에 하나만 수행하기 위한 잠금 (thundering herd 방지)
_records_load_lock = threading.Lock()

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


# ── 값 변환 헬퍼 ───────────────────────────────────────────────────
def to_number(value) -> float:
    """시트 셀 값을 float 로 변환합니다. 변환 불가 시 0.0.

    시트는 사람이 직접 편집하므로 금액 칸이 비어 있거나("") 사람이 "3만원",
    "1,200 원" 처럼 적어 넣는 일이 실제로 발생합니다. 예전에는 그런 행 하나가
    float() ValueError 로 번져 대시보드/봇 전체가 500 이 됐습니다. 한 행의
    오류가 전체를 마비시키지 않도록 0 으로 처리하고 경고만 남깁니다.
    """
    if isinstance(value, bool):          # bool 은 int 의 서브클래스라 먼저 걸러냄
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value if value is not None else "").strip()
    if not s:
        return 0.0
    cleaned = s.replace(",", "").replace("₩", "").replace("원", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        logger.warning("금액으로 변환할 수 없는 셀 값 %r → 0 으로 처리합니다.", value)
        return 0.0


def _col_letter(index: int) -> str:
    """1-기반 컬럼 번호 → A1 표기 컬럼 문자 (1→A, 27→AA)."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


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
    with _cache_lock:
        _insert_call_count += 1
        due = _insert_call_count % _EXPAND_CHECK_EVERY == 0
    if not due:
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


# 마지막 '시트 전체 로드' 시각. 전체 로드 결과는 모든 달에 대한 진실이므로,
# 이 값이 신선하면 "캐시에 없는 달 = 데이터가 없는 달" 로 단정할 수 있다.
_records_full_load_ts: float | None = None


def _full_load_is_fresh() -> bool:
    with _cache_lock:
        ts = _records_full_load_ts
    # ts=None → 아직 전체 로드한 적 없음. time.monotonic() 은 벽시계가 아니라
    # 부팅 이후 경과 초이므로, 0.0 을 "미로드" 센티넬로 쓰면 갓 부팅한 머신
    # (재부팅 직후 서버·CI 러너)에서 now-0.0 < TTL 이 참이 되어 "방금 로드함" 으로
    # 오판한다 → 시트를 안 읽고 빈 결과를 반환. None 으로 명확히 구분한다.
    return ts is not None and (time.monotonic() - ts) < _RECORDS_TTL


def _invalidate_records_cache():
    global _records_full_load_ts
    with _cache_lock:
        _records_cache.clear()
        _records_full_load_ts = None


# ── TTL 캐시 — budgets (2분, 유저×연월 키) ────────────────────────
_budgets_cache: dict[tuple, tuple[dict, float]] = {}
_BUDGETS_TTL: float = 120.0


def _get_budgets_from_cache(user_id: int, year: int, month: int) -> dict[str, float] | None:
    with _cache_lock:
        entry = _budgets_cache.get((str(user_id), year, month))
        if entry and (time.monotonic() - entry[1]) < _BUDGETS_TTL:
            return dict(entry[0])   # 캐시 원본 보호
    return None


def _set_budgets_cache(user_id: int, year: int, month: int, data: dict[str, float]):
    with _cache_lock:
        _budgets_cache[(str(user_id), year, month)] = (data, time.monotonic())


# (연,월) 별 '예산 시트 전체 로드' 시각. 신선하면 캐시에 없는 사용자는
# 예산이 없는 것으로 단정할 수 있어 재조회가 필요 없다.
_budgets_full_load: dict[tuple[int, int], float] = {}


def _budgets_full_load_is_fresh(year: int, month: int) -> bool:
    with _cache_lock:
        ts = _budgets_full_load.get((year, month))
    # _full_load_is_fresh 와 동일한 이유: 미로드(None)를 0.0 으로 두면 갓 부팅한
    # 머신에서 신선한 것으로 오판한다.
    return ts is not None and (time.monotonic() - ts) < _BUDGETS_TTL


def _invalidate_budgets_cache(user_id: int | None = None):
    with _cache_lock:
        _budgets_full_load.clear()
    return _invalidate_budgets_cache_entries(user_id)


def _invalidate_budgets_cache_entries(user_id: int | None = None):
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
            return list(_users_cache)   # 캐시 원본이 밖에서 변형되지 않도록 복사
    # I/O는 락 밖에서 실행
    rows = _safe_get_records(get_sheet("users")) or []
    with _cache_lock:
        _users_cache = rows
        _users_cache_ts = time.monotonic()
    return list(rows)


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


def delete_record_by_id(rec_id: str) -> bool:
    """관리자용: user_id 확인 없이 ID로 기록 삭제."""
    ws = get_sheet("records")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if row["id"] == rec_id:
            ws.delete_rows(i)
            _invalidate_records_cache()
            return True
    return False


def update_record_by_id(rec_id: str, fields: dict) -> bool:
    """관리자용: user_id 확인 없이 ID로 기록의 여러 필드를 한 번에 수정.
    fields 키는 EDITABLE_RECORD_FIELDS 범위 내여야 함."""
    invalid = set(fields.keys()) - set(EDITABLE_RECORD_FIELDS.keys())
    if invalid:
        raise ValueError(f"수정 불가 필드: {invalid}")
    if not fields:
        return False
    ws = get_sheet("records")
    records = _safe_get_records(ws)
    for i, row in enumerate(records, start=2):
        if row["id"] == rec_id:
            # 필드마다 update_cell 을 부르면 3필드 수정 = API 왕복 3회였다.
            # batch_update 로 묶어 1회로 줄인다.
            ws.batch_update(
                [
                    {
                        "range": f"{_col_letter(EDITABLE_RECORD_FIELDS[f])}{i}",
                        "values": [[v]],
                    }
                    for f, v in fields.items()
                ],
                value_input_option="USER_ENTERED",
            )
            _invalidate_records_cache()
            return True
    return False


def get_recent_records(user_id: int | None = None, limit: int = 10) -> list[dict]:
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws)
    if user_id is not None:
        all_rows = [r for r in all_rows if str(r["user_id"]) == str(user_id)]
    return all_rows[-limit:][::-1]


# 봇이 쓰는 형식은 'YYYY-MM-DD HH:MM' 이지만, 시트를 사람이 직접 편집하면
# '2026-1-5', '2026/01/05', '2026. 1. 5' 같은 변형이 섞여 들어온다.
_DATE_PREFIX_RE = re.compile(r"^\s*(\d{4})\s*[-./]\s*(\d{1,2})")
_DATE_FULL_RE   = re.compile(r"^\s*(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})")


def _month_of(row: dict) -> tuple[int, int] | None:
    """기록 행의 date 에서 (연, 월) 추출. 인식할 수 없으면 None."""
    m = _DATE_PREFIX_RE.match(str(row.get("date", "")))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def get_records_for_month(year: int, month: int, user_id: int | None = None) -> list[dict]:
    return get_records_for_months([(year, month)], user_id)[(year, month)]


def _load_all_months() -> dict[tuple[int, int], list[dict]]:
    """시트를 한 번만 읽어 모든 월로 버킷팅하고, 각 월 캐시를 채웁니다."""
    global _records_full_load_ts
    ws = get_sheet("records")
    all_rows = _safe_get_records(ws) or []
    grouped: dict[tuple[int, int], list[dict]] = {}
    for row in all_rows:
        key = _month_of(row)
        if key is not None:
            grouped.setdefault(key, []).append(row)
    for key, rows in grouped.items():
        _set_records_cache(key[0], key[1], rows)
    with _cache_lock:
        _records_full_load_ts = time.monotonic()
    return grouped


def get_records_for_months(
    months: list[tuple[int, int]], user_id: int | None = None
) -> dict[tuple[int, int], list[dict]]:
    """여러 달의 기록을 **시트 조회 1회**로 가져옵니다.

    기존에는 연간 요약이 12개월 × '시트 전체 다운로드' = 12회 왕복이었고,
    트렌드는 6회였습니다. 한 번 읽어 월별로 나누면 1회로 끝납니다.
    (캐시가 전부 살아 있으면 조회 0회)
    """
    result: dict[tuple[int, int], list[dict]] = {}
    missing: list[tuple[int, int]] = []
    for key in months:
        cached = _get_records_from_cache(*key)
        if cached is None:
            missing.append(key)
        else:
            result[key] = cached

    if missing:
        # 단일 비행(single-flight): 캐시가 비어 있을 때 대시보드/연간/트렌드가
        # 동시에 들어오면 각자 시트 전체를 받아 같은 데이터를 중복 다운로드했다.
        # 한 스레드만 받아오게 하고 나머지는 그 결과(캐시)를 재사용한다.
        with _records_load_lock:
            # 락을 기다리는 동안 다른 스레드가 이미 전체를 받아왔다면 재조회하지 않는다.
            if not _full_load_is_fresh():
                _load_all_months()
            for key in missing:
                cached = _get_records_from_cache(*key)
                if cached is None:
                    # 전체 로드에 없던 달 = 기록이 없는 달. 캐시에 남겨 재조회를 막는다.
                    _set_records_cache(key[0], key[1], [])
                    cached = []
                result[key] = cached

    if user_id is not None:
        return {
            k: [r for r in v if str(r.get("user_id")) == str(user_id)]
            for k, v in result.items()
        }
    return {k: list(v) for k, v in result.items()}


def _date_of(row: dict) -> date | None:
    """기록 행의 date 에서 날짜(연·월·일) 추출. 인식할 수 없으면 None."""
    m = _DATE_FULL_RE.match(str(row.get("date", "")))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:      # 2026-02-31 같은 존재하지 않는 날짜
        return None


def get_records_in_range(
    start: date, end: date, user_id: int | None = None
) -> list[dict]:
    """[start, end] 기간의 기록을 반환합니다 (양끝 포함).

    달 경계를 넘는 기간도 정확히 처리하며, 월 캐시를 재사용하므로
    사용자마다 시트를 다시 내려받지 않습니다.
    """
    if end < start:
        start, end = end, start
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    grouped = get_records_for_months(months, user_id)
    result: list[dict] = []
    for key in months:
        for r in grouped.get(key, []):
            d = _date_of(r)
            if d is not None and start <= d <= end:
                result.append(r)
    return result


def get_records_for_week(
    year: int, month: int, day_start: int, day_end: int,
    user_id: int | None = None,
) -> list[dict]:
    """한 달 안의 [day_start, day_end] 구간 기록.

    달을 넘나드는 기간에는 get_records_in_range() 를 쓰세요.
    """
    last_day = calendar.monthrange(year, month)[1]
    start = date(year, month, max(1, min(day_start, last_day)))
    end   = date(year, month, max(1, min(day_end, last_day)))
    return get_records_in_range(start, end, user_id)


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
    return sum(to_number(r.get("amount")) for r in records if r.get("type") == record_type)


def monthly_breakdown(records: list[dict], record_type: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for r in records:
        if r.get("type") == record_type:
            cat = r.get("category") or "미분류"
            result[cat] = result.get(cat, 0) + to_number(r.get("amount"))
    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


def breakdown_by_user(records: list[dict], record_type: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for r in records:
        if r.get("type") == record_type:
            name = r.get("display_name") or "이름없음"
            result[name] = result.get(name, 0) + to_number(r.get("amount"))
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
    if _budgets_full_load_is_fresh(year, month):
        # 방금 전체를 받아왔는데 이 사용자가 없었다 = 예산 미설정. 재조회 불필요.
        _set_budgets_cache(user_id, year, month, {})
        return {}

    # 어차피 예산 시트 전체를 받아오므로, 그 김에 모든 사용자의 캐시를 채운다.
    # (자동 리포트가 가족 구성원마다 시트를 다시 내려받던 문제를 없앤다)
    ws = get_sheet("budgets")
    per_user: dict[str, dict[str, float]] = {}
    for row in _safe_get_records(ws) or []:
        if str(row.get("year")) == str(year) and str(row.get("month")) == str(month):
            uid = str(row.get("user_id"))
            per_user.setdefault(uid, {})[row.get("category")] = to_number(row.get("amount"))
    for uid, budgets in per_user.items():
        _set_budgets_cache(uid, year, month, budgets)
    with _cache_lock:
        _budgets_full_load[(year, month)] = time.monotonic()

    result = per_user.get(str(user_id), {})
    if str(user_id) not in per_user:
        # 예산이 없는 사용자도 캐시에 남겨 매번 재조회하지 않도록 한다.
        _set_budgets_cache(user_id, year, month, {})
    return dict(result)


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
