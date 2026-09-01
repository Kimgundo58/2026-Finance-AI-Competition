# -*- coding: utf-8 -*-
"""(5) 검증·강등기. LLM 출력 [1겹] → 최종 응답 [2겹].

기준 문서: `LLM.md` §3-4(스키마·강등) · §3-7(S번호 사양) · `rule_base.md`(verified 규칙)

원칙 하나로 요약하면 — **LLM 이 자칭한 것은 아무것도 믿지 않는다.**
인용은 s맵으로, 신뢰등급은 `documents.extraction` 으로, 전제는 F 스키마 컬럼으로 검증한다.
검증에 실패한 것은 **고치지 않고 폐기하거나 강등한다.** 기본값은 판단불가다.

═══ 조립기(중앙세션)와의 계약 ═══
    s맵: dict[str, tuple[str, int|None, str|None]]
         {"S01": ("chunk", 12345, "①"), "S07": ("article", 6789, None), ...}
    def 검증(llm출력: dict, s맵: dict) -> tuple[dict, list[str]]

⚠️ 위치인자 2개는 계약 그대로다. 아래 `*` 뒤 키워드는 **전부 기본값이 있어**
   `검증(출력, s맵)` 2인자 호출이 그대로 동작한다. 조립기 코드는 바꿀 필요가 없다.
   키워드를 둔 이유: 강등 규칙 중 셋(vlm·verified·F스키마)이 DB 사실을 필요로 하는데,
   그걸 함수 안에서 직접 조회하면 테스트가 DB 없이는 못 돈다.
   기본값은 라이브 DB 조회이고, 테스트는 스텁을 넣는다.

═══ 층 B — 해야할일 설명 환각 대조 (2026-09-01 추가) ═══
    기준 문서는 `프로토타입_해부_구현명세.md` §2-7. 키워드 2개가 늘었다:
        f사실   dict | None   B5(F축 협약·집행 현황)의 **값**. 문자열이 아니라 dict
        프롬프트 str          조립된 B0~B6 전문. 숫자 화이트리스트의 원천

    🔴 **둘 다 안 넘기면 이 층은 잠들어 있다.** 조립기(`orchestrate.py`)가 넘기기
       전까지 무발효이고, 기존 호출은 아무것도 바뀌지 않는다. 그게 의도다 —
       조립기는 오늘 다른 세션이 열어 둔 파일이라 여기서 같이 고치지 않는다.
    🔴 **선행조건 1건 (스키마 소유자 결정).** 새 강등코드 4종은
       `04_agent.sql` 의 `decisions_강등코드_check` 18종 목록 밖이라 **INSERT 가 거부된다.**
       그 CHECK 에 4종을 더하기 전에는 발효시키지 마라.

실행:
    PYTHONIOENCODING=utf-8 python scripts/llm_validate.py --self-test
    PYTHONIOENCODING=utf-8 python scripts/llm_validate.py --f-paths   # F 경로 실측 출력
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from typing import Any, Callable, Iterable, Optional

# 훅이 PYTHONIOENCODING=utf-8 을 강제하므로 보통 이미 utf-8 이다.
# 조건 없이 다시 감싸면 **import 될 때 앞의 래퍼가 GC 되며 버퍼가 닫힌다** —
# llm_validate 가 llm_schema 를 import 하는 순간 터졌다.
if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # PYTHONPATH 없이 동작
from _lib import db  # noqa: E402
from llm_schema import 판정_ENUM, 미충족시_ENUM, 인용, 전제, 최종응답  # noqa: E402

DSN = db.DSN

# ════════════════════════════════════════════════════════════════════════════
# F 필드 경로 — `tenant` 실제 컬럼에서 만든다 (하드코딩 금지)
# ════════════════════════════════════════════════════════════════════════════
# `서비스 아키텍쳐.md` §2-3 의 축 번호 ↔ 테이블. 경로 표기는 §222 의
# `["F1.정부지원.현금", ...]` 형식이다 — 컬럼 `정부지원_현금` 의 `_` 가 `.` 이 된다.
F축_테이블 = {"F1": "f_profile", "F3": "f_exec", "F4": "f_personnel"}
# f_profile 안에 살지만 축 번호가 따로 붙은 것
F축_특례 = {"F2": {"과업범위요약"}}
# 저장하지 않는 축 (판정 후 폐기) — 테이블이 없으므로 문서의 항목명을 그대로 연다
F5_항목 = {"친족", "전직임직원", "구매명의"}
_메타컬럼 = {"profile_id", "org_id", "exec_id", "person_id", "created_at", "updated_at"}


def f_경로집합(dsn: str | None = None) -> set[str]:
    """허용되는 F 필드 경로 전체. DB 컬럼이 기준 문서이다."""
    out: set[str] = {f"F5.{x}" for x in F5_항목}
    for 축, 컬럼 in F축_특례.items():
        out |= {f"{축}.{c}" for c in 컬럼}
    with db.connect(dsn) as conn:
        for 축, t in F축_테이블.items():
            for (c,) in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='tenant' AND table_name=%s", (t,)).fetchall():
                if c in _메타컬럼:
                    continue
                out.add(f"{축}." + c.replace("_", "."))
    return out


def _항_추출(본문: str, 항호: str | None) -> tuple[str, str]:
    """조 본문에서 해당 항만 잘라낸다. (원문, 범위)

    🔴 **자르기지 요약이 아니다.** 반환값은 반드시 본문의 **정확한 부분 문자열**이다
    (CLAUDE.md 원칙 4 — 인용은 생성이 아니라 추출).
    마커를 못 찾으면 조 전체를 돌려주고 그 사실을 범위로 알린다. 지어내지 않는다.
    """
    if not 본문:
        return "", "없음"
    if not 항호:
        return 본문, "조전체"
    k = 본문.find(항호)
    if k < 0:
        return 본문, "조전체(항호 미발견)"
    뒤 = [본문.find(m, k + 1) for m in _항마커 if 본문.find(m, k + 1) > 0]
    end = min(뒤) if 뒤 else len(본문)
    잘린 = 본문[k:end].strip()
    return (잘린, "항") if 잘린 else (본문, "조전체")


_항마커 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_RE_조 = __import__("re").compile(r"제\d+조")


def s번호_메타(s맵: dict, dsn: str | None = None) -> dict[str, dict]:
    """s맵의 각 S번호 → 치환·등급산정에 필요한 DB 사실.

    §3-4 [2겹] `인용[].조번호·원문 | 코드 | S번호 → DB 원문 치환` 을 여기서 한다.
    반환 키: doc_id · 조번호 · 조제목 · 원문 · 원문범위 · version · extraction · 항호_DB
    """
    청크 = {sid: i for sid, (k, i, _) in s맵.items() if k == "chunk" and i is not None}
    조문 = {sid: i for sid, (k, i, _) in s맵.items() if k == "article" and i is not None}
    l3 = {sid: i for sid, (k, i, _) in s맵.items() if k == "l3" and i is not None}
    항호 = {sid: h for sid, (_, _, h) in s맵.items()}
    out: dict[str, dict] = {}

    with db.connect(dsn) as conn:
        # chunk — 청크 자체가 이미 항 단위(§3-7)라 text 를 그대로 쓴다. 자르지 않는다.
        if 청크:
            m = {r[0]: r for r in conn.execute("""
                SELECT c.chunk_id, c.doc_id, c.조번호, c.조제목, c.항호,
                       COALESCE(c.version, d.version), d.extraction, c.text,
                       COALESCE(c.기관id, d.기관id), d.domain, c.layer
                  FROM corpus.chunks c JOIN corpus.documents d ON d.doc_id = c.doc_id
                 WHERE c.chunk_id = ANY(%s)""", (list(청크.values()),)).fetchall()}
            for sid, cid in 청크.items():
                if cid in m:
                    _, doc, 조, 제목, h, ver, ex, txt, 기관, dom, lay = m[cid]
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=txt or "",
                                    원문범위="청크", version=ver, extraction=ex, 항호_DB=h,
                                    기관id=기관, domain=dom, layer=lay)
        # article — 조 전체가 오므로 s맵의 항호로 잘라낸다
        if 조문:
            m = {r[0]: r for r in conn.execute("""
                SELECT a.article_id, a.doc_id, a.조번호, a.조제목, a.본문,
                       d.version, d.extraction, d.기관id, d.domain, d.layer
                  FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id = a.doc_id
                 WHERE a.article_id = ANY(%s)""", (list(조문.values()),)).fetchall()}
            for sid, aid in 조문.items():
                if aid in m:
                    _, doc, 조, 제목, 본문, ver, ex, 기관, dom, lay = m[aid]
                    원문, 범위 = _항_추출(본문, 항호.get(sid))
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=원문,
                                    원문범위=범위, version=ver, extraction=ex, 항호_DB=None,
                                    기관id=기관, domain=dom, layer=lay)
        # l3 — tenant. extraction·version 축이 없다. 기관 업로드분이라 native 로 본다.
        # 🔴 `기관id` 자리에는 `org_id` 를 넣는다 — 멀티테넌시 3차 방어의 대조 대상이다.
        if l3:
            m = {r[0]: r for r in conn.execute("""
                SELECT article_id, doc_id::text, 조번호, 조제목, 본문, org_id::text
                  FROM tenant.l3_articles WHERE article_id = ANY(%s)""",
                (list(l3.values()),)).fetchall()}
            for sid, aid in l3.items():
                if aid in m:
                    _, doc, 조, 제목, 본문, org = m[aid]
                    원문, 범위 = _항_추출(본문, 항호.get(sid))
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=원문,
                                    원문범위=범위, version=None, extraction="native",
                                    항호_DB=None, 기관id=org, domain="창업지원사업",
                                    layer="L3")
    return out


def 사고기록(종류: str, 상세: dict, *, decision_id=None, org_id=None,
          dsn: str | None = None) -> None:
    """`tenant.incidents` 적재. 🔴 실패해도 판정을 죽이지 않는다 — 로깅이 판정보다 위는 아니다.

    D1-d 가 만들기 전이면 테이블이 없다. 그때는 stderr 로만 남긴다 — 사고를 **조용히**
    삼키는 경로는 만들지 않는다.
    """
    import json as _json

    try:
        with db.connect(dsn) as conn:
            컬럼 = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='tenant' AND table_name='incidents'").fetchall()}
            if not 컬럼:
                raise RuntimeError("tenant.incidents 없음")
            행 = {"종류": 종류, "상세": _json.dumps(상세, ensure_ascii=False, default=str)}
            if decision_id is not None and "decision_id" in 컬럼:
                행["decision_id"] = decision_id
            if org_id is not None and "org_id" in 컬럼:
                행["org_id"] = org_id
            키 = [k for k in 행 if k in 컬럼]
            conn.execute(f'INSERT INTO tenant.incidents ({",".join(chr(34)+k+chr(34) for k in 키)})'
                         f' VALUES ({",".join(["%s"] * len(키))})', [행[k] for k in 키])
            conn.commit()
    except Exception as e:
        print(f"🔴 사고 로그 실패({종류}): {type(e).__name__}: {e} · 상세={상세}",
              file=sys.stderr)


# ════════════════════════════════════════════════════════════════════════════
# 층 B — 해야할일 설명 환각 대조. 기준 문서는 `프로토타입_해부_구현명세.md` §2-7
# ════════════════════════════════════════════════════════════════════════════
# 여기서 막는 건 «없는 상태» 와 «없는 숫자» 다. 인용(S번호)과 항목(code)은 이미 위에서
# 폐쇄 목록으로 닫혀 있는데 `해야할일[].설명` 만 아무 검사 없이 화면으로 나갔다.
#
# 🔴 **처분은 항목 폐기가 아니라 «설명 떨어뜨리기»다.** 항목과 code 는 남긴다 —
#    화면은 code 로 `check_items` 마스터의 정적 설명을 조회할 수 있으므로(§2-4),
#    설명이 비면 **판정 이전의 고정 문구로 대체**한다. 기능을 죽이지 않으면서
#    "없는 상태를 문장으로 만들지 않는다" 가 지켜진다. 항목까지 지우면 룰이 고른
#    확인항목이 사라져 오히려 위험해진다 — 설명이 나쁜 것과 항목이 틀린 것은 다르다.
#
# 🔴 **`f사실=None` 과 `f사실={}` 은 다르다.**
#      None  조립기가 안 넘겼다 = 우리가 모른다  → 상태 규칙 무발효
#      {}    F축이 비었다(게스트)                → 상태 규칙 최대 강도
#    뭉치면 조립기가 인자를 빠뜨린 순간 게스트 가드가 **조용히** 꺼진다.
_RE_숫자 = re.compile(r"\d[\d,]*")

# 자유문(층 A 이전) 모드에서 «상태 서술» 을 알아보는 어구. 문장에서 절만 떼어낼 수는
# 없으므로 이게 걸리면 설명 전체를 떨어뜨린다. 층 A(상태절 필드 분리)가 붙으면 이
# 휴리스틱은 쓰이지 않는다 — **구조가 패턴보다 정확하다.** 패턴은 임시 그물이다.
_RE_상태어구 = re.compile(
    r"현재|귀사|귀 ?기관|님의|보이고|보입니다|상태(로|이|입니다|여서)|"
    r"등록(된|돼|되어)|가입(된|돼|되어|하신)|채용(하신|한|된)|집행(하신|한|중)")

_설명키 = ("설명", "상태절", "근거절", "행동절")


def _숫자들(s: str) -> set[str]:
    """문자열의 숫자 토큰. 콤마를 떼고 앞의 0 을 죽여 비교한다 ('1,000,000' == '1000000')."""
    return {(m.group().replace(",", "").lstrip("0") or "0")
            for m in _RE_숫자.finditer(s or "")}


def _출처숫자(*원천: str) -> set[str]:
    """설명에 나와도 되는 숫자 전부 — **프롬프트에 실제로 있던 것만**이다.

    만/억 표기를 편다: 원천이 '5,000,000' 인데 설명이 '500만원' 이면 같은 값이다.
    반대 방향(원천 '500만' → 설명 '5000000')은 열지 않는다. 넓힐수록 환각이 통과하고,
    좁아서 나는 오폐기는 **정적 문구 대체 경로**이라 안전하다. 오답 비대칭 그대로다.
    """
    허용: set[str] = set()
    for s in 원천:
        허용 |= _숫자들(s)
    확장: set[str] = set()
    for n in 허용:
        if n.isdigit():
            v = int(n)
            if v >= 10_000 and v % 10_000 == 0:
                확장.add(str(v // 10_000))            # 500만
            if v >= 100_000_000 and v % 100_000_000 == 0:
                확장.add(str(v // 100_000_000))       # 1억
    return 허용 | 확장


def _설명검사(h: dict, *, f사실: Optional[dict], 허용숫자: Optional[set[str]],
           s맵: dict) -> list[tuple[str, str, str]]:
    """항목 1건을 대조한다. `[(강등코드, 사유, 제거대상)]` — 빈 리스트면 통과.

    제거대상: `'상태절'` = 그 절만 · `'설명'` = 설명 계열 키 전부
    """
    구조 = any(k in h for k in ("상태절", "행동절", "근거S"))      # 층 A 스키마인가
    상태절 = str(h.get("상태절") or "").strip()
    본문 = " ".join(str(h.get(k) or "") for k in _설명키).strip()
    이름 = str(h.get("항목") or "")[:20]
    위반: list[tuple[str, str, str]] = []

    # 1. 게스트 — F축이 비었는데 상태를 말했다. §2-6 이 지목한 그 환각이다
    if f사실 is not None and not f사실:
        if 구조 and 상태절:
            위반.append(("TASK_STATE_UNSOURCED",
                        f"해야할일 '{이름}' 상태절 '{상태절[:24]}' — F축(B5)이 비었다 → 상태절 폐기",
                        "상태절"))
        elif not 구조 and _RE_상태어구.search(본문):
            위반.append(("TASK_STATE_UNSOURCED",
                        f"해야할일 '{이름}' 설명이 F축 없이 상태를 서술 → 설명 폐기(정적 문구 폴백)",
                        "설명"))
    # 2. 상태절의 숫자는 **F축에서만** 온다. 규정 원문의 숫자를 상태로 옮겨 적으면 안 된다
    #    ("한도가 500만원" 은 근거절이고, "현재 500만원을 집행" 은 상태절이다)
    elif f사실 and 구조 and 상태절:
        밖 = sorted(_숫자들(상태절) - _출처숫자(*[str(v) for v in f사실.values()]))
        if 밖:
            위반.append(("TASK_STATE_MISMATCH",
                        f"해야할일 '{이름}' 상태절 숫자 {밖} 가 F축 값에 없다 → 상태절 폐기",
                        "상태절"))
    # 3. 숫자 출처 — 프롬프트에 없던 숫자는 지어낸 것이다. 한도·기한이 여기로 샌다
    if 허용숫자 is not None:
        밖 = sorted(_숫자들(본문) - 허용숫자)
        if 밖:
            위반.append(("TASK_NUMBER_UNSOURCED",
                        f"해야할일 '{이름}' 숫자 {밖} 가 프롬프트에 없다 → 설명 폐기",
                        "설명"))
    # 4. 근거 S번호 — 층 A 가 붙어 `근거S` 필드가 생기면 **자동 발효**한다.
    #    인용과 같은 잣대다 (s맵 밖 = 환각 인용).
    if 구조 and "근거S" in h and h.get("근거S") not in s맵:
        위반.append(("TASK_BASIS_NOT_IN_MAP",
                    f"해야할일 '{이름}' 근거S '{h.get('근거S')}' 가 s맵 밖 → 설명 폐기",
                    "설명"))
    return 위반


def _설명제거(h: dict, 대상: str) -> None:
    """🔴 항목·code 는 남긴다. 화면이 `check_items` 정적 설명으로 폴백한다."""
    for k in (("상태절",) if 대상 == "상태절" else _설명키):
        h.pop(k, None)


# ════════════════════════════════════════════════════════════════════════════
# 검증·강등
# ════════════════════════════════════════════════════════════════════════════
def 검증(llm출력: dict, s맵: dict, *,
         메타: Optional[dict[str, dict]] = None,
         f경로: Optional[Iterable[str]] = None,
         룰들: Optional[list[dict]] = None,
         체크코드: Optional[Iterable[str]] = None,
         현재기관: Optional[str] = None,
         사업명: Optional[str] = None,
         dangling: Optional[list] = None,
         l3게이팅: Optional[dict] = None,
         룰: Optional[dict] = None,
         f사실: Optional[dict] = None,
         프롬프트: str = "",
         decision_id=None,
         org_id=None,
         dsn: Optional[str] = None) -> tuple[dict, list[str]]:
    """(검증·강등된 출력, 강등사유 목록). 반환 dict 에 `강등코드` 22종이 함께 실린다.

    메타/f경로 를 주지 않으면 DB 에서 읽는다. 테스트는 스텁을 넣어 DB 없이 돈다.
    룰들 은 B4 에 들어간 `effective_rule` 결과다 — `[{"verified": bool, ...}]`.

    2026-08-31 추가 (`Agent.md` §5 미구현 6규칙):
        현재기관   인용의 `기관id` 가 NULL 도 이 값도 아니면 **폐기 + 사고 로그**
        끊긴 참조   참조 확장에서 만난 끊긴 참조 → 경고 + 신뢰등급 하향
        l3게이팅   L3 단독 "가능" → 조건부 강등 (§3-2 (4) 의 실행판)
        룰         precedence 재적용 — L3 근거의 결론이 L2 우선 규칙에 걸리면 뒤집힌다

    2026-09-01 추가 (층 B · §2-7):
        f사실     B5 의 **값** dict. `None`=모름(무발효) / `{}`=게스트(최대 강도)
        프롬프트   조립된 B0~B6 전문. 설명에 쓰인 숫자의 화이트리스트 원천.
                  빈 문자열이면 숫자 대조를 하지 않는다 — 화이트리스트가 비면
                  **모든 숫자가 위반**이 되어 설명이 전멸한다
    """
    사유: list[str] = []
    코드: list[str] = []

    def 깎(코드값: str, 문장: str) -> None:
        사유.append(문장)
        if 코드값 not in 코드:
            코드.append(코드값)

    메타 = 메타 if 메타 is not None else s번호_메타(s맵, dsn)
    허용경로 = set(f경로) if f경로 is not None else f_경로집합(dsn)

    # ── 1. 판정 enum ─────────────────────────────────────────────────────
    # 폐쇄 목록 밖이면 고쳐 맞추지 않는다. 모든 실패의 기본값은 판단불가다.
    판정 = llm출력.get("판정")
    if 판정 not in 판정_ENUM:
        깎("INVALID_JUDGMENT", f"판정 '{판정}' 이 4-way enum 밖 → 판단불가")
        판정 = "판단불가"

    # ── 2. 인용 S번호 ────────────────────────────────────────────────────
    # §3-7: "매핑표에 없는 S번호 인용은 폐기 (환각 인용 차단)"
    인용목록: list[인용] = []
    인용층: list[str] = []
    누수: list[dict] = []
    for s in llm출력.get("인용") or []:
        if s not in s맵:
            깎("CITE_NOT_IN_MAP", f"인용 {s} 가 s맵 밖 → 폐기(환각 인용)")
            continue
        종류, _id, 항호 = s맵[s]
        m = 메타.get(s)
        if m is None:
            # s맵엔 있는데 DB 에 없다 = 조립기가 넘긴 id 가 죽었다. 원문을 못 붙이므로 폐기한다.
            깎("CITE_DB_MISSING", f"인용 {s} 의 원본을 DB 에서 못 찾음({종류} id={_id}) → 폐기")
            continue
        # 🔴 멀티테넌시 3차 방어 (§5). NULL 은 공용 규범(L1·L2)이라 정상이다.
        #    NULL 도 현재기관도 아니면 **남의 기관 규정이 인용된 것** — 그 자체가 오답이다.
        #    고치지 않고 폐기하고 사고로 남긴다. 조용히 넘어가면 다음에도 같은 일이 난다.
        기관 = m.get("기관id")
        if 현재기관 is not None and 기관 is not None and str(기관) != str(현재기관):
            깎("TENANT_LEAK",
              f"인용 {s} 의 기관id={기관} 이 현재기관={현재기관} 도 NULL 도 아님 → 폐기")
            누수.append({"s번호": s, "종류": 종류, "id": _id, "기관id": str(기관),
                        "현재기관": str(현재기관), "doc_id": m.get("doc_id")})
            continue
        # 항호는 s맵(조립기)이 기준 문서이다. 청크가 다른 항을 가리키면 조립 사고이므로 남긴다.
        if m.get("항호_DB") and 항호 and m["항호_DB"] != 항호:
            깎("CITE_HANG_MISMATCH",
              f"인용 {s} 항호 불일치(s맵 {항호} vs DB {m['항호_DB']}) → s맵 값 채택")
        # domain 경고 — 판정을 막지는 않는다. 문구를 삽입하고 등급을 낮춘다.
        if m.get("domain") and m["domain"] != "창업지원사업":
            깎("DOMAIN_WARN",
              f"인용 {s} 의 문서 domain='{m['domain']}' 이 창업지원사업이 아니다 → 경고")
        인용층.append(m.get("layer") or ("L3" if 종류 == "l3" else "?"))
        인용목록.append(인용(s번호=s, doc_id=m.get("doc_id"), 조번호=m.get("조번호"),
                          조제목=m.get("조제목"), 항호=항호,
                          원문=m.get("원문"), 원문범위=m.get("원문범위"),
                          version=m.get("version"), extraction=m.get("extraction")))
    if 누수:
        사고기록("TENANT_LEAK", {"사업명": 사업명, "현재기관": str(현재기관), "인용": 누수},
              decision_id=decision_id, org_id=org_id, dsn=dsn)

    # ── 3. 전제 ──────────────────────────────────────────────────────────
    전제목록: list[전제] = []
    미매핑: list[str] = []
    for p in llm출력.get("전제") or []:
        사실 = (p.get("사실") or "").strip()
        근거 = p.get("근거조항")
        if not 근거:
            깎("PREMISE_NO_BASIS", f"전제 '{사실[:24]}' 근거조항 없음 → 폐기")
            continue
        if 근거 not in s맵:
            깎("PREMISE_BASIS_NOT_IN_MAP",
              f"전제 '{사실[:24]}' 근거조항 {근거} 가 s맵 밖 → 폐기(환각 인용)")
            continue
        미충족 = p.get("미충족시")
        if 미충족 not in 미충족시_ENUM:
            깎("PREMISE_ENUM", f"전제 '{사실[:24]}' 미충족시 '{미충족}' enum 밖 → 불가로 고정")
            미충족 = "불가"
        경로 = list(p.get("매핑") or [])
        밖 = [x for x in 경로 if x not in 허용경로]
        if 밖:
            # 폐기하지 않는다 — 전제 자체는 유효할 수 있다. F 스키마 결손일 수도 있어
            # `tenant.unmapped_premise` 로 쌓아 사람이 본다. (DB 쓰기는 여기서 안 한다)
            깎("PREMISE_UNMAPPED",
              f"전제 '{사실[:24]}' 매핑 {밖} 가 F 스키마 밖 → unmapped_premise 대상")
            미매핑.extend(밖)
        전제목록.append(전제(사실=사실, 근거조항=근거, 매핑=경로,
                          미충족시=미충족, 미매핑=bool(밖)))

    # ── 4. 신뢰등급 — 코드가 산정한다. LLM 자칭 금지 ──────────────────────
    # `documents.extraction='vlm'` = 스캔 판독본. CLAUDE.md "A등급 인용 금지"
    vlm = [c.s번호 for c in 인용목록 if c.extraction == "vlm"]
    if not 인용목록:
        신뢰등급 = None
        if 판정 != "판단불가":
            깎("NO_CITATION", "인용 0건인데 판정이 판단불가가 아니다 → 판단불가")
            판정 = "판단불가"
    elif vlm:
        신뢰등급 = "B"
        깎("VLM_DOWNGRADE", f"인용 {vlm} 가 extraction='vlm'(스캔 판독) → A등급 금지, B 로 강등")
    else:
        신뢰등급 = "A"

    # ── 4-b. 끊긴 참조 — 참조 확장이 끊긴 채로 판정했다 ─────────────────────
    # 판정을 막지는 않는다. 끊긴 참조 너머에 제약이 있었을 수 있으므로 등급만 낮춘다.
    # 🔴 끊긴 참조는 업로드 시점에 알리는 게 원칙이고(§CLAUDE.md), 여기는 마지막 그물이다.
    # 🔴 **조가 지정된 끊긴 참조만 센다** (2026-09-01 C 실측 · A 채택).
    #    진입점이 물고 오는 끊긴 참조문자열 148종 중 146종(98.6%)에 조가 없다
    #    (「국민건강보험법」「보조금법」 같은 문서 통째 인용). 조 없는 인용은 우리가
    #    **애초에 펴지 않기로 한 것**이다 (`RAG.md` §4-3 — 조 없이 펴면 근로기준법
    #    하나가 6,026청크). 그걸로 강등하면 3문항 중 1문항에서 울리는 상시 경보가 되고,
    #    **"근거 불완전" 이 기본 상태가 되면 강등코드가 신호를 잃는다.**
    #    게이팅 후 실측: 정답셋 77문항에서 끊긴 참조 44건 중 조지정 1건 · 1문항.
    조지정_dangling = [x for x in (dangling or []) if _RE_조.search(str(x))]
    if 조지정_dangling:
        if 신뢰등급 == "A":
            신뢰등급 = "B"
        깎("DANGLING_WARN",
          f"참조 폐포에 조 지정된 dangling {len(조지정_dangling)}건 — "
          f"{조지정_dangling[:3]} → 신뢰등급 하향")

    # ── 5. verified=false 룰만으로 '가능' 금지 ────────────────────────────
    # `corpus.rules.verified` 주석: 'false 인 룰만으로 "가능" 판정 금지'
    if 룰들 is not None and 판정 == "가능" and 룰들 and not any(r.get("verified") for r in 룰들):
        깎("UNVERIFIED_RULE", "미검수(verified=false) 룰만으로 '가능' → 조건부로 강등")
        판정 = "조건부"

    # ── 5-c. '가능' 인데 인용이 B등급뿐 ───────────────────────────────────
    # 스캔 판독본만 근거로 "가능" 을 말하면, 판독 오류 하나가 곧바로 치명 오답이 된다.
    if 판정 == "가능" and 인용목록 and 신뢰등급 == "B" and "VLM_DOWNGRADE" in 코드:
        깎("B_GRADE_DOWNGRADE", "'가능' 인데 인용이 B등급(판독본)뿐 → 조건부로 강등")
        판정 = "조건부"

    # ── 5-d. '가능' 인데 인용이 L3 단독 ───────────────────────────────────
    # `Agent.md` §3-2 (4) 의 실행판이다. 주관기관 규정은 국가 지침 위에 얹는 문서라
    # 구조적으로 자족하지 않는다 — "총장 승인만 받으면 됩니다" 만 보고 답하면 상위의
    # "범용성 기자재 소명 필요" 를 놓친 **틀린 '가능'** 이 된다.
    # "불가" 는 L3 단독으로도 안전하므로 건드리지 않는다. 오답 비대칭 그대로다.
    if 판정 == "가능" and 인용층 and all(x == "L3" for x in 인용층):
        깎("L3_ONLY_DOWNGRADE", "'가능' 인데 인용이 L3(기관 규정) 단독 → 조건부로 강등")
        판정 = "조건부"
        if 신뢰등급 == "A":
            신뢰등급 = "B"

    # ── 5-e. precedence 재적용 ────────────────────────────────────────────
    # 🔴 **L3 가 항상 이기는 게 아니다.** 8사업 중 6개가 적용범위 조에서 L2 > L3 를 명시한다
    #    (L3 가 더 엄격해도 진다). L3 단독 근거로 선 "불가" 가 그 규칙에 걸리면 뒤집힌다.
    #
    #    다만 뒤집는 방향이 **관대해지는 쪽**이라 그대로 '가능' 까지 올리지 않는다.
    #    올리려면 L1·L2 조문 인용이 있어야 하는데 그게 없어서 여기 온 것이다.
    #    → 최대 '조건부' 까지만. 오답 비대칭을 여기서도 지킨다. (2026-08-31 A 판단)
    # 🔴 `적용층` 은 이제 `L1+L2` 같은 **결합값**이 될 수 있다 (2026-09-01 B 계약 변경).
    #    층으로 dict/if 분기하면 결합값에서 조용히 빗나간다 — 분기는 `적용층_기여`
    #    (리스트)를 본다. 없으면 옛 계약대로 `적용층` 을 쪼개 쓴다.
    기여 = list((룰 or {}).get("적용층_기여") or
               str((룰 or {}).get("적용층") or "").split("+"))
    if (판정 == "불가" and 인용층 and all(x == "L3" for x in 인용층)
            and any(x in ("L1", "L2") for x in 기여)
            and (룰 or {}).get("허용") in ("가능", "조건부")):
        깎("PRECEDENCE_FLIP",
          f"L3 단독 '불가' 인데 우선 규범이 {'+'.join(기여)}({룰.get('허용')}) → 조건부로 전환")
        판정 = "조건부"

    # ── 5-b. 해야할일 code — check_items 폐쇄 목록 밖이면 폐기 ─────────────
    # code 는 재판정 간 진행상황을 잇는 키다. LLM 이 지어낸 code 를 그대로 두면
    # `tenant.plan_tasks.코드` FK 가 깨지고, 사용자 체크가 다음 판정에 안 붙는다.
    허용코드 = set(체크코드) if 체크코드 is not None else None
    # 🔴 `프롬프트` 가 비면 숫자 대조는 발효하지 않는다 (위 docstring). 조립기가
    #    넘기기 전까지 잠들어 있고, 넘기는 순간 켜진다. 켜는 것은 조립기 쪽 한 줄이다.
    허용숫자 = _출처숫자(프롬프트) if 프롬프트 else None
    해야할일 = []
    for h in (llm출력.get("해야할일") or []):
        if not isinstance(h, dict) or not h.get("항목"):
            continue
        c = h.get("code")
        if 허용코드 is not None and c is not None and c not in 허용코드:
            깎("TASK_CODE_INVALID", f"해야할일 code '{c}' 가 check_items 밖 → 폐기")
            continue
        # ── 층 B. 설명이 나쁜 것과 항목이 틀린 것은 다르다 — 항목·code 는 남긴다 ──
        h = dict(h)                       # 입력 dict 를 제자리에서 고치지 않는다
        for 코드값, 문장, 대상 in _설명검사(h, f사실=f사실, 허용숫자=허용숫자, s맵=s맵):
            깎(코드값, 문장)
            _설명제거(h, 대상)
        해야할일.append(h)

    # ── 6. 버전스탬프 — 인용 문서의 version. 코드가 채운다 ────────────────
    # 인용된 것만 모은다. s맵 전체를 쓰면 인용하지도 않은 문서의 판본이 화면에 붙는다.
    버전 = sorted({c.version for c in 인용목록 if c.version})
    응답 = 최종응답(
        판정=판정,
        요약=(llm출력.get("요약") or "").strip(),
        해야할일=해야할일,
        인용목록=인용목록, 전제목록=전제목록,
        신뢰등급=신뢰등급,
        버전스탬프=", ".join(버전) or None,
        참조사슬=[],                      # 오케스트레이터가 C 의 `참조사슬` 을 넣는다
        강등사유=사유,
        미매핑전제=sorted(set(미매핑)),
    )
    d = 응답.to_dict()
    # 🔴 강등코드 22종 (18 + 층 B 4종). `최종응답` 데이터클래스(`llm_schema.py`)는
    #    오늘도 다른 세션이 열어 둔 파일이라 손대지 않고, 여기서 키를 얹는다.
    #    `tenant.decisions.강등코드` 로 그대로 가는데 — 🔴 `04_agent.sql` 의 CHECK 가
    #    아직 18종이라 **새 4종이 실리면 INSERT 가 거부된다.** 발효 전 선행조건이다.
    d["강등코드"] = 코드
    return d, 사유


def _self_test() -> int:
    """층 B 회귀 — **DB 없이** 돈다 (메타·f경로 스텁).

    🔴 8세션이 한 DB 를 쓰는 날에 회귀가 DB 를 잡으면 남의 작업과 엉킨다.
       이 테스트는 커넥션을 하나도 열지 않는다.
    """
    s맵 = {"S01": ("chunk", 1, None)}
    메타 = {"S01": dict(doc_id="d1", 조번호="제41조", 조제목="인건비", 원문="…",
                       원문범위="청크", version="v1", extraction="native", 항호_DB=None,
                       기관id=None, domain="창업지원사업", layer="L2")}
    기본 = dict(판정="조건부", 요약="요약", 인용=["S01"], 전제=[])
    프롬 = "## B2 제41조 … 한도 5,000,000원 … 30일 이내에 제출한다"
    실패: list[str] = []

    def 돌린다(해야할일, **kw):
        return 검증(dict(기본, 해야할일=해야할일), s맵, 메타=메타, f경로=set(), **kw)

    def 본다(이름: str, 조건: bool) -> None:
        print(f"  {'✅' if 조건 else '🔴'} {이름}")
        if not 조건:
            실패.append(이름)

    # 1. 게스트(f사실={}) · 자유문에 상태 서술 → 설명만 떨어지고 항목·code 는 산다
    d, _ = 돌린다([{"code": "4대보험가입", "항목": "4대보험 확인",
                  "설명": "현재 신규채용 2명이 미가입 상태로 보이므로 확인하세요"}], f사실={})
    t = (d["해야할일"] or [{}])[0]
    본다("게스트 상태서술 → TASK_STATE_UNSOURCED", "TASK_STATE_UNSOURCED" in d["강등코드"])
    본다("게스트 상태서술 → 설명 제거", "설명" not in t)
    본다("게스트 상태서술 → 항목·code 보존", t.get("항목") == "4대보험 확인"
        and t.get("code") == "4대보험가입")

    # 2. f사실=None(조립기 미전달) → 같은 입력이어도 **무발효**. 계약 하위호환
    d, _ = 돌린다([{"code": "4대보험가입", "항목": "4대보험 확인",
                  "설명": "현재 신규채용 2명이 미가입 상태로 보이므로 확인하세요"}])
    본다("f사실=None → 상태 규칙 무발효", "TASK_STATE_UNSOURCED" not in d["강등코드"]
        and (d["해야할일"] or [{}])[0].get("설명"))

    # 3. 숫자 출처 — 프롬프트에 있는 값은 통과, 없는 값은 설명 폐기
    d, _ = 돌린다([{"code": "보증금관리비제외", "항목": "한도 확인",
                  "설명": "한도 500만원 이내로 30일 안에 제출하세요"}], 프롬프트=프롬)
    본다("만 단위 전개(5,000,000 → 500만) 통과",
        "TASK_NUMBER_UNSOURCED" not in d["강등코드"])
    d, _ = 돌린다([{"code": "보증금관리비제외", "항목": "한도 확인",
                  "설명": "60일 안에 제출하세요"}], 프롬프트=프롬)
    본다("없는 숫자(60) → TASK_NUMBER_UNSOURCED", "TASK_NUMBER_UNSOURCED" in d["강등코드"])
    본다("없는 숫자 → 설명 제거", "설명" not in (d["해야할일"] or [{}])[0])

    # 4. 층 A 구조 모드 — 게스트면 **상태절만** 떨어지고 행동절은 산다
    d, _ = 돌린다([{"code": "전대차아님확인", "항목": "전대차 확인",
                  "상태절": "현재 사무실 임차 중이고", "근거S": "S01",
                  "행동절": "임대차계약서 원본을 결제 전에 확인하세요"}], f사실={})
    t = (d["해야할일"] or [{}])[0]
    본다("구조모드 게스트 → 상태절만 제거", "상태절" not in t and t.get("행동절"))

    # 5. F축은 있는데 상태절 숫자가 그 값에 없다 → 지어낸 상태
    d, _ = 돌린다([{"code": "전대차아님확인", "항목": "전대차 확인",
                  "상태절": "현재 임차료 7,000,000원을 집행 중이고", "근거S": "S01",
                  "행동절": "계약서를 확인하세요"}], f사실={"협약총액": 50000000})
    본다("F축 밖 상태절 숫자 → TASK_STATE_MISMATCH", "TASK_STATE_MISMATCH" in d["강등코드"])

    # 6. 근거S 가 s맵 밖 → 인용과 같은 잣대로 폐기
    d, _ = 돌린다([{"code": "전대차아님확인", "항목": "전대차 확인",
                  "근거S": "S99", "행동절": "확인하세요"}])
    본다("근거S s맵 밖 → TASK_BASIS_NOT_IN_MAP", "TASK_BASIS_NOT_IN_MAP" in d["강등코드"])

    # 7. 하위호환 — 인자를 안 주면 해야할일이 **그대로** 나온다 (기존 77문항 경로)
    원본 = {"code": "성공보수아님", "항목": "성공보수 확인", "설명": "현재 5억 계약을 확인"}
    d, _ = 돌린다([dict(원본)])
    본다("2인자 계약 하위호환 — 무변형 통과", (d["해야할일"] or [{}])[0] == 원본)

    print(f"\n{'🔴 실패 ' + str(len(실패)) if 실패 else '✅ 층 B 회귀 전건 통과'}")
    return 1 if 실패 else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--f-paths", action="store_true", help="허용 F 경로를 DB 에서 뽑아 출력")
    ap.add_argument("--self-test", action="store_true", help="층 B 회귀 (DB 불필요)")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if a.f_paths:
        for p in sorted(f_경로집합()):
            print(" ", p)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
