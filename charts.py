"""
charts.py — matplotlib 차트 생성
한국어 폰트 자동 설치 지원
"""

import io
import logging
import subprocess
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.ticker as mticker
import numpy as np

logger = logging.getLogger(__name__)

# ── 한국어 폰트 설정 ────────────────────────────────────────────
def _setup_korean_font():
    """NanumGothic 폰트 적용 (최신 matplotlib 호환)"""
    # 이미 등록된 나눔 폰트가 있는지 확인
    for f in fm.fontManager.ttflist:
        if "Nanum" in f.name:
            matplotlib.rcParams["font.family"] = f.name
            return

    # 없으면 우분투 시스템 경로에서 직접 가져오기
    try:
        import glob
        font_paths = glob.glob("/usr/share/fonts/truetype/nanum/*.ttf")
        for path in font_paths:
            fm.fontManager.addfont(path)
            
        for f in fm.fontManager.ttflist:
            if "Nanum" in f.name:
                matplotlib.rcParams["font.family"] = f.name
                logger.info(f"한국어 폰트 수동 추가 완료: {f.name}")
                return
    except Exception as e:
        logger.warning(f"폰트 설정 실패: {e}")

    # 폴백: 기본 폰트
    matplotlib.rcParams["font.family"] = "DejaVu Sans"


_setup_korean_font()

# ── 공통 스타일 ────────────────────────────────────────────────
PALETTE = [
    "#4C9BE8", "#F4845F", "#62BB84", "#F7C86F", "#A98FD8",
    "#E87777", "#5EC8D4", "#F4A261", "#90BE6D", "#C77DFF",
    "#FFB5A7", "#80B3FF",
]
DARK_BG  = "#1E2023"
TEXT_CLR = "#E8EAF0"
GRID_CLR = "#333740"


def _buf_from_fig(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130)
    buf.seek(0)
    result = buf.read()
    plt.close(fig)
    return result


def pie_chart(title: str, data: dict[str, float], unit: str = "원") -> bytes:
    """카테고리별 지출/수입 파이차트"""
    if not data:
        return None

    labels = list(data.keys())
    values = list(data.values())
    total  = sum(values)

    fig, ax = plt.subplots(figsize=(7, 5.5), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda p: f"{p:.1f}%\n({int(p*total/100):,}{unit})" if p >= 3 else "",
        startangle=90,
        colors=PALETTE[:len(labels)],
        wedgeprops=dict(width=0.65, edgecolor=DARK_BG, linewidth=1.5),
        pctdistance=0.78,
    )
    for t in autotexts:
        t.set_fontsize(8)
        t.set_color(TEXT_CLR)

    # 범례
    legend_labels = [f"{l}  {v:,.0f}{unit}" for l, v in zip(labels, values)]
    ax.legend(
        wedges, legend_labels,
        loc="center left", bbox_to_anchor=(1, 0.5),
        fontsize=9, frameon=False,
        labelcolor=TEXT_CLR,
    )

    ax.set_title(f"{title}\n총 {total:,.0f}{unit}", color=TEXT_CLR, fontsize=13, pad=14)
    fig.tight_layout()
    return _buf_from_fig(fig)


def bar_chart_budget(
    title: str,
    categories: list[str],
    actual: list[float],
    budget: list[float],
) -> bytes:
    """예산 대비 실지출 막대그래프"""
    if not categories:
        return None

    x = np.arange(len(categories))
    w = 0.38

    fig, ax = plt.subplots(figsize=(max(7, len(categories) * 1.1), 5), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors=TEXT_CLR)
    ax.spines[:].set_color(GRID_CLR)
    ax.yaxis.grid(True, color=GRID_CLR, linewidth=0.6, linestyle="--")
    ax.set_axisbelow(True)

    bars_actual = ax.bar(x - w/2, actual, w, label="실지출", color="#4C9BE8", alpha=0.9, zorder=3)
    bars_budget = ax.bar(x + w/2, budget, w, label="예산",   color="#62BB84", alpha=0.75, zorder=3)

    # 초과 표시
    for i, (a, b) in enumerate(zip(actual, budget)):
        if a > b:
            ax.bar(x[i] - w/2, a - b, w, bottom=b, color="#E87777", alpha=0.9, zorder=4, label="_over")

    # 금액 레이블
    for bar in bars_actual:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + max(actual)*0.01,
                    f"{int(h):,}", ha="center", va="bottom", fontsize=7.5, color=TEXT_CLR)

    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=9, color=TEXT_CLR)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(axis="y", colors=TEXT_CLR)
    ax.set_title(title, color=TEXT_CLR, fontsize=12, pad=12)
    ax.legend(fontsize=9, frameon=False, labelcolor=TEXT_CLR)

    fig.tight_layout()
    return _buf_from_fig(fig)


def bar_chart_monthly_trend(
    title: str,
    months: list[str],
    incomes: list[float],
    expenses: list[float],
) -> bytes:
    """월별 수입/지출 트렌드 막대그래프"""
    x = np.arange(len(months))
    w = 0.38

    fig, ax = plt.subplots(figsize=(max(6, len(months) * 1.2), 5), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors=TEXT_CLR)
    ax.spines[:].set_color(GRID_CLR)
    ax.yaxis.grid(True, color=GRID_CLR, linewidth=0.6, linestyle="--")
    ax.set_axisbelow(True)

    ax.bar(x - w/2, incomes,  w, label="수입", color="#62BB84", alpha=0.9, zorder=3)
    ax.bar(x + w/2, expenses, w, label="지출", color="#F4845F", alpha=0.9, zorder=3)

    # 순수지 선
    net = [i - e for i, e in zip(incomes, expenses)]
    ax.plot(x, net, color="#F7C86F", marker="o", linewidth=1.8, label="순수지", zorder=5)

    ax.set_xticks(x)
    ax.set_xticklabels(months, fontsize=9, color=TEXT_CLR)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(axis="y", colors=TEXT_CLR)
    ax.axhline(0, color=GRID_CLR, linewidth=1)
    ax.set_title(title, color=TEXT_CLR, fontsize=12, pad=12)
    ax.legend(fontsize=9, frameon=False, labelcolor=TEXT_CLR)

    fig.tight_layout()
    return _buf_from_fig(fig)


def bar_chart_by_member(
    title: str,
    members: list[str],
    values: list[float],
    label: str = "지출",
) -> bytes:
    """가족 구성원별 지출/수입 막대그래프"""
    if not members:
        return None
    fig, ax = plt.subplots(figsize=(max(5, len(members) * 1.3), 4.5), facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)
    ax.tick_params(colors=TEXT_CLR)
    ax.spines[:].set_color(GRID_CLR)
    ax.yaxis.grid(True, color=GRID_CLR, linewidth=0.6, linestyle="--")
    ax.set_axisbelow(True)

    colors = PALETTE[:len(members)]
    bars = ax.bar(members, values, color=colors, alpha=0.9, zorder=3)

    for bar in bars:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + max(values)*0.01,
                    f"{int(h):,}원", ha="center", va="bottom", fontsize=9, color=TEXT_CLR)

    ax.set_xticklabels(members, fontsize=10, color=TEXT_CLR)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(axis="y", colors=TEXT_CLR)
    ax.set_title(title, color=TEXT_CLR, fontsize=12, pad=12)
    ax.set_ylabel(label, color=TEXT_CLR, fontsize=9)

    fig.tight_layout()
    return _buf_from_fig(fig)
