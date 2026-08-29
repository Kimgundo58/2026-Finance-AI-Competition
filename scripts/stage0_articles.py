# -*- coding: utf-8 -*-
"""Stage 0-d : 평문 → 조(條) 단위 재조립 + 검증 게이트 V1~V6.

파이프라인 문서 §2.3(3단 fallback) / §2.4(검증 게이트) 구현.
"""
from __future__ import annotations
import re

# 1순위: 제N조(제목) — 법령·지침·규정·규칙
RE_JO = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*\(([^)\n]{1,50})\)")
# 1순위 보조: 제목 없는 제N조
RE_JO_BARE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?=\s)")
# 2순위: 제N장 / 번호 목록 — 매뉴얼·가이드라인
RE_JANG = re.compile(r"제\s*(\d+)\s*장\s*([^\n]{0,50})")


def sanitize(t: str) -> str:
    """Postgres text 에 넣을 수 없는 문자 제거.
    NUL(0x00) 은 DataError, 짝 없는 서로게이트는 인코딩 오류를 낸다."""
    if not t:
        return ""
    return "".join(
        c for c in t
        if c != "\x00" and not (0xD800 <= ord(c) <= 0xDFFF)
    )


def _clean(t: str) -> str:
    t = sanitize(t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _page_of(offset: int, offsets: dict[int, int]) -> int | None:
    if not offsets:
        return None
    best = None
    for pos, pg in offsets.items():
        if pos <= offset:
            best = pg
        else:
            break
    return best


# 부칙 경계 — 이후의 제N조는 본칙과 번호가 겹친다
RE_BUCHIK = re.compile(r"(?:^|\n)\s*부\s{0,4}칙\s*(?=[<(\n])")
# 붙임/별표 섹션 헤더 (줄머리 + 제목이 같은 줄에 옴)
RE_ATTACH = re.compile(r"(?:^|\n)[ \t]*[\[【]?\s*(붙임|별표|별지|서식|참고)\s*(\d*)\s*[\]】][ \t]*([^\n]{0,60})")
# 참고 를 빠뜨리면 부칙 컷이 연쇄로 깨진다. 실측(창업중심대학 2025):
#   [참고1] 이하 28,000자가 본문에 남아 전체 길이를 부풀렸고, 부칙 위치가
#   14,305/44,107 = 32% 로 계산돼 아래 40% 임계에 미달했다. 부칙이 분리되지 않아
#   부칙 제1조(시행일)가 본칙 제1조(목적)를 덮어써 한 조가 28,178자가 됐다.
#   rule_base.md §5 — [참고N] 에 비목 카탈로그·증빙 매핑이 있어 룰 소스이기도 하다.


def _cut_sections(text: str) -> tuple[str, str | None, list[tuple[str, str]]]:
    """본칙 / 부칙 / [(붙임라벨, 본문)] 으로 分割."""
    half = int(len(text) * 0.45)
    attach_starts = [m for m in RE_ATTACH.finditer(text) if m.start() >= half]

    body_end = attach_starts[0].start() if attach_starts else len(text)
    attachments = []
    for i, m in enumerate(attach_starts):
        end = attach_starts[i + 1].start() if i + 1 < len(attach_starts) else len(text)
        label = f"{m.group(1)}{m.group(2) or (i + 1)}"
        seg = text[m.start():end].strip()
        if len(seg) >= 80:
            attachments.append((label, seg))

    body = text[:body_end]
    bm = RE_BUCHIK.search(body)
    if bm and bm.start() > len(body) * 0.4:
        return body[:bm.start()], body[bm.start():], attachments
    return body, None, attachments


# 인용 표기를 조 헤딩으로 오인하지 않게 거른다.
#   실측: 예비/초기창업 세부관리기준 제30조 본문에
#     "① 창업기업의 권리 의무 이전은 지침 제65조(권리 의무 이전)에 따른다."
#   가 있어 `제65조(권리 의무 이전)` 이 34번째 조로 잡혔다. 뒤 조를 전부 삼켜
#   본문 7,220자가 됐고 단조성 검증도 깨졌다.
#   조 헤딩은 줄머리에 오고, 인용은 앞에 규범어가 붙는다.
RE_CITE_PREFIX = re.compile(
    r"(?:(지침|요령|관리기준|기준|법|법률|시행령|시행규칙|규정|규칙|조례)"
    r"|[」』〉›])\s*$")
# 닫는 인용부호도 접두로 본다 — 「중소기업창업 지원법」제43조 (재도전 2025 실측)


def _is_citation(text: str, start: int) -> bool:
    """이 위치의 `제N조(...)` 가 조 헤딩이 아니라 본문 속 인용인가.

    판정은 **규범어 접두 하나로만** 한다. "줄머리가 아니면 인용" 규칙도 넣어봤으나
    과잉 차단이었다 — 실측에서 조 36개가 21개로 줄었다. 조 헤딩을 놓치는 것이
    인용을 하나 더 잡는 것보다 훨씬 나쁘다.
    """
    return bool(RE_CITE_PREFIX.search(text[max(0, start - 14):start]))


def split_articles(text: str, page_offsets: dict[int, int] | None = None) -> tuple[list[dict], str]:
    """반환: (조 리스트, 사용한 전략 이름)"""
    text = _clean(text)
    page_offsets = dict(sorted((page_offsets or {}).items()))

    본칙, 부칙, 붙임들 = _cut_sections(text)

    # ── 1순위: 제N조(제목) ──────────────────────────────────────
    ms = [m for m in RE_JO.finditer(본칙) if not _is_citation(본칙, m.start())]
    if len(ms) >= 5:
        arts = _build(본칙, ms, page_offsets, titled=True)
        # 부칙의 제N조는 본칙과 번호가 겹치므로 별도 라벨을 붙인다
        if 부칙:
            off = len(본칙)
            bms = list(RE_JO.finditer(부칙)) or list(RE_JO_BARE.finditer(부칙))
            if bms:
                for a in _build(부칙, bms, {}, titled=bool(RE_JO.search(부칙))):
                    a["조번호"] = "부칙 " + a["조번호"]
                    a["조번호_int"] = None      # 단조성 검증에서 제외
                    arts.append(a)
            else:
                arts.append({"조번호": "부칙", "조제목": None, "조번호_int": None,
                             "본문": 부칙.strip(), "페이지": None})
        # 붙임/별표는 각각 독립 조로 (룰 소스의 실체가 여기 있다)
        for label, seg in 붙임들:
            arts.append({"조번호": label, "조제목": seg.split("\n", 1)[0][:60],
                         "조번호_int": None, "본문": seg, "페이지": None})
        return arts, "jo_titled"

    # ── 1순위 보조: 제목 없는 제N조 ─────────────────────────────
    ms = list(RE_JO_BARE.finditer(text))
    if len(ms) >= 5:
        return _build(text, ms, page_offsets, titled=False), "jo_bare"

    # ── 2순위: 제N장 ────────────────────────────────────────────
    ms = list(RE_JANG.finditer(text))
    if len(ms) >= 3:
        arts = []
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            body = text[m.start():end].strip()
            if len(body) < 30:
                continue
            arts.append({
                "조번호": f"제{m.group(1)}장",
                "조제목": (m.group(2) or "").strip() or None,
                "조번호_int": int(m.group(1)),
                "본문": body,
                "페이지": _page_of(m.start(), page_offsets),
            })
        if arts:
            return arts, "jang"

    # ── 3순위: 단락 분할 (구조 없음) ────────────────────────────
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) >= 100]
    arts = [{
        "조번호": f"단락{i+1:03d}",
        "조제목": None,
        "조번호_int": i + 1,
        "본문": p,
        "페이지": None,
    } for i, p in enumerate(paras)]
    return arts, "paragraph"


def _build(text, ms, page_offsets, titled: bool) -> list[dict]:
    arts, seen = [], set()
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        num, branch = m.group(1), m.group(2)
        조번호 = f"제{num}조" + (f"의{branch}" if branch else "")
        if 조번호 in seen:                       # 목차 중복·재등장 → 긴 쪽 채택
            prev = next(a for a in arts if a["조번호"] == 조번호)
            body = text[m.start():end].strip()
            if len(body) > len(prev["본문"]):
                prev["본문"] = body
            continue
        seen.add(조번호)
        arts.append({
            "조번호": 조번호,
            "조제목": (m.group(3).strip() if titled else None),
            "조번호_int": int(num),
            "본문": text[m.start():end].strip(),
            "페이지": _page_of(m.start(), page_offsets),
        })
    return arts


# ── 검증 게이트 V1~V6 ───────────────────────────────────────────
def validate(arts: list[dict], strategy: str) -> dict:
    """반환: {ok, quality, flags[]}"""
    flags = []

    # V2 조 개수
    if len(arts) < 5:
        flags.append(f"V2:조_개수_부족({len(arts)})")

    # V1 조 번호 단조 증가
    nums = [a["조번호_int"] for a in arts if a.get("조번호_int") is not None]
    breaks = [(nums[i], nums[i + 1]) for i in range(len(nums) - 1) if nums[i + 1] < nums[i]]
    if breaks:
        s = ", ".join(f"{a}→{b}" for a, b in breaks[:3])
        flags.append(f"V1:조번호_비단조({len(breaks)}건: {s})")

    # V3 빈 조 비율
    empty = sum(1 for a in arts if len(a["본문"]) < 50)
    if arts and empty / len(arts) > 0.10:
        flags.append(f"V3:빈조_과다({empty}/{len(arts)})")

    # 구조 품질
    if strategy == "paragraph":
        flags.append("구조없음:단락분할")
    elif strategy == "jang":
        flags.append("구조약함:장단위")

    quality = "low" if (strategy in ("paragraph", "jang") or len(arts) < 5) else "high"
    ok = len(arts) > 0
    return {"ok": ok, "quality": quality, "flags": flags}


# ── 크로스 레퍼런스 추출 (V6) ───────────────────────────────────
RE_XREF = re.compile(r"지침\s*제\s*(\d+)\s*조(?:\s*부터\s*제\s*(\d+)\s*조)?")


def find_xrefs(text: str) -> list[dict]:
    out = []
    for m in RE_XREF.finditer(text):
        out.append({"참조문자열": m.group(0).strip(),
                    "시작조": int(m.group(1)),
                    "종료조": int(m.group(2)) if m.group(2) else None})
    return out
