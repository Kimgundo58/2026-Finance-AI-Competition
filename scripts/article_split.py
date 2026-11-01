# -*- coding: utf-8 -*-
"""추출한 평문을 조(條) 단위로 나누고 분할 품질을 검증한다.

l3_parse(L3 업로드)와 코퍼스 적재가 text_extract 다음 단계로 부른다. 공개 함수:
split_articles(text, page_offsets) -> (조 리스트, 전략 이름), validate(arts, strategy) -> {ok, quality, flags},
is_deleted(art), sanitize(t). 전략은 article_titled → outline_numbered → article_untitled → chapter →
roman_chapter → paragraph 순으로 시도한다. 불변식: 조 0개면 validate 의 ok 가 False 다.
"""
from __future__ import annotations
import re

# 1순위: 제N조(제목) — 법령·지침·규정·규칙
RE_조 = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*\(([^)\n]{1,50})\)")
# 1순위 보조: 제목 없는 제N조
RE_조_제목없음 = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?=\s)")
# 2순위: 제N장 / 번호 목록 — 매뉴얼·가이드라인
RE_장 = re.compile(r"제\s*(\d+)\s*장\s*([^\n]{0,50})")

# 2.5순위: 로마숫자 장(Ⅰ,Ⅱ,Ⅲ…) — "제N장" 이 없는 문서. hwpx 추출은 표제줄이 로마숫자 앞에 온다.
RE_ROMAN = re.compile(r"(?:^|\n)[ \t]*([Ⅰ-Ⅹ])[ \t]*(?=\n|$)")
_ROMAN_ORDER = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"


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


# ── 섹션 경계 ───────────────────────────────────────────────────
# 부칙 경계 — 이후의 제N조는 본칙과 번호가 겹친다
RE_부칙 = re.compile(r"(?:^|\n)\s*부\s{0,4}칙\s*(?=[<(\n])")

# 붙임/별표/참고 섹션 헤더. 괄호는 [] 【】 <> 세 종류.
#   [참고1] · 【붙임 1】 · [별지 제①호] · [별지서식] · < 붙임1 > · < 별첨2-1 >
# 여는 괄호는 필수 — 없으면 본문 문장이 걸린다.
# 닫는 괄호 뒤 구두점은 표 행(`<붙임1>, 운영사가 …`)이라 막는다. 표제 뒤에는 제목이나 줄바꿈이 온다.
RE_ATTACH = re.compile(
    r"(?:^|\n)[ \t]*[\[【<]\s*(붙임|별표|별지|서식|참고|별첨)\s*([^\]】>\n]{0,12}?)\s*[\]】>]"
    r"[ \t]*(?![,、。·;:])([^\n]{0,60})")

# 목차 점선(리더, leader dots)
RE_DOTS = re.compile(r"·{5,}[^\n]*")

# 원문자 → 아라비아 숫자.  [별지 제①호] (모두의창업)
_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)}


def _cut_toc(text: str) -> int:
    """목차 끝 오프셋. 없으면 0.

    목차 점선이 10회 이상 나오고 마지막이 앞 30% 안이면 그때까지가 목차다.
    목차를 남기면 개요형(`N.`)에서 표제 번호가 본문과 함께 두 번 나와 분할이 깨진다.
    """
    dots = list(RE_DOTS.finditer(text))
    if len(dots) >= 10 and dots[-1].start() < len(text) * 0.30:
        return dots[-1].end()
    return 0


def _부칙_위치(text: str) -> int | None:
    """부칙 시작 오프셋. 줄 시작 마커 뒤 300자 안에 '시행' 이 있는 첫 마커. 없으면 None."""
    for m in RE_부칙.finditer(text):
        if "시행" in text[m.end():m.end() + 300]:
            return m.start()
    return None


def _attach_label(kind: str, raw: str, seq: int) -> str:
    """[별지 제①호] → 별지1 · [별지서식] → 별지서식 · < 붙임2-1 > → 붙임2"""
    s = "".join(_CIRCLED.get(c, c) for c in raw).strip()
    d = re.search(r"\d+", s)
    if d:
        return f"{kind}{d.group(0)}"
    return f"{kind}{s}" if s else f"{kind}{seq}"


def _cut_sections(text: str) -> tuple[str, str | None, list[tuple[str, str]], int]:
    """(본칙, 부칙, [(붙임라벨, 본문)], 본칙_시작오프셋) 으로 분할.

    부칙을 먼저 찾고, 붙임은 부칙 뒤에서만 찾는다 — 부칙 앞의 `[별지2]을 준수하여 …` 는 본문 속 참조다.
    """
    base = _cut_toc(text)
    body_all = text[base:]

    b = _부칙_위치(body_all)
    scan_from = b if b is not None else int(len(body_all) * 0.45)
    starts = [m for m in RE_ATTACH.finditer(body_all) if m.start() >= scan_from]

    attachments, used = [], {}
    kept_first = None
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body_all)
        seg = body_all[m.start():end].strip()
        if len(seg) < 80:                     # 표제만 있고 내용이 없으면 섹션이 아니다
            continue
        label = _attach_label(m.group(1), m.group(2) or "", i + 1)
        used[label] = used.get(label, 0) + 1  # TIPS 는 [별지1] 이 세 번 나온다
        if used[label] > 1:
            label = f"{label}[{used[label]}]"
        if kept_first is None:
            kept_first = m.start()
        attachments.append((label, seg))

    body_end = kept_first if kept_first is not None else len(body_all)
    if b is not None:
        return body_all[:b], body_all[b:body_end], attachments, base
    return body_all[:body_end], None, attachments, base


# 인용 표기(`지침 제65조(권리 의무 이전)에 따른다`)를 조 표제로 오인하지 않게 거른다.
# 조 표제는 줄 시작에 오고, 인용은 앞에 법령명 접두어가 붙는다.
RE_CITE_PREFIX = re.compile(
    r"(?:(지침|요령|관리기준|기준|법|법률|시행령|시행규칙|규정|규칙|조례)"
    r"|[」』〉›])\s*$")
# 닫는 인용부호도 접두로 본다 — 「중소기업창업 지원법」제43조


# 닫는 괄호 뒤에 띄어쓰기 없이 붙는 조사. 조 표제면 `)` 뒤에 공백이나 `①` 이 온다.
#   인용  `제65조(제재 및 환수)에 따른다`      -> `)` 뒤 `에`
#   표제  `제65조(제재 및 환수) ① 전문기관의`  -> `)` 뒤 공백
# `이 / 가 / 은 / 는 / 도` 는 뺀다 — `제1조(시행일)이 기준은 …` 처럼 지시어로 시작하는 표제가 있다.
RE_CITE_SUFFIX = re.compile(r"^(?:에서|에도|으로|부터|까지|에|와|과|의|를|을|로|및)")


def _is_citation(text: str, start: int, end: int | None = None) -> bool:
    """이 위치의 `제N조(...)` 가 조 표제가 아니라 본문 속 인용인가.

    앞의 법령명 접두어(`지침 제65조…` · `「…법」제43조`)와 닫는 괄호 뒤 조사(`제65조(…)에`) 두 방향을 본다.
    '줄 시작이 아니면 인용' 규칙은 쓰지 않는다 — 조 표제를 놓치는 쪽이 훨씬 나쁘다.
    접두어는 같은 줄 안에서만 본다. 문서 제목 줄이 `…사업비 관리 지침` 으로 끝나고 다음 줄이
    `제1조(목적)` 이면, 줄을 넘겨 보면 제목 끝의 `지침` 을 접두어로 읽어 제1조를 인용으로 버린다.
    """
    앞 = text[max(0, start - 14):start].rsplit("\n", 1)[-1]
    if RE_CITE_PREFIX.search(앞):
        return True
    if end is not None and RE_CITE_SUFFIX.match(text[end:end + 3]):
        return True
    return False


# ── 개요형 (TIPS 계열) ──────────────────────────────────────────
RE_OUTLINE = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})\.[ \t]*([^\n]{1,40})")


def _outline_headings(body: str) -> list[re.Match]:
    """`N.` 표제만 고른다.

    중첩 열거도 표기가 같아 정규식으로는 못 가른다. 번호 단조성으로 가른다 —
    표제 번호는 장을 넘어 이어지고, 중첩 열거는 1부터 다시 시작한다.
    """
    out, last = [], 0
    for m in RE_OUTLINE.finditer(body):
        n = int(m.group(1))
        if last < n <= last + 3:   # 결번 허용 3 — 조판 오류로 표제 하나를 놓쳐도 회복한다
            out.append(m)
            last = n
    return out


def _roman_chapters(text: str, page_offsets: dict[int, int]) -> list[dict]:
    """"표제줄 \\n 로마숫자 단독줄" 을 장 경계로 쓴다 — "제N장" 이 없는 개요형 문서용.

    등장 순서가 Ⅰ,Ⅱ,Ⅲ… 로 정확히 이어지는지 먼저 검사한다. 3개 미만이거나 순서가 어긋나면
    빈 리스트를 돌려주고 호출부가 단락 분할로 내려간다.
    """
    marks = list(RE_ROMAN.finditer(text))
    seq = "".join(m.group(1) for m in marks)
    if len(marks) < 3 or not _ROMAN_ORDER.startswith(seq):
        return []
    arts = []
    for i, m in enumerate(marks):
        끝 = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        표제줄 = text[:m.start()].rstrip().rsplit("\n", 1)[-1].strip()
        본문 = ((표제줄 + "\n") if 표제줄 else "") + text[m.end():끝].strip()
        if len(본문.strip()) < 30:
            continue
        arts.append({
            "조번호": m.group(1), "조제목": 표제줄 or None, "조순번": i + 1,
            "본문": 본문.strip(), "페이지": _page_of(m.start(), page_offsets),
        })
    return arts


# 단락 폴백 조각의 최소 길이 — 미만이면 버리지 않고 채울 때까지 이어 붙인다(버리면 HWP 표가 사라진다).
_단락_최소 = 100


def _짧은_조문구조(본칙: str, ms: list[re.Match]) -> bool:
    """조가 5개 미만인 짧은 규정(기관 사업비 지침 등)도 조 구조로 인정할 수 있는가.

    `제N조(제목)` 이 2개 이상이고, 모두 줄 시작에 있고, 번호가 제1조부터 빠짐없이 이어질 때만 인정한다.
    인용만 몇 개 있는 문서를 조 구조로 잘못 읽지 않도록 5개 기준보다 조건을 좁힌다.
    """
    if len(ms) < 2 or [m.group(1) for m in ms] != [str(i) for i in range(1, len(ms) + 1)]:
        return False
    return all(not 본칙[max(0, m.start() - 20):m.start()].rsplit("\n", 1)[-1].strip() for m in ms)


def split_articles(text: str, page_offsets: dict[int, int] | None = None
                   ) -> tuple[list[dict], str]:
    """반환: (조 리스트, 사용한 전략 이름)"""
    text = _clean(text)
    page_offsets = dict(sorted((page_offsets or {}).items()))

    본칙, 부칙, 붙임들, base = _cut_sections(text)

    # ── 1순위: 제N조(제목) ──────────────────────────────────────
    ms = [m for m in RE_조.finditer(본칙) if not _is_citation(본칙, m.start(), m.end())]
    if len(ms) >= 5 or _짧은_조문구조(본칙, ms):
        arts = _build(본칙, ms, page_offsets, titled=True, base=base)
        return arts + _tail(부칙, 붙임들), "article_titled"

    # ── 개요형: 제N장 > N. > 가. (TIPS 총괄 운영지침) ───────────
    oms = _outline_headings(본칙)
    if len(oms) >= 10 and int(oms[-1].group(1)) >= 10:
        arts = _build_outline(본칙, oms, page_offsets, base)
        return arts + _tail(부칙, 붙임들), "outline_numbered"

    # ── 1순위 보조: 제목 없는 제N조 ─────────────────────────────
    ms = list(RE_조_제목없음.finditer(text))
    if len(ms) >= 5:
        return _build(text, ms, page_offsets, titled=False), "article_untitled"

    # ── 2순위: 제N장 ────────────────────────────────────────────
    ms = list(RE_장.finditer(text))
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
                "조순번": int(m.group(1)),
                "본문": body,
                "페이지": _page_of(m.start(), page_offsets),
            })
        if arts:
            return arts, "chapter"

    # ── 2.5순위: 로마숫자 장. 앞 전략이 하나도 성립하지 않았을 때만 시도한다 ──
    romans = _roman_chapters(text, page_offsets)
    if romans:
        return romans, "roman_chapter"

    # ── 3순위: 단락 분할 (구조 없음) ────────────────────────────
    # 짧은 조각을 버리지 않고 이어 붙인다 — HWP 표는 셀마다 한 줄이라 대부분 100자 미만이다.
    # 표의 행–열 짝은 포기한다(셀 텍스트만 남는다).
    조각 = [x.strip() for x in re.split(r"\n{2,}", text) if x.strip()]
    if len(조각) <= 1:
        # 빈 줄이 없는 문서(예: LibreOffice 변환 docx)는 문서 전체가 한 조각이 된다 — 단일 개행으로 다시 가른다.
        조각 = [x.strip() for x in re.split(r"\n", text) if x.strip()]
    묶음: list[str] = []
    버퍼: list[str] = []
    for x in 조각:
        버퍼.append(x)
        if sum(len(y) for y in 버퍼) + len(버퍼) - 1 >= _단락_최소:
            묶음.append("\n".join(버퍼))
            버퍼 = []
    if 버퍼:
        # 남은 조각을 버리지 않는다.
        if 묶음:
            묶음[-1] = 묶음[-1] + "\n" + "\n".join(버퍼)
        else:
            묶음.append("\n".join(버퍼))
    arts = [{
        "조번호": f"단락{i+1:03d}",
        "조제목": None,
        "조순번": i + 1,
        "본문": p,
        "페이지": None,
    } for i, p in enumerate(묶음)]
    return arts, "paragraph"


def _tail(부칙: str | None, 붙임들: list[tuple[str, str]]) -> list[dict]:
    """부칙 + 붙임/별표를 각각 독립 조로. 규정의 실제 내용은 붙임에 있다."""
    arts: list[dict] = []
    if 부칙:
        bms = list(RE_조.finditer(부칙)) or list(RE_조_제목없음.finditer(부칙))
        if bms:
            for a in _build(부칙, bms, {}, titled=bool(RE_조.search(부칙))):
                a["조번호"] = "부칙 " + a["조번호"]
                a["조순번"] = None              # 조번호 역전 검사에서 제외
                arts.append(a)
        else:
            # TIPS 부칙은 `1. 동 지침은 …부터 시행한다` 라 조 패턴이 없다
            arts.append({"조번호": "부칙", "조제목": None, "조순번": None,
                         "본문": 부칙.strip(), "페이지": None})
    for label, seg in 붙임들:
        arts.append({"조번호": label, "조제목": seg.split("\n", 1)[0][:60],
                     "조순번": None, "본문": seg, "페이지": None})
    return arts


def _build_outline(text, ms, page_offsets, base: int = 0) -> list[dict]:
    """개요형 표제 → 조 레코드. 조번호는 문서가 스스로를 인용하는 표기(`7.`)를 그대로 쓴다."""
    arts = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        n = int(m.group(1))
        arts.append({
            "조번호": f"{n}.",
            "조제목": m.group(2).strip() or None,
            "조순번": n,
            "본문": text[m.start():end].strip(),
            "페이지": _page_of(base + m.start(), page_offsets),
        })
    return arts


def _build(text, ms, page_offsets, titled: bool, base: int = 0) -> list[dict]:
    """조 표제 매치 → 조 레코드.

    같은 조번호가 두 번 나오면 — 조제목이 같으면 목차 중복이라 긴 쪽 하나만 남기고,
    조제목이 다르면 원문 오류라 둘 다 남기고 뒤엣것에 `[2]` 를 붙인다(DB 가 UNIQUE(doc_id, 조번호)).
    """
    arts: list[dict] = []
    by_key: dict[tuple[str, str | None], dict] = {}
    dup_count: dict[str, int] = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        num, branch = m.group(1), m.group(2)
        조번호 = f"제{num}조" + (f"의{branch}" if branch else "")
        조제목 = m.group(3).strip() if titled else None
        body = text[m.start():end].strip()

        key = (조번호, 조제목)
        if key in by_key:                      # 목차 중복 — 긴 쪽 채택
            prev = by_key[key]
            if len(body) > len(prev["본문"]):
                prev["본문"] = body
            continue

        dup_count[조번호] = dup_count.get(조번호, 0) + 1
        표기 = 조번호 if dup_count[조번호] == 1 else f"{조번호}[{dup_count[조번호]}]"
        rec = {
            "조번호": 표기,
            "조제목": 조제목,
            "조순번": int(num),
            "본문": body,
            "페이지": _page_of(base + m.start(), page_offsets),
        }
        if dup_count[조번호] > 1:
            rec["원문_조번호중복"] = 조번호
            for a in arts:                     # 첫 번째에도 표시해 둔다
                if a["조번호"] == 조번호:
                    a["원문_조번호중복"] = 조번호
        by_key[key] = rec
        arts.append(rec)
    return arts


# ── 검증 (조번호 역전, 조 개수, 빈 조 비율) ──────────────────────────────
# 폐지된 조·별표·서식. 법령 XML 은 지우지 않고 자리만 남긴다.
#   `제11조 삭제 <2003.8.26>` · `[별표 2의2] 삭제 <2021.3.16>` · `[별지 제9호서식] 삭제 <1996.3.30>`
# 조 형태만 잡으면 별표·서식 폐지분이 빈 조로 남아 빈 조 비율 검사를 오염시킨다.
RE_DELETED = re.compile(
    r"^(?:[■□▶]\s*)?(?:[^\n]{0,40}?)?"
    r"(?:제\s*\d+\s*조(?:의\s*\d+)?|[\[【]\s*(?:별표|별지|서식|붙임)[^\]】]{0,20}[\]】])"
    r"\s*삭\s*제\s*[<(]")


def is_deleted(art: dict) -> bool:
    """폐지 조문인가. 빈 조(추출 실패)와 구분한다 — 폐지 조는 검색 대상에 넣지 않는다."""
    return bool(RE_DELETED.match((art.get("본문") or "").strip()))


def validate(arts: list[dict], strategy: str) -> dict:
    """반환: {ok, quality, flags[]}. ok 는 조가 하나라도 있는가이고, flags 는 l3_parse 가 warn 판정에 쓴다."""
    flags = []

    # 조 개수
    if len(arts) < 5:
        flags.append(f"조개수부족:{len(arts)}")

    # 조번호 역전 — 원문 조번호 중복으로 생긴 역전은 파싱 실패가 아니라 따로 센다
    dup = {a["원문_조번호중복"] for a in arts if a.get("원문_조번호중복")}
    seq = [a for a in arts if a.get("조순번") is not None]
    breaks, src = [], []
    for i in range(len(seq) - 1):
        a, b = seq[i], seq[i + 1]
        if b["조순번"] < a["조순번"]:
            pair = (a["조순번"], b["조순번"])
            if a.get("원문_조번호중복") or b.get("원문_조번호중복"):
                src.append(pair)
            else:
                breaks.append(pair)
    if breaks:
        s = ", ".join(f"{a}→{b}" for a, b in breaks[:3])
        flags.append(f"조번호역전:{len(breaks)}건 ({s})")
    if dup:
        flags.append(f"원문오류:조번호_중복({', '.join(sorted(dup))})")

    # 빈 조 비율 — 삭제 조문은 빈 조가 아니라 원문 사실이다. 이 검사는 텍스트 추출 실패를 잡는다
    deleted = sum(1 for a in arts if is_deleted(a))
    live = [a for a in arts if not is_deleted(a)]
    empty = sum(1 for a in live if len(a["본문"]) < 50)
    if live and empty / len(live) > 0.10:
        flags.append(f"빈조과다:{empty}/{len(live)}")
    if deleted:
        flags.append(f"참고:삭제조({deleted})")

    # 구조 품질
    if strategy == "paragraph":
        flags.append("구조없음:단락분할")
    elif strategy == "chapter":
        flags.append("구조약함:장단위")

    quality = "low" if (strategy in ("paragraph", "chapter") or len(arts) < 5) else "high"
    ok = len(arts) > 0
    return {"ok": ok, "quality": quality, "flags": flags}

