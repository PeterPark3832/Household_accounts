import os
import sys
import secrets
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from cachetools import TTLCache
import uvicorn
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

load_dotenv(os.path.join(PARENT_DIR, ".env"))
sys.path.insert(0, PARENT_DIR)

import sheets  # noqa: E402  (imported after path setup)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("dashboard")

KST = ZoneInfo("Asia/Seoul")

# Sheets API는 I/O 바운드 → 워커를 넉넉히 (기본값 4 → 16)
executor = ThreadPoolExecutor(max_workers=16)

# ── 응답 레벨 TTL 캐시 ────────────────────────────────────────────────────────
# sheets.py 내부 records 캐시(5분)보다 짧게 설정해 신선도 보장
_dash_cache   = TTLCache(maxsize=36, ttl=120)   # 월별 대시보드  2분
_annual_cache = TTLCache(maxsize=10, ttl=300)   # 연간 요약      5분
_trend_cache  = TTLCache(maxsize=12, ttl=120)   # 트렌드        2분

app = FastAPI(title="가계부 대시보드", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
security = HTTPBasic()

DASH_USER = os.environ.get("DASHBOARD_USER", "admin")
DASH_PASS = os.environ.get("DASHBOARD_PASS", "change_me_please")


def verify(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username.encode(), DASH_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), DASH_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


async def run_sync(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: fn(*args))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(verify)])
async def root(request: Request):
    now = datetime.now(KST)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_year": now.year,
        "current_month": now.month,
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/summary", dependencies=[Depends(verify)])
async def api_summary(year: int = Query(None), month: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    try:
        records = await run_sync(sheets.get_records_for_month, year, month)
        income  = sheets.monthly_total(records, "income")
        expense = sheets.monthly_total(records, "expense")
        return {
            "year": year, "month": month,
            "income": income, "expense": expense,
            "net": income - expense,
            "transaction_count": len(records),
        }
    except Exception as e:
        logger.error("api_summary(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/breakdown", dependencies=[Depends(verify)])
async def api_breakdown(
    year: int = Query(None),
    month: int = Query(None),
    record_type: str = Query("expense"),
):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    try:
        records  = await run_sync(sheets.get_records_for_month, year, month)
        breakdown = sheets.monthly_breakdown(records, record_type)
        return {"breakdown": breakdown, "type": record_type}
    except Exception as e:
        logger.error("api_breakdown(%d, %d, %s) failed: %s", year, month, record_type, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/trend", dependencies=[Depends(verify)])
async def api_trend(months: int = Query(6)):
    now = datetime.now(KST)
    cache_key = (now.year, now.month, months)

    if cache_key in _trend_cache:
        logger.info("trend cache HIT  %d-%02d (%dm)", now.year, now.month, months)
        return _trend_cache[cache_key]
    logger.info("trend cache MISS %d-%02d — fetching %d months", now.year, now.month, months)

    month_keys = []
    for i in range(months - 1, -1, -1):
        total = now.year * 12 + (now.month - 1) - i
        month_keys.append((total // 12, total % 12 + 1))

    async def safe_fetch(y, m):
        try:
            return await run_sync(sheets.get_records_for_month, y, m)
        except Exception as e:
            logger.warning("trend fetch %d-%02d failed: %s", y, m, e)
            return []

    try:
        all_recs = await asyncio.gather(*[safe_fetch(y, m) for y, m in month_keys])
        result = [
            {
                "year": y, "month": m,
                "label": f"{y}.{m:02d}",
                "income":  sheets.monthly_total(recs, "income"),
                "expense": sheets.monthly_total(recs, "expense"),
                "net":     sheets.monthly_total(recs, "income") - sheets.monthly_total(recs, "expense"),
            }
            for (y, m), recs in zip(month_keys, all_recs)
        ]
        _trend_cache[cache_key] = result
        return result
    except Exception as e:
        logger.error("api_trend failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/comparison", dependencies=[Depends(verify)])
async def api_comparison(year: int = Query(None), month: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    total = year * 12 + (month - 1) - 1
    py, pm = total // 12, total % 12 + 1
    try:
        cur_recs  = await run_sync(sheets.get_records_for_month, year, month)
        prev_recs = await run_sync(sheets.get_records_for_month, py, pm)

        def summary(recs):
            inc = sheets.monthly_total(recs, "income")
            exp = sheets.monthly_total(recs, "expense")
            return {"income": inc, "expense": exp, "net": inc - exp, "count": len(recs)}

        def pct(cur, prev):
            if prev == 0:
                return None
            return round((cur - prev) / abs(prev) * 100, 1)

        cur  = summary(cur_recs)
        prev = summary(prev_recs)
        return {
            "current":  cur,
            "previous": prev,
            "change": {
                "income":  pct(cur["income"],  prev["income"]),
                "expense": pct(cur["expense"], prev["expense"]),
                "net":     pct(cur["net"],     prev["net"]),
                "count":   pct(cur["count"],   prev["count"]),
            },
        }
    except Exception as e:
        logger.error("api_comparison(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/members", dependencies=[Depends(verify)])
async def api_members(year: int = Query(None), month: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    try:
        records         = await run_sync(sheets.get_records_for_month, year, month)
        income_by_user  = sheets.breakdown_by_user(records, "income")
        expense_by_user = sheets.breakdown_by_user(records, "expense")
        members: dict[str, dict] = {}
        for name, amt in income_by_user.items():
            members.setdefault(name, {"income": 0, "expense": 0})["income"] = amt
        for name, amt in expense_by_user.items():
            members.setdefault(name, {"income": 0, "expense": 0})["expense"] = amt
        return members
    except Exception as e:
        logger.error("api_members(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/transactions", dependencies=[Depends(verify)])
async def api_transactions(
    year: int = Query(None),
    month: int = Query(None),
    limit: int = Query(200),
):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    try:
        records = await run_sync(sheets.get_records_for_month, year, month)
        records.sort(key=lambda x: x.get("date", ""), reverse=True)
        return records[:limit]
    except Exception as e:
        logger.error("api_transactions(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/users", dependencies=[Depends(verify)])
async def api_users():
    try:
        return await run_sync(sheets.get_all_users)
    except Exception as e:
        logger.error("api_users failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/budgets", dependencies=[Depends(verify)])
async def api_budgets(
    user_id: str = Query(None),
    year: int = Query(None),
    month: int = Query(None),
):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    try:
        if not user_id:
            users = await run_sync(sheets.get_all_users)
            admins = [u for u in users if u.get("role") == "admin"]
            target = admins[0] if admins else (users[0] if users else None)
            user_id = target["user_id"] if target else None
        if not user_id:
            return []

        budgets = await run_sync(sheets.get_all_budgets_for_month, int(user_id), year, month)
        records = await run_sync(sheets.get_records_for_month, year, month)
        actuals = sheets.monthly_breakdown(records, "expense")

        result = []
        seen: set[str] = set()
        for category, budget_amount in budgets.items():
            actual = actuals.get(category, 0)
            pct = (actual / budget_amount * 100) if budget_amount > 0 else 0
            result.append({
                "category": category,
                "budget": budget_amount,
                "actual": actual,
                "percentage": round(pct, 1),
                "over_budget": actual > budget_amount,
            })
            seen.add(category)

        for cat, actual in actuals.items():
            if cat not in seen:
                result.append({
                    "category": cat, "budget": 0,
                    "actual": actual, "percentage": 100,
                    "over_budget": True,
                })

        result.sort(key=lambda x: x["actual"], reverse=True)
        return result
    except Exception as e:
        logger.error("api_budgets(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/dashboard", dependencies=[Depends(verify)])
async def api_dashboard(year: int = Query(None), month: int = Query(None)):
    """현재 월 핵심 데이터 — 병렬 fetch + TTL 캐시."""
    now = datetime.now(KST)
    year  = year  or now.year
    month = month or now.month

    cache_key = (year, month)
    if cache_key in _dash_cache:
        logger.info("dashboard cache HIT  %d-%02d", year, month)
        return _dash_cache[cache_key]
    logger.info("dashboard cache MISS %d-%02d — fetching Sheets", year, month)

    total_prev = year * 12 + (month - 1) - 1
    py, pm = total_prev // 12, total_prev % 12 + 1

    try:
        # ── 3개 병렬 fetch ─────────────────────────────────────
        users, cur_recs, prev_recs = await asyncio.gather(
            run_sync(sheets.get_all_users),
            run_sync(sheets.get_records_for_month, year, month),
            run_sync(sheets.get_records_for_month, py, pm),
        )

        admins  = [u for u in users if u.get("role") == "admin"]
        target  = admins[0] if admins else (users[0] if users else None)
        user_id = int(target["user_id"]) if target else None

        budgets_raw = await run_sync(sheets.get_all_budgets_for_month, user_id, year, month) \
                      if user_id else {}

        def pct(a, b):
            return round((a - b) / abs(b) * 100, 1) if b else None

        ci, ce  = sheets.monthly_total(cur_recs,  "income"), sheets.monthly_total(cur_recs,  "expense")
        pi_, pe = sheets.monthly_total(prev_recs, "income"), sheets.monthly_total(prev_recs, "expense")

        actuals = sheets.monthly_breakdown(cur_recs, "expense")
        budgets_list, seen = [], set()
        for cat, bamt in budgets_raw.items():
            actual = actuals.get(cat, 0)
            p = (actual / bamt * 100) if bamt > 0 else 0
            budgets_list.append({"category": cat, "budget": bamt, "actual": actual,
                                  "percentage": round(p, 1), "over_budget": actual > bamt})
            seen.add(cat)
        for cat, actual in actuals.items():
            if cat not in seen:
                budgets_list.append({"category": cat, "budget": 0, "actual": actual,
                                     "percentage": 100, "over_budget": True})
        budgets_list.sort(key=lambda x: x["actual"], reverse=True)

        inc_by = sheets.breakdown_by_user(cur_recs, "income")
        exp_by = sheets.breakdown_by_user(cur_recs, "expense")
        members: dict = {}
        for name, amt in inc_by.items():
            members.setdefault(name, {"income": 0, "expense": 0})["income"] = amt
        for name, amt in exp_by.items():
            members.setdefault(name, {"income": 0, "expense": 0})["expense"] = amt

        result = {
            "summary": {
                "year": year, "month": month,
                "income": ci, "expense": ce, "net": ci - ce,
                "transaction_count": len(cur_recs),
            },
            "comparison": {
                "current":  {"income": ci,  "expense": ce,  "net": ci - ce,  "count": len(cur_recs)},
                "previous": {"income": pi_, "expense": pe,  "net": pi_ - pe, "count": len(prev_recs)},
                "change": {
                    "income":  pct(ci,  pi_),
                    "expense": pct(ce,  pe),
                    "net":     pct(ci - ce, pi_ - pe),
                    "count":   pct(len(cur_recs), len(prev_recs)),
                },
            },
            "breakdown_expense":      sheets.monthly_breakdown(cur_recs,  "expense"),
            "breakdown_income":       sheets.monthly_breakdown(cur_recs,  "income"),
            "prev_breakdown_expense": sheets.monthly_breakdown(prev_recs, "expense"),
            "members":      members,
            "budgets":      budgets_list,
            "transactions": sorted(cur_recs, key=lambda x: x.get("date", ""), reverse=True)[:200],
        }
        _dash_cache[cache_key] = result
        return result
    except Exception as e:
        logger.error("api_dashboard(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/annual", dependencies=[Depends(verify)])
async def api_annual(year: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year

    if year in _annual_cache:
        logger.info("annual cache HIT  %d", year)
        return _annual_cache[year]
    logger.info("annual cache MISS %d — fetching 12 months", year)

    async def safe_fetch(m):
        try:
            return await run_sync(sheets.get_records_for_month, year, m)
        except Exception as e:
            logger.warning("annual fetch %d-%02d failed: %s", year, m, e)
            return []

    try:
        all_recs = await asyncio.gather(*[safe_fetch(m) for m in range(1, 13)])
        result = [
            {
                "month": m,
                "label": f"{m}월",
                "income":  sheets.monthly_total(recs, "income"),
                "expense": sheets.monthly_total(recs, "expense"),
                "net":     sheets.monthly_total(recs, "income") - sheets.monthly_total(recs, "expense"),
            }
            for m, recs in enumerate(all_recs, 1)
        ]
        _annual_cache[year] = result
        return result
    except Exception as e:
        logger.error("api_annual(%d) failed: %s", year, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/cache/clear", dependencies=[Depends(verify)])
async def clear_cache():
    """대시보드 캐시 수동 초기화 — 봇에서 새 거래 기록 직후 호출 가능."""
    _dash_cache.clear()
    _annual_cache.clear()
    _trend_cache.clear()
    logger.info("Response cache cleared manually")
    return {"status": "cleared", "message": "all caches cleared"}


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
