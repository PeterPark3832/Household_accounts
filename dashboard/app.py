import os
import sys
import time as _time
import secrets
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from cachetools import TTLCache
import uvicorn
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

load_dotenv(os.path.join(PARENT_DIR, ".env"))
sys.path.insert(0, PARENT_DIR)

import sheets  # noqa: E402  (imported after path setup)

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("dashboard")

KST = ZoneInfo("Asia/Seoul")

# Sheets API I/O bound => more workers
executor = ThreadPoolExecutor(max_workers=16)

# -- TTL caches ----------------------------------------------------------------
_dash_cache   = TTLCache(maxsize=36, ttl=120)
_annual_cache = TTLCache(maxsize=10, ttl=300)
_trend_cache  = TTLCache(maxsize=12, ttl=120)

app = FastAPI(title="가계부 대시보드", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

DASH_PASS = os.environ.get("DASHBOARD_PASS") or ""
if not DASH_PASS:
    raise RuntimeError("DASHBOARD_PASS 환경 변수를 반드시 설정하세요.")

# -- Token auth ----------------------------------------------------------------
_tokens: dict[str, float] = {}

def _new_token() -> str:
    tok = secrets.token_hex(32)
    _tokens[tok] = _time.time() + 86400  # 24 h
    return tok

def _check_token(tok: str) -> bool:
    exp = _tokens.get(tok)
    if not exp or _time.time() > exp:
        _tokens.pop(tok, None)
        return False
    return True

def verify_token(authorization: str = Header(default="")):
    tok = authorization.removeprefix("Bearer ").strip()
    if not _check_token(tok):
        raise HTTPException(status_code=401, detail="Unauthorized")


# -- Helpers -------------------------------------------------------------------
def build_budget_report(budgets: dict, actuals: dict) -> list:
    result: list[dict] = []
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


def pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


async def run_sync(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: fn(*args))


# -- Pages ---------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
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


@app.post("/api/auth")
async def api_auth(req: Request):
    body = await req.json()
    if secrets.compare_digest(body.get("password", ""), DASH_PASS):
        return {"token": _new_token()}
    raise HTTPException(status_code=401, detail="Invalid password")


# -- API -----------------------------------------------------------------------

@app.get("/api/summary", dependencies=[Depends(verify_token)])
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
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/breakdown", dependencies=[Depends(verify_token)])
async def api_breakdown(
    year: int = Query(None),
    month: int = Query(None),
    record_type: str = Query("expense", pattern="^(income|expense)$"),
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
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/trend", dependencies=[Depends(verify_token)])
async def api_trend(months: int = Query(6, ge=1, le=24)):
    now = datetime.now(KST)
    cache_key = (now.year, now.month, months)

    if cache_key in _trend_cache:
        logger.info("trend cache HIT  %d-%02d (%dm)", now.year, now.month, months)
        return _trend_cache[cache_key]
    logger.info("trend cache MISS %d-%02d -- fetching %d months", now.year, now.month, months)

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
        result = []
        for (y, m), recs in zip(month_keys, all_recs):
            inc = sheets.monthly_total(recs, "income")
            exp = sheets.monthly_total(recs, "expense")
            result.append({
                "year": y, "month": m,
                "label": f"{y}.{m:02d}",
                "income": inc, "expense": exp, "net": inc - exp,
            })
        _trend_cache[cache_key] = result
        return result
    except Exception as e:
        logger.error("api_trend failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/comparison", dependencies=[Depends(verify_token)])
async def api_comparison(year: int = Query(None), month: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year
    month = month or now.month
    total = year * 12 + (month - 1) - 1
    py, pm = total // 12, total % 12 + 1
    try:
        cur_recs, prev_recs = await asyncio.gather(
            run_sync(sheets.get_records_for_month, year, month),
            run_sync(sheets.get_records_for_month, py, pm),
        )

        def summary(recs):
            inc = sheets.monthly_total(recs, "income")
            exp = sheets.monthly_total(recs, "expense")
            return {"income": inc, "expense": exp, "net": inc - exp, "count": len(recs)}

        cur  = summary(cur_recs)
        prev = summary(prev_recs)
        return {
            "current":  cur,
            "previous": prev,
            "change": {
                "income":  pct_change(cur["income"],  prev["income"]),
                "expense": pct_change(cur["expense"], prev["expense"]),
                "net":     pct_change(cur["net"],     prev["net"]),
                "count":   pct_change(cur["count"],   prev["count"]),
            },
        }
    except Exception as e:
        logger.error("api_comparison(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/members", dependencies=[Depends(verify_token)])
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
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/transactions", dependencies=[Depends(verify_token)])
async def api_transactions(
    year: int = Query(None),
    month: int = Query(None),
    limit: int = Query(200, ge=1, le=500),
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
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/users", dependencies=[Depends(verify_token)])
async def api_users():
    try:
        return await run_sync(sheets.get_all_users)
    except Exception as e:
        logger.error("api_users failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/budgets", dependencies=[Depends(verify_token)])
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

        budgets, records = await asyncio.gather(
            run_sync(sheets.get_all_budgets_for_month, int(user_id), year, month),
            run_sync(sheets.get_records_for_month, year, month),
        )
        actuals = sheets.monthly_breakdown(records, "expense")

        return build_budget_report(budgets, actuals)
    except Exception as e:
        logger.error("api_budgets(%d, %d) failed: %s", year, month, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/dashboard", dependencies=[Depends(verify_token)])
async def api_dashboard(year: int = Query(None), month: int = Query(None)):
    now = datetime.now(KST)
    year  = year  or now.year
    month = month or now.month

    cache_key = (year, month)
    if cache_key in _dash_cache:
        logger.info("dashboard cache HIT  %d-%02d", year, month)
        return _dash_cache[cache_key]
    logger.info("dashboard cache MISS %d-%02d -- fetching Sheets", year, month)

    total_prev = year * 12 + (month - 1) - 1
    py, pm = total_prev // 12, total_prev % 12 + 1

    try:
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

        ci, ce  = sheets.monthly_total(cur_recs,  "income"), sheets.monthly_total(cur_recs,  "expense")
        pi_, pe = sheets.monthly_total(prev_recs, "income"), sheets.monthly_total(prev_recs, "expense")

        actuals = sheets.monthly_breakdown(cur_recs, "expense")
        budgets_list = build_budget_report(budgets_raw, actuals)

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
                    "income":  pct_change(ci,  pi_),
                    "expense": pct_change(ce,  pe),
                    "net":     pct_change(ci - ce, pi_ - pe),
                    "count":   pct_change(len(cur_recs), len(prev_recs)),
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
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.get("/api/annual", dependencies=[Depends(verify_token)])
async def api_annual(year: int = Query(None)):
    now = datetime.now(KST)
    year = year or now.year

    if year in _annual_cache:
        logger.info("annual cache HIT  %d", year)
        return _annual_cache[year]
    logger.info("annual cache MISS %d -- fetching 12 months", year)

    async def safe_fetch(m):
        try:
            return await run_sync(sheets.get_records_for_month, year, m)
        except Exception as e:
            logger.warning("annual fetch %d-%02d failed: %s", year, m, e)
            return []

    try:
        all_recs = await asyncio.gather(*[safe_fetch(m) for m in range(1, 13)])
        result = []
        for m, recs in enumerate(all_recs, 1):
            inc = sheets.monthly_total(recs, "income")
            exp = sheets.monthly_total(recs, "expense")
            result.append({
                "month": m, "label": f"{m}월",
                "income": inc, "expense": exp, "net": inc - exp,
            })
        _annual_cache[year] = result
        return result
    except Exception as e:
        logger.error("api_annual(%d) failed: %s", year, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": "데이터를 불러오지 못했습니다."})


@app.post("/api/cache/clear", dependencies=[Depends(verify_token)])
async def clear_cache(x_clear: str | None = Header(None, alias="X-Dashboard-Clear")):
    if x_clear != "1":
        raise HTTPException(status_code=400, detail="X-Dashboard-Clear: 1 헤더가 필요합니다.")
    _dash_cache.clear()
    _annual_cache.clear()
    _trend_cache.clear()
    logger.info("Response cache cleared manually")
    return {"status": "cleared", "message": "all caches cleared"}


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)