"""
💰 가계부 텔레그램 봇 v2
- Google Sheets 저장
- 가족 멀티유저
- 월간/주간 리포트 + 예산 알림 + 카테고리 차트
"""

import asyncio
import calendar
import functools
import io
import logging
import logging.handlers
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    BotCommand,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import sheets
import charts

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("BUDGET_BOT_TOKEN")
ADMIN_ID   = int(os.getenv("ADMIN_USER_ID", "0"))
KST        = ZoneInfo("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "budget_bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# ── 카테고리 ──────────────────────────────────────────────────────
INCOME_CATEGORIES = {
    "급여": "💼", "투자수익": "📈", "배당금": "💹",
    "부업": "🛠️", "보너스": "🎁", "환급/환불": "🔄", "기타수입": "💰",
}
EXPENSE_CATEGORIES = {
    "식비": "🍚", "카페/음료": "☕", "교통비": "🚌", "주거비": "🏠",
    "의료/건강": "🏥", "교육비": "📚", "쇼핑": "🛒", "문화/여가": "🎮",
    "통신비": "📱", "보험": "🛡️", "구독서비스": "📺",
    "경조사": "🎊", "기타지출": "💸",
}

# ── 대화 상태 ──────────────────────────────────────────────────────
(
    WAITING_CATEGORY,
    WAITING_AMOUNT,
    WAITING_MEMO,
    WAITING_BUDGET_CATEGORY,
    WAITING_BUDGET_AMOUNT,
    WAITING_REGISTER_NAME,
) = range(6)

CONV_TIMEOUT = 1800  # 30분 유휴 시 대화 자동 종료

# ── Phase 6: 스케줄러 전역 참조 (graceful shutdown용) ─────────────
_scheduler: AsyncIOScheduler | None = None


# ── Phase 6: 환경 변수 시작 검증 ──────────────────────────────────
def _validate_env():
    """필수 환경 변수 누락·형식 오류 시 즉시 종료합니다."""
    errors = []
    if not BOT_TOKEN:
        errors.append("BUDGET_BOT_TOKEN 이 설정되지 않았습니다.")
    elif len(BOT_TOKEN.split(":")) != 2:
        errors.append("BUDGET_BOT_TOKEN 형식이 올바르지 않습니다. (예: 1234567890:ABCdef…)")
    if not ADMIN_ID:
        errors.append("ADMIN_USER_ID 가 0 또는 미설정입니다.")
    if not os.getenv("SPREADSHEET_ID"):
        errors.append("SPREADSHEET_ID 가 설정되지 않았습니다.")
    creds_path = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")
    if not os.path.exists(creds_path):
        errors.append(f"Google 인증 파일을 찾을 수 없습니다: '{creds_path}'")
    if errors:
        for msg in errors:
            logger.critical(f"환경 설정 오류: {msg}")
        raise SystemExit("❌ .env 파일 또는 환경 변수를 확인하세요.")

# ── 비동기 래퍼 ────────────────────────────────────────────────────
async def run_sync(fn, *args, **kwargs):
    """동기 함수를 스레드 풀에서 실행해 이벤트 루프 블로킹을 방지합니다."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


# ── 유틸 ──────────────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)


def fmt(amount: float) -> str:
    return f"{int(amount):,}원"


def parse_amount(text: str) -> float | None:
    """입력 텍스트에서 숫자(정수/소수)만 추출합니다."""
    cleaned = re.sub(r"[^\d.]", "", text.strip())
    if not cleaned:
        return None
    try:
        value = float(cleaned)
        return value if value > 0 else None
    except ValueError:
        return None


def match_category(keyword: str) -> tuple[str, str] | None:
    """키워드로 카테고리를 검색합니다. (카테고리명, record_type) 반환."""
    keyword = keyword.strip()
    # 완전 일치 우선
    for cat in INCOME_CATEGORIES:
        if cat == keyword:
            return cat, "income"
    for cat in EXPENSE_CATEGORIES:
        if cat == keyword:
            return cat, "expense"
    # 부분 일치
    for cat in INCOME_CATEGORIES:
        if keyword in cat or cat in keyword:
            return cat, "income"
    for cat in EXPENSE_CATEGORIES:
        if keyword in cat or cat in keyword:
            return cat, "expense"
    return None


def prev_month(year: int, month: int) -> tuple[int, int]:
    """이전 달 (year, month) 반환."""
    total = year * 12 + month - 2
    y, rem = divmod(total, 12)
    return y, rem + 1


def build_cat_kb(categories: dict, prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, (name, emoji) in enumerate(categories.items()):
        row.append(InlineKeyboardButton(f"{emoji} {name}", callback_data=f"{prefix}:{name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ 취소", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 수입 기록"),   KeyboardButton("💸 지출 기록")],
            [KeyboardButton("📊 이번달 요약"), KeyboardButton("👨‍👩‍👧 가족 현황")],
            [KeyboardButton("📋 최근 내역"),   KeyboardButton("🎯 예산 설정")],
            [KeyboardButton("📈 차트 보기"),   KeyboardButton("📤 CSV 내보내기")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ── 예산 경고 메시지 생성 ─────────────────────────────────────────
async def _build_budget_warn(uid: int, category: str, emoji: str, year: int, month: int) -> str:
    """예산 대비 지출 비율에 따라 경고 메시지를 반환합니다."""
    budget = await run_sync(sheets.get_budget, uid, category, year, month)
    if not budget:
        return ""
    month_records = await run_sync(sheets.get_records_for_month, year, month, uid)
    spent = sheets.monthly_breakdown(month_records, "expense").get(category, 0)
    pct   = spent / budget * 100
    if pct >= 100:
        over = spent - budget
        return (
            f"\n\n🚨 *예산 초과!*\n"
            f"{emoji} {category}: {fmt(spent)} / {fmt(budget)}\n"
            f"초과 금액: *+{fmt(over)}* ({pct:.0f}%)"
        )
    elif pct >= 80:
        remain = budget - spent
        return (
            f"\n\n⚡ *예산 80% 도달*\n"
            f"{emoji} {category}: {fmt(spent)} / {fmt(budget)} ({pct:.0f}%)\n"
            f"남은 예산: *{fmt(remain)}*"
        )
    return ""


# ── 인증 & 등록 ────────────────────────────────────────────────────
async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    uid  = update.effective_user.id
    name = update.effective_user.full_name

    user = await run_sync(sheets.find_user, uid)
    if user is None:
        role = "admin" if uid == ADMIN_ID else "pending"
        await run_sync(sheets.register_user, uid, name, role)
        if uid == ADMIN_ID:
            return await run_sync(sheets.find_user, uid)
        await update.effective_message.reply_text(
            f"👋 *{name}*님, 가계부 앱 접근 요청이 전송됐습니다.\n"
            "관리자 승인 후 이용 가능합니다 ⏳",
            parse_mode="Markdown",
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🔔 *새 가입 요청*\n\n"
                    f"이름: {name}\n"
                    f"ID: `{uid}`\n\n"
                    f"승인: `/approve {uid}`\n"
                    f"거절: `/deny {uid}`"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"관리자 알림 발송 실패: {e}")
        return None

    if user.get("role") == "pending":
        await update.effective_message.reply_text("⏳ 아직 관리자 승인 대기 중입니다.")
        return None

    if user.get("role") == "denied":
        await update.effective_message.reply_text("🚫 접근이 거부된 계정입니다.")
        return None

    return user


# ── /start ──────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    await update.message.reply_text(
        f"👋 *{user['display_name']}*님, 안녕하세요!\n\n"
        "💰 *가족 가계부*에 오신 걸 환영합니다.\n"
        "아래 버튼으로 수입/지출을 기록하세요 📊\n\n"
        "⚡ 빠른 입력: `/q 식비 12000 편의점`\n"
        "_도움말: /help_",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )


# ── /help ──────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    is_admin_user = user.get("role") == "admin"
    text = (
        "📖 *가계부 봇 도움말*\n"
        "─────────────────────────\n\n"
        "📌 *버튼 메뉴*\n"
        "  💰 수입 기록 / 💸 지출 기록\n"
        "  📊 이번달 요약 / 👨‍👩‍👧 가족 현황\n"
        "  📋 최근 내역 / 🎯 예산 설정\n"
        "  📈 차트 보기 / 📤 CSV 내보내기\n\n"
        "📌 *기록 명령어*\n"
        "  `/q [카테고리] [금액] [메모]` — 빠른 1줄 입력\n"
        "  `/edit [번호] [필드] [값]` — 기록 수정\n"
        "  `/del [번호]` — 기록 삭제\n\n"
        "📌 *조회 명령어*\n"
        "  `/summary [월]` — 월 요약 (예: `/summary 3`)\n"
        "  `/budgets` — 이번달 예산 현황\n"
        "  `/stats` — 이번달 지출 통계\n"
        "  `/search [키워드]` — 기록 검색\n"
        "  `/backup` — 전체 기간 CSV 내보내기\n\n"
        "📌 *설정 명령어*\n"
        "  `/copybudget` — 지난달 예산 복사\n"
        "  `/rename [이름]` — 표시 이름 변경\n"
        "  `/cancel` — 진행 중인 입력 취소\n\n"
        "📌 */edit 필드 안내*\n"
        "  `amount` — 금액 수정\n"
        "  `memo` — 메모 수정\n"
        "  `category` — 카테고리 수정\n"
        "  예) `/edit A1B2C3D4 amount 15000`\n"
        "  예) `/edit A1B2C3D4 memo 스타벅스아아`\n"
    )
    if is_admin_user:
        text += (
            "\n📌 *관리자 명령어*\n"
            "  `/approve [user_id]` — 가입 승인\n"
            "  `/deny [user_id]` — 가입 거절\n"
            "  `/users` — 구성원 목록 조회\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb())


# ── /q 빠른 입력 ──────────────────────────────────────────────────
async def cmd_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/q [카테고리] [금액] [메모(선택)] — 대화 없이 1줄로 기록."""
    user = await ensure_user(update, context)
    if not user:
        return

    if len(context.args) < 2:
        cats_income  = " / ".join(INCOME_CATEGORIES.keys())
        cats_expense = " / ".join(EXPENSE_CATEGORIES.keys())
        await update.message.reply_text(
            "⚡ *빠른 입력 사용법*\n\n"
            "`/q [카테고리] [금액] [메모(선택)]`\n\n"
            "*예시*\n"
            "  `/q 식비 12000 편의점`\n"
            "  `/q 급여 3000000`\n"
            "  `/q 카페 4500 아아`\n\n"
            f"*수입 카테고리:* {cats_income}\n"
            f"*지출 카테고리:* {cats_expense}",
            parse_mode="Markdown",
        )
        return

    cat_keyword   = context.args[0]
    amount_text   = context.args[1]
    memo          = " ".join(context.args[2:]) if len(context.args) > 2 else ""

    # 카테고리 매칭
    match = match_category(cat_keyword)
    if match is None:
        all_cats = list(INCOME_CATEGORIES.keys()) + list(EXPENSE_CATEGORIES.keys())
        await update.message.reply_text(
            f"⚠️ *'{cat_keyword}'* 카테고리를 찾을 수 없습니다.\n\n"
            f"사용 가능: {', '.join(all_cats)}",
            parse_mode="Markdown",
        )
        return

    matched_cat, rec_type = match
    amount = parse_amount(amount_text)
    if amount is None:
        await update.message.reply_text("⚠️ 올바른 금액을 입력하세요. (예: 12000)")
        return

    now    = now_kst()
    rec_id = await run_sync(
        sheets.insert_record,
        user_id=update.effective_user.id,
        display_name=user["display_name"],
        record_type=rec_type,
        category=matched_cat,
        amount=amount,
        memo=memo,
        recorded_at=now,
    )

    emoji      = (INCOME_CATEGORIES if rec_type == "income" else EXPENSE_CATEGORIES).get(matched_cat, "")
    type_label = "💰 수입" if rec_type == "income" else "💸 지출"
    budget_warn = ""
    if rec_type == "expense":
        budget_warn = await _build_budget_warn(update.effective_user.id, matched_cat, emoji, now.year, now.month)

    await update.message.reply_text(
        f"⚡ *빠른 입력 완료* `[{rec_id}]`\n\n"
        f"유형: {type_label}\n"
        f"카테고리: {emoji} {matched_cat}\n"
        f"금액: *{fmt(amount)}*\n"
        f"메모: {memo or '없음'}"
        f"{budget_warn}",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )


# ── /summary [year] [month] ────────────────────────────────────────
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/summary, /summary 3, /summary 2024 12"""
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    year, month = now.year, now.month

    if len(context.args) == 1:
        try:
            month = int(context.args[0])
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "사용법: `/summary` / `/summary 3` / `/summary 2024 12`",
                parse_mode="Markdown",
            )
            return
    elif len(context.args) >= 2:
        try:
            year  = int(context.args[0])
            month = int(context.args[1])
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "사용법: `/summary [년] [월]` (예: `/summary 2024 12`)",
                parse_mode="Markdown",
            )
            return

    await _send_summary(update.effective_message, update.effective_user.id, user["display_name"], year, month)


# ── /stats ────────────────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """이번달 지출 통계: 일평균, 월말 예상, 최대 단건, 가장 잦은 카테고리."""
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    uid = update.effective_user.id

    records = await run_sync(sheets.get_records_for_month, now.year, now.month, uid)
    expenses = [r for r in records if r["type"] == "expense"]

    if not expenses:
        await update.message.reply_text(
            f"📊 {now.month}월 지출 기록이 없습니다.", reply_markup=main_kb()
        )
        return

    amounts        = [float(r["amount"]) for r in expenses]
    total_ex       = sum(amounts)
    days_passed    = now.day
    days_in_month  = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - days_passed
    avg_daily      = total_ex / days_passed
    projected_eom  = avg_daily * days_in_month  # 이 속도면 월말 예상

    max_r      = max(expenses, key=lambda r: float(r["amount"]))
    cat_counts = Counter(r["category"] for r in expenses)
    top_cat, top_cnt = cat_counts.most_common(1)[0]
    top_emoji  = EXPENSE_CATEGORIES.get(top_cat, "💸")

    # 예산 대비 소진율
    budgets  = await run_sync(sheets.get_all_budgets_for_month, uid, now.year, now.month)
    ex_break = sheets.monthly_breakdown(records, "expense")
    budget_lines = ""
    for cat, budget in budgets.items():
        spent = ex_break.get(cat, 0)
        pct   = spent / budget * 100 if budget > 0 else 0
        bar   = "🟥" if pct >= 100 else ("🟨" if pct >= 80 else "🟩")
        budget_lines += f"  {bar} {cat}: {pct:.0f}%\n"

    max_emoji = EXPENSE_CATEGORIES.get(max_r["category"], "💸")
    text = (
        f"📊 *{now.month}월 지출 통계*\n"
        f"{'─'*24}\n\n"
        f"💸 *총 지출:* {fmt(total_ex)}\n"
        f"📅 경과: {days_passed}일 / {days_in_month}일 (남은 {days_remaining}일)\n"
        f"📊 *일평균 지출:* {fmt(avg_daily)}\n"
        f"🔮 *이 속도면 월말 예상:* {fmt(projected_eom)}\n\n"
        f"🏆 *최대 단건*\n"
        f"  {max_emoji} {max_r['category']} — *{fmt(float(max_r['amount']))}*\n"
        f"  {str(max_r['date'])[:10]} | {max_r.get('memo') or '메모 없음'}\n\n"
        f"🔁 *가장 잦은 지출:* {top_emoji} {top_cat} ({top_cnt}회)"
    )
    if budget_lines:
        text += f"\n\n🎯 *예산 소진율:*\n{budget_lines}"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb())


# ── /copybudget ───────────────────────────────────────────────────
async def cmd_copybudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """지난달 예산을 이번달로 복사합니다. 이미 설정된 항목은 건너뜁니다."""
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    prev_y, prev_m = prev_month(now.year, now.month)

    copied, skipped = await run_sync(
        sheets.copy_budgets_from_month,
        update.effective_user.id, user["display_name"],
        prev_y, prev_m, now.year, now.month,
    )

    if not copied and not skipped:
        await update.message.reply_text(
            f"📭 {prev_m}월에 설정된 예산이 없습니다.\n'🎯 예산 설정' 버튼으로 직접 설정해보세요!",
            reply_markup=main_kb(),
        )
        return

    lines = f"🔄 *{prev_m}월 예산 → {now.month}월 복사*\n\n"
    if copied:
        budgets_src = await run_sync(sheets.get_all_budgets_for_month, update.effective_user.id, prev_y, prev_m)
        lines += "✅ *복사된 항목:*\n"
        for cat in copied:
            emoji = EXPENSE_CATEGORIES.get(cat, "💸")
            lines += f"  {emoji} {cat}: {fmt(budgets_src.get(cat, 0))}\n"
    if skipped:
        lines += "\n⏭ *건너뜀 (이미 설정됨):*\n"
        for cat in skipped:
            lines += f"  · {cat}\n"

    await update.message.reply_text(lines, parse_mode="Markdown", reply_markup=main_kb())


# ── 관리자 명령어 ──────────────────────────────────────────────────
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("사용법: /approve {user_id}")
        return
    target_id = int(context.args[0])
    ok = await run_sync(sheets.set_user_role, target_id, "member")
    if ok:
        await update.message.reply_text(f"✅ {target_id} 승인 완료!")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="✅ *가계부 앱 접근이 승인됐습니다!*\n/start 로 시작하세요 🎉",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"승인 알림 발송 실패 ({target_id}): {e}")
    else:
        await update.message.reply_text("⚠️ 유저를 찾을 수 없습니다.")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        return
    target_id = int(context.args[0])
    await run_sync(sheets.set_user_role, target_id, "denied")
    await update.message.reply_text(f"🚫 {target_id} 거절 처리됨.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    if not await run_sync(sheets.is_admin, update.effective_user.id):
        return
    all_users = await run_sync(sheets.get_all_users)
    lines = "👥 *등록된 가족 구성원*\n\n"
    for u in all_users:
        role_icon = {"admin": "👑", "member": "👤", "pending": "⏳", "denied": "🚫"}.get(u["role"], "❓")
        lines += f"{role_icon} {u['display_name']} (`{u['user_id']}`)\n"
    await update.message.reply_text(lines, parse_mode="Markdown")


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    if not context.args:
        await update.message.reply_text("사용법: /rename {새이름}")
        return
    new_name = " ".join(context.args)
    await run_sync(sheets.update_display_name, update.effective_user.id, new_name)
    await update.message.reply_text(f"✅ 이름이 *{new_name}*으로 변경됐습니다!", parse_mode="Markdown")


# ── /budgets ──────────────────────────────────────────────────────
async def cmd_budgets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    uid = update.effective_user.id
    budgets, records = await asyncio.gather(
        run_sync(sheets.get_all_budgets_for_month, uid, now.year, now.month),
        run_sync(sheets.get_records_for_month, now.year, now.month, uid),
    )
    if not budgets:
        await update.message.reply_text(
            f"🎯 {now.month}월 설정된 예산이 없습니다.\n"
            "'🎯 예산 설정' 버튼 또는 `/copybudget` 으로 이전달 예산을 복사하세요!",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return
    ex_break = sheets.monthly_breakdown(records, "expense")
    lines    = f"🎯 *{now.month}월 예산 현황*\n\n"
    for cat, budget in sorted(budgets.items()):
        emoji  = EXPENSE_CATEGORIES.get(cat, "💸")
        spent  = ex_break.get(cat, 0)
        pct    = (spent / budget * 100) if budget > 0 else 0
        bar    = "🟥" if pct >= 100 else ("🟨" if pct >= 80 else "🟩")
        remain = budget - spent
        remain_str = f"남은 {fmt(remain)}" if remain >= 0 else f"초과 {fmt(-remain)}"
        lines += f"{bar} {emoji} *{cat}*\n  {fmt(spent)} / {fmt(budget)} ({pct:.0f}%) — {remain_str}\n\n"
    await update.message.reply_text(lines, parse_mode="Markdown", reply_markup=main_kb())


# ── 수입/지출 기록 흐름 ─────────────────────────────────────────
async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return ConversationHandler.END
    context.user_data.update({"record_type": "income", "user": user})
    await update.message.reply_text(
        "💰 *수입 카테고리를 선택하세요*",
        parse_mode="Markdown",
        reply_markup=build_cat_kb(INCOME_CATEGORIES, "cat"),
    )
    return WAITING_CATEGORY


async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return ConversationHandler.END
    context.user_data.update({"record_type": "expense", "user": user})
    await update.message.reply_text(
        "💸 *지출 카테고리를 선택하세요*",
        parse_mode="Markdown",
        reply_markup=build_cat_kb(EXPENSE_CATEGORIES, "cat"),
    )
    return WAITING_CATEGORY


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ 취소됐습니다.")
        return ConversationHandler.END
    cat = query.data.split(":", 1)[1]
    context.user_data["category"] = cat
    rec_type = context.user_data["record_type"]
    emoji = (INCOME_CATEGORIES if rec_type == "income" else EXPENSE_CATEGORIES).get(cat, "")
    await query.edit_message_text(
        f"{emoji} *{cat}* 선택됨\n\n금액을 입력하세요 (예: 50000)",
        parse_mode="Markdown",
    )
    return WAITING_AMOUNT


async def on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("⚠️ 올바른 금액을 입력하세요 (예: 50000)")
        return WAITING_AMOUNT
    context.user_data["amount"] = amount
    await update.message.reply_text(
        "📝 메모를 입력하세요\n_(없으면 `-` 입력)_",
        parse_mode="Markdown",
    )
    return WAITING_MEMO


async def on_memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memo = update.message.text.strip()
    if memo == "-":
        memo = ""
    ud       = context.user_data
    user     = ud["user"]
    rec_type = ud["record_type"]
    category = ud["category"]
    amount   = ud["amount"]
    now      = now_kst()

    rec_id = await run_sync(
        sheets.insert_record,
        user_id=update.effective_user.id,
        display_name=user["display_name"],
        record_type=rec_type,
        category=category,
        amount=amount,
        memo=memo,
        recorded_at=now,
    )

    emoji      = (INCOME_CATEGORIES if rec_type == "income" else EXPENSE_CATEGORIES).get(category, "")
    type_label = "💰 수입" if rec_type == "income" else "💸 지출"
    budget_warn = ""
    if rec_type == "expense":
        budget_warn = await _build_budget_warn(update.effective_user.id, category, emoji, now.year, now.month)

    await update.message.reply_text(
        f"✅ *기록 완료* `[{rec_id}]`\n\n"
        f"유형: {type_label}\n"
        f"카테고리: {emoji} {category}\n"
        f"금액: *{fmt(amount)}*\n"
        f"날짜: {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"메모: {memo or '없음'}"
        f"{budget_warn}",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )
    return ConversationHandler.END


async def conv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """대화 30분 유휴 시 자동 종료 핸들러."""
    await update.effective_message.reply_text(
        "⏰ 30분 동안 입력이 없어 취소됐습니다.",
        reply_markup=main_kb(),
    )
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 취소됐습니다.", reply_markup=main_kb())
    return ConversationHandler.END


# ── 이번달 요약 (버튼) ────────────────────────────────────────────
async def monthly_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    await _send_summary(
        update.effective_message, update.effective_user.id,
        user["display_name"], now.year, now.month,
    )


async def _send_summary(message, user_id: int, display_name: str, year: int, month: int):
    records, budgets = await asyncio.gather(
        run_sync(sheets.get_records_for_month, year, month, user_id),
        run_sync(sheets.get_all_budgets_for_month, user_id, year, month),
    )
    total_in = sheets.monthly_total(records, "income")
    total_ex = sheets.monthly_total(records, "expense")
    balance  = total_in - total_ex
    in_break = sheets.monthly_breakdown(records, "income")
    ex_break = sheets.monthly_breakdown(records, "expense")

    in_lines = ""
    for cat, amt in in_break.items():
        in_lines += f"  {INCOME_CATEGORIES.get(cat, '💰')} {cat}: {fmt(amt)}\n"

    ex_lines = ""
    for cat, amt in ex_break.items():
        emoji  = EXPENSE_CATEGORIES.get(cat, "💸")
        budget = budgets.get(cat)
        if budget:
            pct = amt / budget * 100
            bar = "🟥" if pct >= 100 else ("🟨" if pct >= 80 else "🟩")
            ex_lines += f"  {emoji} {cat}: {fmt(amt)} / {fmt(budget)} {bar}\n"
        else:
            ex_lines += f"  {emoji} {cat}: {fmt(amt)}\n"

    icon = "📈" if balance >= 0 else "📉"
    text = (
        f"📊 *{year}년 {month}월 — {display_name}님 가계부*\n"
        f"{'─'*28}\n\n"
        f"💰 *총 수입* {fmt(total_in)}\n"
        f"{in_lines or '  (없음)\n'}\n"
        f"💸 *총 지출* {fmt(total_ex)}\n"
        f"{ex_lines or '  (없음)\n'}\n"
        f"{'─'*28}\n"
        f"{icon} *순수지*: {fmt(balance)}"
    )
    await message.reply_text(text, parse_mode="Markdown", reply_markup=main_kb())


# ── 가족 전체 현황 ────────────────────────────────────────────────
async def family_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    now = now_kst()
    all_records = await run_sync(sheets.get_records_for_month, now.year, now.month)

    total_in     = sheets.monthly_total(all_records, "income")
    total_ex     = sheets.monthly_total(all_records, "expense")
    by_member_ex = sheets.breakdown_by_user(all_records, "expense")
    by_member_in = sheets.breakdown_by_user(all_records, "income")

    lines = (
        f"👨‍👩‍👧 *{now.year}년 {now.month}월 가족 현황*\n"
        f"{'─'*28}\n\n"
        f"💰 가족 총 수입: {fmt(total_in)}\n"
        f"💸 가족 총 지출: {fmt(total_ex)}\n"
        f"📈 가족 순수지: {fmt(total_in - total_ex)}\n\n"
        f"*구성원별 지출:*\n"
    )
    for name, amt in by_member_ex.items():
        lines += f"  👤 {name}: {fmt(amt)}\n"
    lines += "\n*구성원별 수입:*\n"
    for name, amt in by_member_in.items():
        lines += f"  👤 {name}: {fmt(amt)}\n"

    await update.message.reply_text(lines, parse_mode="Markdown", reply_markup=main_kb())


# ── 차트 보기 ────────────────────────────────────────────────────
CHART_MENU_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🥧 지출 파이차트",   callback_data="chart:pie_expense"),
        InlineKeyboardButton("🥧 수입 파이차트",   callback_data="chart:pie_income"),
    ],
    [
        InlineKeyboardButton("🎯 예산 대비 현황",  callback_data="chart:budget"),
        InlineKeyboardButton("📅 월별 트렌드",     callback_data="chart:trend"),
    ],
    [
        InlineKeyboardButton("👨‍👩‍👧 가족 지출 비교", callback_data="chart:family"),
    ],
    [InlineKeyboardButton("❌ 닫기", callback_data="chart:close")],
])


async def chart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    context.user_data["chart_user"] = user
    await update.message.reply_text(
        "📈 *어떤 차트를 볼까요?*",
        parse_mode="Markdown",
        reply_markup=CHART_MENU_KB,
    )


async def on_chart_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_text("차트 메뉴를 닫았습니다.")
        return

    user = context.user_data.get("chart_user") or await run_sync(sheets.find_user, update.effective_user.id)
    now  = now_kst()
    uid  = update.effective_user.id

    await query.edit_message_text("⏳ 차트 생성 중...")

    try:
        img = None

        if action == "pie_expense":
            records = await run_sync(sheets.get_records_for_month, now.year, now.month, uid)
            img     = await run_sync(charts.pie_chart, f"{now.month}월 지출 분석",
                                     sheets.monthly_breakdown(records, "expense"))

        elif action == "pie_income":
            records = await run_sync(sheets.get_records_for_month, now.year, now.month, uid)
            img     = await run_sync(charts.pie_chart, f"{now.month}월 수입 분석",
                                     sheets.monthly_breakdown(records, "income"))

        elif action == "budget":
            records, budgets = await asyncio.gather(
                run_sync(sheets.get_records_for_month, now.year, now.month, uid),
                run_sync(sheets.get_all_budgets_for_month, uid, now.year, now.month),
            )
            ex_data = sheets.monthly_breakdown(records, "expense")
            cats    = list(set(ex_data.keys()) | set(budgets.keys()))
            img     = await run_sync(
                charts.bar_chart_budget, f"{now.month}월 예산 대비 지출",
                cats, [ex_data.get(c, 0) for c in cats], [budgets.get(c, 0) for c in cats],
            )

        elif action == "trend":
            def _month_info(delta: int) -> tuple[int, int]:
                total = now.year * 12 + now.month - 1 - delta
                y, rem = divmod(total, 12)
                return y, rem + 1

            month_params = [_month_info(d) for d in range(5, -1, -1)]
            all_recs = await asyncio.gather(
                *[run_sync(sheets.get_records_for_month, y, m, uid) for y, m in month_params]
            )
            months, incomes, expenses = [], [], []
            prev_year = None
            for (y, m), recs in zip(month_params, all_recs):
                label = f"{m}월" if (prev_year is None or y == prev_year) else f"{y}.{m}월"
                prev_year = y
                months.append(label)
                incomes.append(sheets.monthly_total(recs, "income"))
                expenses.append(sheets.monthly_total(recs, "expense"))
            img = await run_sync(charts.bar_chart_monthly_trend, "최근 6개월 수입/지출 트렌드",
                                 months, incomes, expenses)

        elif action == "family":
            all_records = await run_sync(sheets.get_records_for_month, now.year, now.month)
            by_member   = sheets.breakdown_by_user(all_records, "expense")
            img = await run_sync(
                charts.bar_chart_by_member, f"{now.month}월 가족 지출 비교",
                list(by_member.keys()), list(by_member.values()),
            )

        if img:
            await query.message.reply_photo(photo=io.BytesIO(img))
        else:
            await query.message.reply_text("📭 데이터가 없습니다.")

    except Exception as e:
        logger.error(f"차트 생성 오류: {e}", exc_info=True)
        await query.message.reply_text(f"⚠️ 차트 생성 실패: {e}")


# ── 최근 내역 ──────────────────────────────────────────────────────
async def recent_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    uid  = update.effective_user.id
    rows = await run_sync(sheets.get_recent_records, uid, 10)
    if not rows:
        await update.message.reply_text("📋 아직 기록이 없습니다.", reply_markup=main_kb())
        return
    lines = "📋 *최근 10건*\n\n"
    for r in rows:
        cats  = INCOME_CATEGORIES if r["type"] == "income" else EXPENSE_CATEGORIES
        emoji = cats.get(r["category"], "")
        icon  = "➕" if r["type"] == "income" else "➖"
        dt    = str(r["date"])[:10]
        lines += f"{icon} {dt} {emoji}{r['category']} *{fmt(float(r['amount']))}*"
        if r.get("memo"):
            lines += f" _{r['memo']}_"
        lines += f" `[{r['id']}]`\n"
    lines += "\n_삭제: /del {{번호}}_"
    await update.message.reply_text(lines, parse_mode="Markdown", reply_markup=main_kb())


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return
    if not context.args:
        await update.message.reply_text("사용법: /del {번호} (예: /del A1B2C3D4)")
        return
    ok = await run_sync(sheets.delete_record, update.effective_user.id, context.args[0].upper())
    await update.message.reply_text("🗑️ 삭제 완료!" if ok else "⚠️ 해당 기록을 찾을 수 없습니다.")


# ── /edit [id] [field] [value] ────────────────────────────────────
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/edit [번호] [필드] [값]
    필드: amount | memo | category
    예시: /edit A1B2C3D4 amount 15000
          /edit A1B2C3D4 memo 스타벅스아아
          /edit A1B2C3D4 category 카페/음료
    """
    user = await ensure_user(update, context)
    if not user:
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "사용법: `/edit [번호] [필드] [값]`\n\n"
            "필드: `amount` / `memo` / `category`\n"
            "예시: `/edit A1B2C3D4 amount 15000`\n"
            "예시: `/edit A1B2C3D4 memo 스타벅스아아`",
            parse_mode="Markdown",
        )
        return

    rec_id = context.args[0].upper()
    field  = context.args[1].lower()
    value  = " ".join(context.args[2:])

    if field not in ("amount", "memo", "category"):
        await update.message.reply_text(
            "⚠️ 수정 가능한 필드: `amount` / `memo` / `category`",
            parse_mode="Markdown",
        )
        return

    # amount 유효성 검사
    if field == "amount":
        parsed = parse_amount(value)
        if parsed is None:
            await update.message.reply_text("⚠️ 올바른 금액을 입력하세요. (예: 15000)")
            return
        value = str(int(parsed))

    # category 유효성 검사
    if field == "category":
        all_cats = list(INCOME_CATEGORIES.keys()) + list(EXPENSE_CATEGORIES.keys())
        if value not in all_cats:
            await update.message.reply_text(
                f"⚠️ 올바른 카테고리를 입력하세요.\n"
                f"수입: {', '.join(INCOME_CATEGORIES.keys())}\n"
                f"지출: {', '.join(EXPENSE_CATEGORIES.keys())}",
            )
            return

    ok = await run_sync(sheets.update_record, update.effective_user.id, rec_id, field, value)
    if ok:
        field_label = {"amount": "금액", "memo": "메모", "category": "카테고리"}[field]
        display_val = fmt(float(value)) if field == "amount" else value
        await update.message.reply_text(
            f"✏️ 수정 완료 `[{rec_id}]`\n{field_label}: *{display_val}*",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("⚠️ 해당 기록을 찾을 수 없습니다. 번호를 확인하세요.")


# ── /search [키워드] ──────────────────────────────────────────────
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search [키워드] — 메모·카테고리에서 검색"""
    user = await ensure_user(update, context)
    if not user:
        return

    if not context.args:
        await update.message.reply_text(
            "사용법: `/search [키워드]`\n예시: `/search 스타벅스`",
            parse_mode="Markdown",
        )
        return

    keyword = " ".join(context.args)
    rows    = await run_sync(sheets.search_records, update.effective_user.id, keyword, 20)

    if not rows:
        await update.message.reply_text(
            f"🔍 *'{keyword}'* 검색 결과가 없습니다.",
            parse_mode="Markdown",
            reply_markup=main_kb(),
        )
        return

    lines = f"🔍 *'{keyword}'* 검색 결과 ({len(rows)}건)\n\n"
    for r in rows:
        cats  = INCOME_CATEGORIES if r["type"] == "income" else EXPENSE_CATEGORIES
        emoji = cats.get(r["category"], "")
        icon  = "➕" if r["type"] == "income" else "➖"
        dt    = str(r["date"])[:10]
        lines += f"{icon} {dt} {emoji}{r['category']} *{fmt(float(r['amount']))}*"
        if r.get("memo"):
            lines += f" _{r['memo']}_"
        lines += f" `[{r['id']}]`\n"

    await update.message.reply_text(lines, parse_mode="Markdown", reply_markup=main_kb())


# ── /backup ───────────────────────────────────────────────────────
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backup — 전체 기간 기록을 CSV로 내보내기"""
    import csv
    import io as _io

    user = await ensure_user(update, context)
    if not user:
        return

    await update.message.reply_text("⏳ 전체 기록을 불러오는 중…")

    uid     = update.effective_user.id
    records = await run_sync(sheets.get_all_records_for_user, uid)

    if not records:
        await update.message.reply_text("📤 기록이 없습니다.", reply_markup=main_kb())
        return

    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["번호", "유형", "카테고리", "금액", "메모", "날짜"])
    for r in records:
        writer.writerow([
            r["id"],
            "수입" if r["type"] == "income" else "지출",
            r["category"],
            int(float(r["amount"])),
            r.get("memo", ""),
            str(r["date"]),
        ])
    output.seek(0)

    now      = now_kst()
    filename = f"가계부_전체_{user['display_name']}_{now.strftime('%Y%m%d')}.csv"
    await update.message.reply_document(
        document=output.getvalue().encode("utf-8-sig"),
        filename=filename,
        caption=f"📤 전체 기간 가계부 — {user['display_name']} ({len(records)}건)",
        reply_markup=main_kb(),
    )


# ── 예산 설정 흐름 ─────────────────────────────────────────────────
async def budget_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)
    if not user:
        return ConversationHandler.END
    context.user_data["budget_user"] = user
    await update.message.reply_text(
        "🎯 *예산을 설정할 카테고리를 선택하세요*",
        parse_mode="Markdown",
        reply_markup=build_cat_kb(EXPENSE_CATEGORIES, "budget"),
    )
    return WAITING_BUDGET_CATEGORY


async def on_budget_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ 취소됐습니다.")
        return ConversationHandler.END
    cat = query.data.split(":", 1)[1]
    context.user_data["budget_cat"] = cat
    emoji = EXPENSE_CATEGORIES.get(cat, "")
    await query.edit_message_text(
        f"{emoji} *{cat}* 이번달 예산을 입력하세요 (예: 300000)",
        parse_mode="Markdown",
    )
    return WAITING_BUDGET_AMOUNT


async def on_budget_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("⚠️ 올바른 금액을 입력하세요")
        return WAITING_BUDGET_AMOUNT
    user = context.user_data["budget_user"]
    cat  = context.user_data["budget_cat"]
    now  = now_kst()
    await run_sync(sheets.set_budget, update.effective_user.id, user["display_name"], cat, amount, now.year, now.month)
    emoji = EXPENSE_CATEGORIES.get(cat, "")
    await update.message.reply_text(
        f"✅ {emoji} *{cat}* 예산: *{fmt(amount)}* 설정 완료!",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )
    return ConversationHandler.END


# ── CSV 내보내기 ──────────────────────────────────────────────────
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import csv
    import io as _io
    user = await ensure_user(update, context)
    if not user:
        return
    now     = now_kst()
    uid     = update.effective_user.id
    records = await run_sync(sheets.get_records_for_month, now.year, now.month, uid)
    if not records:
        await update.message.reply_text("📤 이번달 기록이 없습니다.")
        return
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["번호", "유형", "카테고리", "금액", "메모", "날짜"])
    for r in records:
        writer.writerow([
            r["id"],
            "수입" if r["type"] == "income" else "지출",
            r["category"],
            int(float(r["amount"])),
            r.get("memo", ""),
            str(r["date"]),
        ])
    output.seek(0)
    filename = f"가계부_{now.year}{now.month:02d}_{user['display_name']}.csv"
    await update.message.reply_document(
        document=output.getvalue().encode("utf-8-sig"),
        filename=filename,
        caption=f"📤 {now.year}년 {now.month}월 가계부 — {user['display_name']}",
    )


# ── 자동 스케줄 ────────────────────────────────────────────────────
async def _auto_report(app: Application, year: int, month: int, label: str):
    users = [u for u in await run_sync(sheets.get_all_users) if u.get("role") in ("admin", "member")]
    for u in users:
        try:
            uid  = int(u["user_id"])
            name = u["display_name"]
            records, budgets = await asyncio.gather(
                run_sync(sheets.get_records_for_month, year, month, uid),
                run_sync(sheets.get_all_budgets_for_month, uid, year, month),
            )
            total_in = sheets.monthly_total(records, "income")
            total_ex = sheets.monthly_total(records, "expense")
            balance  = total_in - total_ex
            ex_break = sheets.monthly_breakdown(records, "expense")
            icon = "📈" if balance >= 0 else "📉"

            ex_lines = ""
            for cat, amt in list(ex_break.items())[:5]:
                cat_emoji = EXPENSE_CATEGORIES.get(cat, "💸")
                budget    = budgets.get(cat)
                bar = ""
                if budget:
                    pct = amt / budget * 100
                    bar = " 🟥" if pct >= 100 else (" 🟨" if pct >= 80 else "")
                ex_lines += f"  {cat_emoji} {cat}: {fmt(amt)}{bar}\n"

            await app.bot.send_message(
                chat_id=uid,
                text=(
                    f"📊 *{label} 가계부 리포트* — {name}\n\n"
                    f"💰 수입: {fmt(total_in)}\n"
                    f"💸 지출: {fmt(total_ex)}\n"
                    f"{icon} 순수지: {fmt(balance)}\n\n"
                    f"*지출 상위 카테고리:*\n{ex_lines or '  (없음)'}\n\n"
                    "_상세 내용은 '이번달 요약' 버튼을 눌러 확인하세요_"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"자동 리포트 발송 실패 ({u['display_name']}): {e}")


async def scheduled_monthly(app: Application):
    """매월 1일 오전 9시 — 전월 결산 리포트"""
    now = now_kst().replace(day=1) - timedelta(days=1)
    await _auto_report(app, now.year, now.month, f"{now.month}월 월간")


async def scheduled_weekly(app: Application):
    """매주 월요일 오전 9시 — 지난 7일(월~일) 리포트"""
    now        = now_kst()
    week_end   = now - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    label      = f"{week_start.month}/{week_start.day}~{week_end.month}/{week_end.day} 주간"

    users = [u for u in await run_sync(sheets.get_all_users) if u.get("role") in ("admin", "member")]
    for u in users:
        try:
            uid  = int(u["user_id"])
            name = u["display_name"]
            recs = await run_sync(
                sheets.get_records_for_week,
                week_start.year, week_start.month,
                week_start.day, week_end.day, uid,
            )
            total_in = sheets.monthly_total(recs, "income")
            total_ex = sheets.monthly_total(recs, "expense")
            balance  = total_in - total_ex
            icon     = "📈" if balance >= 0 else "📉"
            await app.bot.send_message(
                chat_id=uid,
                text=(
                    f"📅 *{label} 리포트* — {name}\n\n"
                    f"💰 수입: {fmt(total_in)}\n"
                    f"💸 지출: {fmt(total_ex)}\n"
                    f"{icon} 순수지: {fmt(balance)}"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"주간 리포트 발송 실패 ({u['display_name']}): {e}")


# ── Phase 6: 봇 시작 / 종료 훅 ───────────────────────────────────
async def _on_startup(app: Application) -> None:
    """봇 시작 시 텔레그램 명령어 목록을 자동 등록합니다."""
    commands = [
        BotCommand("start",      "시작 / 메뉴 열기"),
        BotCommand("help",       "도움말"),
        BotCommand("q",          "빠른 입력: /q 식비 12000 메모"),
        BotCommand("summary",    "월 요약: /summary 또는 /summary 3"),
        BotCommand("stats",      "이번달 지출 통계"),
        BotCommand("budgets",    "이번달 예산 현황"),
        BotCommand("copybudget", "지난달 예산 이번달로 복사"),
        BotCommand("search",     "기록 검색: /search 키워드"),
        BotCommand("edit",       "기록 수정: /edit ID 필드 값"),
        BotCommand("del",        "기록 삭제: /del ID"),
        BotCommand("backup",     "전체 기간 CSV 내보내기"),
        BotCommand("rename",     "표시 이름 변경"),
        BotCommand("cancel",     "진행 중인 입력 취소"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info(f"Bot commands {len(commands)}개 등록 완료")


async def _on_shutdown(app: Application) -> None:
    """봇 종료 시 APScheduler를 정상 종료합니다."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler 정상 종료 완료")


# ── 메인 ──────────────────────────────────────────────────────────
def main():
    global _scheduler

    # Phase 6: 환경 변수 검증 (문제 있으면 즉시 종료)
    _validate_env()

    sheets.init_sheets()

    if ADMIN_ID:
        if not sheets.find_user(ADMIN_ID):
            sheets.register_user(ADMIN_ID, "관리자", role="admin")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)       # 시작 시 Bot Commands 등록
        .post_shutdown(_on_shutdown)  # 종료 시 스케줄러 정리
        .build()
    )

    # 공통 ConversationHandler 옵션
    conv_kwargs = dict(
        conversation_timeout=CONV_TIMEOUT,
    )

    income_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 수입 기록$"), income_start)],
        states={
            WAITING_CATEGORY: [CallbackQueryHandler(on_category,  pattern=r"^(cat:|cancel)")],
            WAITING_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount)],
            WAITING_MEMO:     [MessageHandler(filters.TEXT & ~filters.COMMAND, on_memo)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        **conv_kwargs,
    )
    expense_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 지출 기록$"), expense_start)],
        states={
            WAITING_CATEGORY: [CallbackQueryHandler(on_category,  pattern=r"^(cat:|cancel)")],
            WAITING_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, on_amount)],
            WAITING_MEMO:     [MessageHandler(filters.TEXT & ~filters.COMMAND, on_memo)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        **conv_kwargs,
    )
    budget_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎯 예산 설정$"), budget_start)],
        states={
            WAITING_BUDGET_CATEGORY: [CallbackQueryHandler(on_budget_cat, pattern=r"^(budget:|cancel)")],
            WAITING_BUDGET_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, on_budget_amount)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        **conv_kwargs,
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("q",          cmd_quick))
    app.add_handler(CommandHandler("summary",    cmd_summary))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("budgets",    cmd_budgets))
    app.add_handler(CommandHandler("copybudget", cmd_copybudget))
    app.add_handler(CommandHandler("search",     cmd_search))
    app.add_handler(CommandHandler("edit",       cmd_edit))
    app.add_handler(CommandHandler("del",        cmd_delete))
    app.add_handler(CommandHandler("backup",     cmd_backup))
    app.add_handler(CommandHandler("approve",    cmd_approve))
    app.add_handler(CommandHandler("deny",       cmd_deny))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("rename",     cmd_rename))
    app.add_handler(income_conv)
    app.add_handler(expense_conv)
    app.add_handler(budget_conv)
    app.add_handler(CallbackQueryHandler(on_chart_selected, pattern=r"^chart:"))
    app.add_handler(MessageHandler(filters.Regex("^📊 이번달 요약$"),  monthly_summary))
    app.add_handler(MessageHandler(filters.Regex("^👨‍👩‍👧 가족 현황$"),   family_summary))
    app.add_handler(MessageHandler(filters.Regex("^📋 최근 내역$"),    recent_history))
    app.add_handler(MessageHandler(filters.Regex("^📈 차트 보기$"),    chart_menu))
    app.add_handler(MessageHandler(filters.Regex("^📤 CSV 내보내기$"), export_csv))

    _scheduler = AsyncIOScheduler(timezone=KST)
    _scheduler.add_job(scheduled_monthly, "cron", day=1,            hour=9, minute=0, args=[app])
    _scheduler.add_job(scheduled_weekly,  "cron", day_of_week="mon", hour=9, minute=0, args=[app])
    _scheduler.start()

    logger.info("💰 가계부 봇 v2 시작! (Google Sheets + 멀티유저 + 차트)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
