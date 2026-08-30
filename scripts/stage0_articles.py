# -*- coding: utf-8 -*-
"""Stage 0-d : 평문 → 조(條) 단위 재조립 + 검증 게이트 V1~V6.

파이프라인 문서 §2.3(3단 fallback) / §2.4(검증 게이트) 구현.

전략 4종 (앞에서부터 시도)
    jo_titled         제N조(제목)          법령·지침·규정·규칙 (대부분)
    outline_numbered  제N장 > N. > 가.     TIPS 총괄 운영지침 계열 (조 체계 아님)
    jo_bare / jang    제N조 / 제N장        구조 약함
    paragraph         빈 줄 2개            구조 없음 → 판정 인덱스 제외
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


# ── 섹션 경계 ───────────────────────────────────────────────────
# 부칙 경계 — 이후의 제N조는 본칙과 번호가 겹친다
RE_BUCHIK = re.compile(r"(?:^|\n)\s*부\s{0,4}칙\s*(?=[<(\n])")

# 붙임/별표/참고 섹션 헤더. 괄호는 [] 【】 <> 세 종류가 실측된다.
#   [참고1] · 【붙임 1】 · [별지 제①호] · [별지서식] · < 붙임1 > · < 별첨2-1 >
# 여는 괄호를 **필수**로 둔다. 없으면 본문 문장이 걸린다.
# 닫는 괄호 뒤 구두점도 막는다 — TIPS 별첨1 의 제재사유 표에 `<붙임1>, 운영사가 …`
# 같은 표 행이 있어 별첨1 이 세 조각으로 쪼개졌다. 표제 뒤에는 제목이나 줄바꿈이 온다.
RE_ATTACH = re.compile(
    r"(?:^|\n)[ \t]*[\[【<]\s*(붙임|별표|별지|서식|참고|별첨)\s*([^\]】>\n]{0,12}?)\s*[\]】>]"
    r"[ \t]*(?![,、。·;:])([^\n]{0,60})")

# 목차의 점선 지도(leader dots)
RE_DOTS = re.compile(r"·{5,}[^\n]*")

# 원문자 → 아라비아 숫자.  [별지 제①호] (모두의창업)
_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)}


def _cut_toc(text: str) -> int:
    """목차 끝 오프셋. 없으면 0.

    점선 지도가 10회 이상 나오고 마지막이 앞 30% 안이면 그때까지가 목차다.
    실측: TIPS 96회(6%) · 도약 51회(17%) · 모두의창업 38회(7%), 나머지 5건은 0회.
    목차를 남기면 개요형(`N.`)에서 표제 번호가 본문과 두 번 돌아 분해가 깨진다.
    """
    dots = list(RE_DOTS.finditer(text))
    if len(dots) >= 10 and dots[-1].start() < len(text) * 0.30:
        return dots[-1].end()
    return 0


def _find_buchik(text: str) -> int | None:
    """부칙 시작 오프셋. 줄머리 마커 + **뒤 300자에 '시행' 언명**이 있는 첫 마커.

    위치 비율이나 조 개수로 거르던 옛 가드는 둘 다 틀린다:
      - 비율: 붙임이 길면 부칙이 앞으로 밀린다 (초격차 27% · 창업중심대학 32%)
      - 조 개수: TIPS 부칙은 `제N조` 가 아니라 `1. 동 지침은 …부터 시행한다` 다
    실측 8건에서 후보 17개 전부가 진짜 부칙이었고 오탐은 0이다.
    """
    for m in RE_BUCHIK.finditer(text):
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

    순서가 중요하다. **부칙을 먼저 찾고, 붙임은 부칙 뒤에서만 찾는다.**
    실측 근거 — 8건 전수에서 RE_ATTACH 후보의 부칙 앞/뒤 분포:
        부칙 뒤 21건: 전부 진짜 섹션 헤더
        부칙 앞  4건: 전부 본문 속 참조 (`[별지2]을 준수하여 …`,
                     `[붙임] 1. 국가연구개발과제 포함)의 …`)
    옛 코드는 "문서 길이의 45% 이후" 로 걸렀고 두 방향으로 다 틀렸다:
        놓침 — 창업중심대학 [참고1] 이 33% 라 섹션이 안 됐다.
               부칙 제1조가 5,314자로 부풀며 **비목 정의·집행기준(룰 소스)을 삼켰다**
        오인 — 모두의창업 본문의 `[별지2]을 준수하여…`(56%)가 섹션으로 잡혀
               부칙까지 통째로 들어간 23,729자짜리 가짜 붙임이 만들어졌다
    """
    base = _cut_toc(text)
    body_all = text[base:]

    b = _find_buchik(body_all)
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


# ── 개요형 (TIPS 계열) ──────────────────────────────────────────
RE_OUTLINE = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})\.[ \t]*([^\n]{1,40})")


def _outline_headings(body: str) -> list[re.Match]:
    """`N.` 표제만 고른다.

    중첩 열거도 표기가 같아서(`1. 과제수행과 관련이 없거나 …`) 정규식으로는 못 가른다.
    **번호 단조성**으로 가른다 — 표제 번호는 장을 넘어 이어지고, 중첩 열거는 1부터 다시 돈다.
    실측(TIPS 2026): 후보 335개 → 표제 35개(1..35, 역전 0). 목차와 정확히 일치한다.
    """
    out, last = [], 0
    for m in RE_OUTLINE.finditer(body):
        n = int(m.group(1))
        if last < n <= last + 3:   # 결번 허용 3 — 조판 사고로 표제 하나를 놓쳐도 회복한다
            out.append(m)
            last = n
    return out


def split_articles(text: str, page_offsets: dict[int, int] | None = None) -> tuple[list[dict], str]:
    """반환: (조 리스트, 사용한 전략 이름)"""
    text = _clean(text)
    page_offsets = dict(sorted((page_offsets or {}).items()))

    본칙, 부칙, 붙임들, base = _cut_sections(text)

    # ── 1순위: 제N조(제목) ──────────────────────────────────────
    ms = [m for m in RE_JO.finditer(본칙) if not _is_citation(본칙, m.start())]
    if len(ms) >= 5:
        arts = _build(본칙, ms, page_offsets, titled=True, base=base)
        return arts + _tail(부칙, 붙임들), "jo_titled"

    # ── 개요형: 제N장 > N. > 가. (TIPS 총괄 운영지침) ───────────
    oms = _outline_headings(본칙)
    if len(oms) >= 10 and int(oms[-1].group(1)) >= 10:
        arts = _build_outline(본칙, oms, page_offsets, base)
        return arts + _tail(부칙, 붙임들), "outline_numbered"

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


def _tail(부칙: str | None, 붙임들: list[tuple[str, str]]) -> list[dict]:
    """부칙 + 붙임/별표를 각각 독립 조로. 룰 소스의 실체가 붙임 쪽에 있다."""
    arts: list[dict] = []
    if 부칙:
        bms = list(RE_JO.finditer(부칙)) or list(RE_JO_BARE.finditer(부칙))
        if bms:
            for a in _build(부칙, bms, {}, titled=bool(RE_JO.search(부칙))):
                a["조번호"] = "부칙 " + a["조번호"]
                a["조번호_int"] = None          # 단조성 검증에서 제외
                arts.append(a)
        else:
            # TIPS 부칙은 `1. 동 지침은 …부터 시행한다` 라 조 패턴이 없다
            arts.append({"조번호": "부칙", "조제목": None, "조번호_int": None,
                         "본문": 부칙.strip(), "페이지": None})
    for label, seg in 붙임들:
        arts.append({"조번호": label, "조제목": seg.split("\n", 1)[0][:60],
                     "조번호_int": None, "본문": seg, "페이지": None})
    return arts


def _build_outline(text, ms, page_offsets, base: int = 0) -> list[dict]:
    """개요형 표제 → 조 레코드.

    조번호는 문서가 스스로를 인용하는 표기를 그대로 쓴다 —
    TIPS 부칙이 *"이 지침 7.(사업신청·접수), 9.(창업기업 선정평가) 중 나"* 라고 쓴다.
    `제7조` 로 바꿔 적으면 인용이 원문과 어긋난다 (구현.md 원칙 4: 인용은 추출이다).
    """
    arts = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        n = int(m.group(1))
        arts.append({
            "조번호": f"{n}.",
            "조제목": m.group(2).strip() or None,
            "조번호_int": n,
            "본문": text[m.start():end].strip(),
            "페이지": _page_of(base + m.start(), page_offsets),
        })
    return arts


def _build(text, ms, page_offsets, titled: bool, base: int = 0) -> list[dict]:
    """조 헤딩 매치 → 조 레코드.

    같은 조번호가 두 번 나오는 경우가 두 가지인데 처리가 정반대다.
      목차 중복    조제목이 같다   → 긴 쪽 하나만 남긴다
      원문 오류    조제목이 다르다 → **둘 다 남긴다.** 뒤엣것에 `[2]` 를 붙인다
    실측(창업중심대학 2025): 원문에 제35조가 둘이다 —
    제35조(이의신청) 과 제35조(권리 의무 이전). 옛 코드는 조번호만으로 묶어
    긴 쪽 본문을 채택했고, 그 결과 **조제목은 '이의신청' 인데 본문은 '권리 의무 이전'**
    인 레코드가 만들어졌다. 이의신청 조문은 코퍼스에서 통째로 사라졌다.
    DB 는 UNIQUE(doc_id, 조번호) 라 접미 없이는 둘을 같이 넣을 수도 없다.
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
            "조번호_int": int(num),
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


# ── 검증 게이트 V1~V6 ───────────────────────────────────────────
# 폐지된 조. 법령 XML 은 조를 지우지 않고 자리만 남긴다.
#   `제11조 삭제 <2003.8.26>` · `제3조 삭제<2016.2.12>`
RE_DELETED = re.compile(r"^제\s*[\d]+\s*조(?:의\s*\d+)?\s*삭\s*제\s*[<(]")


def is_deleted(art: dict) -> bool:
    """폐지 조문인가. 빈 조(추출 실패)와 구분해야 한다.

    효력이 없으므로 판정 인덱스에도 넣지 않는다 — 폐지된 조를 근거로 인용하면 오답이다.
    Stage 2 가 이 판정으로 걸러낸다.
    """
    return bool(RE_DELETED.match((art.get("본문") or "").strip()))


def validate(arts: list[dict], strategy: str) -> dict:
    """반환: {ok, quality, flags[]}"""
    flags = []

    # V2 조 개수
    if len(arts) < 5:
        flags.append(f"V2:조_개수_부족({len(arts)})")

    # V1 조 번호 단조 증가
    #   원문이 조번호를 중복시켜 생긴 역전은 파싱 실패가 아니다. 따로 센다 —
    #   섞어 세면 진짜 조판 사고(컬럼 혼입)를 이 소음에 묻어버린다.
    dup = {a["원문_조번호중복"] for a in arts if a.get("원문_조번호중복")}
    seq = [a for a in arts if a.get("조번호_int") is not None]
    breaks, src = [], []
    for i in range(len(seq) - 1):
        a, b = seq[i], seq[i + 1]
        if b["조번호_int"] < a["조번호_int"]:
            pair = (a["조번호_int"], b["조번호_int"])
            if a.get("원문_조번호중복") or b.get("원문_조번호중복"):
                src.append(pair)
            else:
                breaks.append(pair)
    if breaks:
        s = ", ".join(f"{a}→{b}" for a, b in breaks[:3])
        flags.append(f"V1:조번호_비단조({len(breaks)}건: {s})")
    if dup:
        flags.append(f"원문오류:조번호_중복({', '.join(sorted(dup))})")

    # V3 빈 조 비율
    #   삭제 조문(`제11조 삭제 <2003.8.26>`)은 빈 조가 아니라 **원문 사실**이다.
    #   법령 XML 은 폐지된 조를 자리만 남겨 두므로 이걸 세면 오탐이 쏟아진다 —
    #   실측: L1 219건의 빈 조 1,964개 중 1,667개(85%)가 삭제 조문이었고
    #   V3 경고 46건이 전부 여기서 나왔다. V3 는 **텍스트 추출 실패**를 잡는 검사다.
    deleted = sum(1 for a in arts if is_deleted(a))
    live = [a for a in arts if not is_deleted(a)]
    empty = sum(1 for a in live if len(a["본문"]) < 50)
    if live and empty / len(live) > 0.10:
        flags.append(f"V3:빈조_과다({empty}/{len(live)})")
    if deleted:
        flags.append(f"참고:삭제조({deleted})")

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
