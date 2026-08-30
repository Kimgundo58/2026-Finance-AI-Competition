# -*- coding: utf-8 -*-
"""architecture_simple.png 스타일로 「써도돼요」 파이프라인 2장 생성."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Ellipse, Rectangle, Circle, Polygon
import matplotlib.font_manager as fm
import numpy as np, os

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

OUT = r"C:\Users\dogun\Downloads\Desktop\Desktop\Desktop\Desktop\김건도\3-1 여름방학\금융 AI공모전"

GRAY_BOX = "#e9e9e9"
GRAY_CYL = "#6f6f6f"
BLACK = "#111111"


def box(ax, cx, cy, w, h, title, sub=None, fs=17, fs2=13):
    p = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                       boxstyle="round,pad=2,rounding_size=14",
                       facecolor=GRAY_BOX, edgecolor=BLACK, linewidth=2.2, zorder=3)
    ax.add_patch(p)
    if sub:
        ax.text(cx, cy + h * 0.16, title, ha="center", va="center", fontsize=fs, fontweight="bold", zorder=4)
        ax.text(cx, cy - h * 0.20, sub, ha="center", va="center", fontsize=fs2, color="#333333", zorder=4)
    else:
        ax.text(cx, cy, title, ha="center", va="center", fontsize=fs, fontweight="bold", zorder=4)


def cylinder(ax, cx, cy, w, h, label_below=None, fs=15, inner=None):
    ry = w * 0.16
    body = Rectangle((cx - w / 2, cy - h / 2 + ry), w, h - 2 * ry,
                     facecolor=GRAY_CYL, edgecolor="none", zorder=3)
    ax.add_patch(body)
    bot = Ellipse((cx, cy - h / 2 + ry), w, 2 * ry, facecolor=GRAY_CYL, edgecolor=BLACK, lw=2, zorder=3)
    ax.add_patch(bot)
    ax.plot([cx - w / 2, cx - w / 2], [cy - h / 2 + ry, cy + h / 2 - ry], color=BLACK, lw=2, zorder=4)
    ax.plot([cx + w / 2, cx + w / 2], [cy - h / 2 + ry, cy + h / 2 - ry], color=BLACK, lw=2, zorder=4)
    top = Ellipse((cx, cy + h / 2 - ry), w, 2 * ry, facecolor=GRAY_CYL, edgecolor=BLACK, lw=2, zorder=4)
    ax.add_patch(top)
    if inner:
        ax.text(cx, cy - ry * 0.4, inner, ha="center", va="center", fontsize=12,
                color="white", fontweight="bold", zorder=5)
    if label_below:
        ax.text(cx, cy - h / 2 - 26, label_below, ha="center", va="top", fontsize=fs, fontweight="bold")


def arrow(ax, x1, y1, x2, y2, label=None, fs=13.5, dy=14, lw=2.4):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=26,
                        linewidth=lw, color=BLACK, zorder=2, shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    if label:
        ax.text((x1 + x2) / 2, max(y1, y2) + dy, label, ha="center", va="bottom", fontsize=fs)


def region(ax, x1, y1, x2, y2, title, fs=19):
    r = Rectangle((x1, y1), x2 - x1, y2 - y1, fill=False, edgecolor=BLACK,
                  linewidth=1.8, linestyle=(0, (5, 5)), zorder=1)
    ax.add_patch(r)
    ax.text((x1 + x2) / 2, y2 + 34, title, ha="center", va="bottom", fontsize=fs, fontweight="bold")


def gear(ax, cx, cy, r=26):
    ang = np.linspace(0, 2 * np.pi, 33)[:-1]
    pts = []
    for i, a in enumerate(ang):
        rr = r if i % 2 == 0 else r * 0.62
        pts.append((cx + rr * np.cos(a), cy + rr * np.sin(a)))
    ax.add_patch(Polygon(pts, closed=True, facecolor=BLACK, zorder=3))
    ax.add_patch(Circle((cx, cy), r * 0.30, facecolor="white", zorder=4))


def person(ax, cx, cy, s=26):
    ax.add_patch(Circle((cx, cy + s * 0.75), s * 0.42, facecolor="#4a4a4a", zorder=3))
    body = Polygon([(cx - s * 0.62, cy - s * 0.75), (cx + s * 0.62, cy - s * 0.75),
                    (cx + s * 0.45, cy + 2), (cx - s * 0.45, cy + 2)],
                   closed=True, facecolor="#4a4a4a", zorder=3)
    ax.add_patch(body)


def pin(ax, cx, cy, s=42):
    ax.add_patch(Circle((cx, cy + s * 0.25), s * 0.55, facecolor="#5a5a5a", edgecolor=BLACK, lw=2, zorder=3))
    tri = Polygon([(cx - s * 0.40, cy + s * 0.02), (cx + s * 0.40, cy + s * 0.02), (cx, cy - s * 0.85)],
                  closed=True, facecolor="#5a5a5a", edgecolor="none", zorder=2)
    ax.add_patch(tri)
    ax.add_patch(Circle((cx, cy + s * 0.25), s * 0.22, facecolor="white", zorder=4))


def canvas(w, h):
    fig, ax = plt.subplots(figsize=(w / 100, h / 100), dpi=100)
    ax.set_xlim(0, w); ax.set_ylim(0, h)
    ax.set_aspect("equal"); ax.axis("off")
    ax.add_patch(FancyBboxPatch((14, 14), w - 28, h - 28, boxstyle="round,pad=0,rounding_size=18",
                                fill=False, edgecolor=BLACK, linewidth=3, zorder=1))
    return fig, ax


# ═══════════════ 1) 온라인 판정 파이프라인 ═══════════════
fig, ax = canvas(2640, 960)

# 좌측: 입력
cylinder(ax, 170, 540, 130, 185, "창업팀 질문 ·\nF 프로필", fs=16)

# 범위 1 — AI 판정
region(ax, 320, 210, 1560, 780, "AI 판정 범위 — 오픈소스 LLM 2회 고정 · p50 ~3.2초")
box(ax, 470, 560, 240, 165, "① 질문 정규화", "LLM (Qwen3 · 로컬)", fs=16)
box(ax, 840, 560, 280, 165, "②③ 룰조회 · 검색\n· 참조확장", "코드 (LLM 0회)", fs=15)
box(ax, 1190, 560, 250, 165, "④ 판정 조립", "LLM (Qwen3 32B · 투표)", fs=16, fs2=12.5)
box(ax, 1455, 560, 165, 165, "⑥ 검증\n· 강등", "코드", fs=14)

arrow(ax, 172, 540, 348, 540)
arrow(ax, 592, 560, 698, 560, "품목·금액·비목후보", dy=100, fs=13)
arrow(ax, 982, 560, 1063, 560, "원문 top-5+폐포 · 룰 결과", dy=100, fs=13)
arrow(ax, 1317, 560, 1370, 560, "S번호 인용", dy=100, fs=13)

# 코퍼스 실린더 (범위 1 내부 하단)
cylinder(ax, 840, 330, 120, 150, None, inner="L1·L2")
ax.text(970, 330, "규정 코퍼스 + 룰·refs\nL3 는 검색 없이 통째 로드", ha="left", va="center", fontsize=13)
arrow(ax, 840, 408, 840, 475, lw=2)

# 중간 실린더 — 판정 로그
cylinder(ax, 1690, 540, 130, 185, "판정 결과 저장\n(decisions)", fs=15)
arrow(ax, 1540, 560, 1622, 560)

# 범위 2 — 확인·문의
region(ax, 1830, 210, 2350, 780, "확인 · 문의 범위 — 사람")
box(ax, 2088, 560, 466, 165, "화면 5·6·7 — 판정 · 할일 · 근거 원문", "판단불가 → 사례 안내 + 문의 초안 (LLM ⑤)", fs=14.5, fs2=12)
arrow(ax, 1760, 585, 1852, 585)
arrow(ax, 1852, 500, 1760, 500)
ax.text(1806, 541, "컨펌·재판정", ha="center", va="center", fontsize=10.5)

# 우측: 사람
pin(ax, 2478, 560, 46)
ax.text(2478, 478, "주관기관 담당자\n(판단불가 문의)", ha="center", va="top", fontsize=14, fontweight="bold")
arrow(ax, 2324, 585, 2428, 585)
arrow(ax, 2428, 500, 2324, 500)

# 하단 아이콘 주석
gear(ax, 700, 110)
ax.text(745, 110, "자동 수행 — 호출 수·순서 고정 · 모든 실패의 기본값은 판단불가", ha="left", va="center", fontsize=16)
person(ax, 2000, 105)
ax.text(2045, 110, "창업팀 컨펌 (비목·전제 인라인 입력)", ha="left", va="center", fontsize=16)

fig.savefig(os.path.join(OUT, "architecture_pipeline.png"), bbox_inches="tight", facecolor="white")
plt.close(fig)

# ═══════════════ 2) 오프라인 인덱싱 파이프라인 ═══════════════
fig, ax = canvas(2640, 760)

cylinder(ax, 180, 420, 135, 190, "원문 260+ 문서\n(XML·PDF·HWP→PDF)", fs=15)

region(ax, 350, 170, 2050, 640, "오프라인 인덱싱 범위 — 자동 + 사람 검수")
box(ax, 560, 430, 300, 165, "Stage 0\n파싱 · 조 분해", "pdftext · 게이트 V1~V4", fs=16)
box(ax, 985, 430, 350, 165, "Stage 0.5~0.8  태깅 ·\n참조그래프 · 우선순위", "적용대상 · refs · precedence", fs=15)
box(ax, 1420, 430, 300, 165, "Stage 1\n룰 컴파일", "verified=false → 사람 검수", fs=16)
box(ax, 1830, 430, 330, 165, "Stage 2  청킹 · 임베딩\n· BM25", "900토큰 분할 · 컨텍스트 헤더", fs=15)

arrow(ax, 182, 420, 385, 420)
arrow(ax, 712, 430, 808, 430, "조 단위\nJSON", dy=20)
arrow(ax, 1162, 430, 1268, 430)
arrow(ax, 1572, 430, 1663, 430)

cylinder(ax, 2330, 420, 135, 190, "판정 코퍼스\nchunks·rules·refs", fs=15)
arrow(ax, 2000, 430, 2260, 430, "index_guard 게이트\n(골든셋·archive·L4 거부)", dy=22)

gear(ax, 700, 95)
ax.text(745, 95, "재인덱싱 = 트랜잭션 1개 (BM25 포함) · 골든셋 재실행 의무", ha="left", va="center", fontsize=16)
person(ax, 1600, 90)
ax.text(1645, 95, "D단계 사람 검수 → verified=true (룰 19행 · 골든셋 68문항)", ha="left", va="center", fontsize=16)

fig.savefig(os.path.join(OUT, "architecture_indexing.png"), bbox_inches="tight", facecolor="white")
plt.close(fig)
print("saved")
