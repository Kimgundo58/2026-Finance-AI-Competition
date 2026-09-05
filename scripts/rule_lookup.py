# -*- coding: utf-8 -*-
"""룰 조회 — 금지목록 즉답 · 비목 확정 · 효력 결정 · L3 게이팅.

`0831_최종구현.md` §4 의 동결 인터페이스 4개를 공급한다 (B 세션 소유).
설계 기준 문서는 `rule_base.md` §3 · `Agent.md` §3-2 §3-3.

    금지적중()      통과 조건 A. LLM 0회로 "불가" 를 즉답한다
    비목확정()      정확조회 → 실패 시 벡터. 통과 조건 C 의 재료
    effective_rule()  L1·L2·L3 를 precedence_rules 로 병합
    l3_게이팅()     상위를 봐야 하는가 — 4갈래

🔴 **이 모듈은 판정을 만들지 않는다.** 강등도 하지 않는다 (A 의 몫).
   룰이 무엇을 말하는지만 돌려주고, 모르는 건 None 으로 정직하게 남긴다.
   `effective_rule() -> None` 은 오류가 아니라 정상이다 — 룰 없는 비목·사업이 있다.

## 왜 유사매칭을 넣지 않았나 (B1)

`corpus.rules.허용` 분포가 조건부 51 / 가능 3 / **불가 0** 이라 "불가" 판정의 유일한
룰 경로가 금지예시 204개 매칭이다. 그래서 매칭을 느슨하게 하고 싶은 압력이 크다.
하지만 금지목록 적중은 **LLM 없이 즉답**하는 경로다 — 오탐이 곧 "틀린 불가"이고,
근거 조문까지 붙여서 나간다. 정규화(NFKC·공백·조사·괄호)와 **문자 그대로의 포함**
까지만 인정하고, 편집거리·부분어·임베딩 유사도는 쓰지 않는다.
재현율은 `_B_금지매칭_감사.json` 에 실측으로 남긴다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/rule_lookup.py --self-test   # DB 없이 (순수 함수)
    PYTHONIOENCODING=utf-8 python scripts/rule_lookup.py --smoke       # 실 커넥션 (타입·예외)
    PYTHONIOENCODING=utf-8 python scripts/rule_lookup.py --golden      # 정답셋 77문항 3분류
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db                                                   # noqa: E402
DSN = db.DSN

허용_강도 = {"불가": 3, "조건부": 2, "가능": 1}   # 클수록 엄격


# ══════════════════════════════════════════════════════════════════════════════
# 1. 정규화 — 조사·공백·괄호까지만. 여기서 의미를 만들지 않는다
# ══════════════════════════════════════════════════════════════════════════════

# 후치 조사. 한 글자 명사를 먹지 않도록 어간 2자 이상일 때만 뗀다.
_조사 = (
    "으로써", "으로서", "이라고", "라고", "으로", "에서", "에게", "한테", "부터",
    "까지", "보다", "라도", "이나", "처럼", "만큼", "조차", "마저", "이란", "란",
    "로써", "로서", "께서", "이며", "며", "이고", "고", "은", "는", "이", "가",
    "을", "를", "의", "에", "와", "과", "도", "만", "나", "로", "께",
)
_꼬리 = ("입니다", "이다", "인가요", "나요", "되나요", "됩니까", "해요", "요", "임", "함")

_구두점 = re.compile(r"[\s··・,，.\-–—~〜/／·:;'\"“”‘’!?()（）\[\]<>「」『』]+")
_괄호 = re.compile(r"[（(]([^）)]*)[）)]")


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", (s or "")).strip()


def _조사떼기(tok: str) -> str:
    """어간이 2자 이상 남을 때만, 고정점까지 반복해서 뗀다.

    반복이 필요한 이유 — '손잡이를' 은 1회로 '손잡이', '손잡이' 는 '손잡' 이 되어
    같은 말인데 정규형이 갈린다. 양쪽을 고정점까지 돌려야 대칭이 맞는다.
    """
    for _ in range(4):
        before = tok
        for k in _꼬리 + _조사:
            if tok.endswith(k) and len(tok) - len(k) >= 2:
                tok = tok[: -len(k)]
                break
        if tok == before:
            break
    return tok


def _norm(s: str) -> str:
    """비교용 정규형. 공백 제거 · 구두점 제거 · 조사 제거 · 소문자."""
    s = _nfkc(s).lower()
    toks = [t for t in _구두점.split(s) if t]
    return "".join(_조사떼기(t) for t in toks)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 금지예시 해부 — 핵(核)과 예외단서
# ══════════════════════════════════════════════════════════════════════════════

# 괄호 안이 "이 항목을 조건부로 되돌리는 단서" 인 경우.
# 🔴 실측 26종을 전수로 보고 정했다 (`_B_금지매칭_감사.json` 의 `괄호_분류`).
#   예외로 보는 것   : 예외 / 제외 / 집행 제한 / "…비로 집행|구매해야"(비목 재분류)
#   예외가 아닌 것   : "대행 수수료는 가능"  — 다른 대상을 가리킨다. 성공보수 자체는 불가
#                      "현물로만 계상 가능"  — 현물은 지출이 아니다(§2 오늘 확정). 지출로는 불가
#                      "…등" "전담조직" "선집행액 환수 대상" — 서술일 뿐이다
_예외단서 = re.compile(r"(예외|제외|집행\s*제한)")
_비목재분류 = re.compile(r"[가-힣]+비로\s*(집행|구매)")


def 금지예시_해부(예시: str) -> dict[str, Any]:
    """금지예시 한 줄 → {핵, 핵_정규형, 예외단서, 무조건}.

    괄호는 두 일을 한다. 하나는 부연('법인은 …로 등재된 법인'), 하나는 예외 단서
    ('사전승인 시 예외'). 후자가 붙은 항목은 **즉답 불가의 재료가 아니다** —
    정답셋 31번 '시제품 제작에 금 도금' 의 정답이 불가가 아니라 조건부인 이유가 이것이다.
    """
    원문 = _nfkc(예시)
    단서 = [m for m in _괄호.findall(원문)
            if _예외단서.search(m) or _비목재분류.search(m)]
    핵 = _괄호.sub(" ", 원문).strip()
    return {
        "핵": 핵,
        "핵_정규형": _norm(핵),
        "예외단서": 단서 or None,
        "무조건": not 단서,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. 룰 행 조회 — base 는 L1(사업명 NULL) ∪ L2(사업명 일치)
# ══════════════════════════════════════════════════════════════════════════════

_룰컬럼 = """rule_id, layer, 기관id, 사업명, 비목, 허용, 사전승인, 사전승인_조건,
             한도_유형, 한도_값, 한도_단위, 증빙, 금지예시, 허용예시, 근거,
             출처도메인, verified"""


def _행(cur, sql: str, args: Sequence[Any]) -> list[dict]:
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["한도_값"] = float(d["한도_값"]) if d.get("한도_값") is not None else None
        for k in ("증빙", "금지예시", "허용예시"):
            d[k] = list(d.get(k) or [])
        근거 = d.get("근거")
        d["근거"] = json.loads(근거) if isinstance(근거, str) else (근거 or [])
        out.append(d)
    return out


def 비목계통(cur, 사업명: str | None) -> str | None:
    """'창업' | 'RND' | **None**. 사업명이 없으면(정답셋 '공통' 27문항) 창업 계통으로 본다.

    🔴 2026-09-02 — **"준 사업명을 못 찾았다"와 "사업명을 안 줬다"는 다르다.**
    이전에는 못 찾아도 조용히 "창업"으로 떨어졌다. `corpus.programs` 표기와
    프론트·L3 등 다른 출처의 표기가 갈리면(예: TIPS 를 "민관공동 창업자 발굴·육성"
    으로 보내는 경우) 이 폴백이 **정확히 막으려던 시나리오를 스스로 만든다** —
    R&D 계통 TIPS 질문이 창업 계통 L1 지침으로 잘못 분류되고, 그 결과가
    "근거를 단 오답"으로 나간다(§base_룰 ②). CLAUDE.md 확정 원칙: 모든 실패의
    기본값은 판단불가다. "모르는 사업 = 창업 계통"은 그 원칙이 금지하는 "아마"다.
    → 사업명을 **줬는데** 없으면 `None`. 호출부(`base_룰`·`l3적용가능`)가 그 `None`
    을 판단불가로 닫는다. 사업명을 아예 안 준 경우(공통 문항)는 기존대로 "창업".
    """
    if not 사업명:
        return "창업"
    try:
        cur.execute("SELECT 비목계통 FROM corpus.programs WHERE 사업명 = %s", [사업명])
        r = cur.fetchone()
        if r and r[0]:
            return r[0]
    except Exception:                       # programs 가 아직 없는 워킹트리
        cur.connection.rollback()
        return "창업"                       # 표 자체가 없는 워킹트리 — 기존 동작 유지
    return None                             # 표는 있는데 이 사업명이 없다 — 판단불가로 닫는다


def base_룰(cur, 사업명: str | None, 비목: str | None = None) -> list[dict]:
    """🔴 `layer IN ('L1','L2') AND 사업명=?` 이 아니다. 두 군데가 다르다.

    ① **L1 은 `사업명 IS NULL` 이다.** G3 이후 L1 행이 그렇게 들어오는데
       `NULL = ?` 는 항상 false 라 옛 조건으로는 L1 을 **영영 못 집는다**.
       정답셋 '공통' 27문항(35%)이 전부 그 L1 행에만 걸려 있다.

    ② 🔴 **L1 은 창업 계통 사업에만 붙인다.** TIPS 는 위임 계통이 다르다 —
       상위가 「중소기업창업 지원사업 통합관리지침」이 아니라 「중소기업기술혁신
       촉진법」·「국가연구개발사업 연구개발비 사용 기준」이다. 계통 조건이 없으면
       지침 제41조("대표자 인건비 불가")가 TIPS 연구수당 질문에 **근거를 달고**
       발화한다. 근거 없는 오답보다 근거 붙은 오답이 나쁘다 — 사용자가 검증할
       방법이 없기 때문이다. TIPS 는 rules 0행 → None → 판단불가가 맞다.

    ③ 🔴 **사업명을 줬는데 `corpus.programs` 에 없으면 조회 자체를 접는다.**
       `비목계통()` 이 `None` 을 돌려주는 경우다(2026-09-02). 그때 "창업"으로
       가정하고 계속 조회하면 ②가 막으려던 오분류가 그대로 재발한다 — 그래서
       여기서 `[]` 로 끊는다. `effective_rule()` 은 base 도 l3 도 없으면 판단불가로
       닫으므로(§effective_rule), 이건 "룰이 없다"가 아니라 "이 사업명을 모른다"를
       올바르게 판단불가로 접는 것이다. 사업명을 아예 안 준 경우(공통 문항)는
       `비목계통(None)`이 그대로 "창업"이라 이 분기를 안 탄다.

    (①②는 2026-08-31 A 세션과 합의. ②는 G 가 조문으로 잡은 건. ③은 W3 가 2026-09-02
     TIPS 프론트 표기 불일치 조사에서 잡았고 ai-ae 승인)
    """
    계통 = 비목계통(cur, 사업명)
    if 사업명 is not None and 계통 is None:
        return []                            # 모르는 사업명 — 조회 없이 판단불가로 접는다
    창업계통 = 계통 == "창업"
    where = ["((layer='L1' AND 사업명 IS NULL AND %s) OR (layer='L2' AND 사업명 = %s))"]
    args: list[Any] = [창업계통, 사업명]
    if 비목:
        where.append("비목 = %s")
        args.append(비목)
    return _행(cur, f"SELECT {_룰컬럼} FROM corpus.rules WHERE " + " AND ".join(where)
                    + " ORDER BY layer, rule_id", args)


def _l3정규화(l3: dict | None) -> dict | None:
    """E 의 `l3_load.l3룰()` 산출물을 병합이 먹을 수 있는 행 모양으로 맞춘다.

    🔴 **L3 룰은 `corpus.rules` 에 없다.** `tenant.l3_articles` 에서 조립돼 오므로
    `rule_id` 가 없고 일부 키도 빠진다. 없는 키를 인덱싱하면 병합이 죽는데, L3 는
    판정 경로에서 늘 도는 자리라 그대로 두면 기관 사용자 전원이 깨진다.
    인용 식별자는 `rule_id` 가 아니라 **`article_id`** 다 (A 의 인용 검증이 층별로 가른다).
    """
    if l3 is None:
        return None
    근거 = list(l3.get("근거") or [])
    article_id = next((g.get("article_id") for g in 근거 if g.get("article_id")), None)
    r = {
        "rule_id": l3.get("rule_id"),
        "article_id": article_id,
        "layer": "L3",
        "기관id": None,                  # 🔴 프롬프트로 새면 TENANT_LEAK 이다. 여기서 끊는다
        "사업명": None,
        "비목": l3.get("비목"),
        "허용": l3.get("허용"),
        "참조만": l3.get("참조만"),
        "사전승인": bool(l3.get("사전승인")),
        "사전승인_조건": l3.get("사전승인_조건"),
        "한도_유형": l3.get("한도_유형"),
        "한도_값": float(l3["한도_값"]) if l3.get("한도_값") is not None else None,
        "한도_단위": l3.get("한도_단위"),
        "증빙": list(l3.get("증빙") or []),
        "금지예시": list(l3.get("금지예시") or []),
        "허용예시": list(l3.get("허용예시") or []),
        "근거": 근거,
        "verified": bool(l3.get("verified")),
        "seed_refs": list(l3.get("seed_refs") or []),
        "dangling": list(l3.get("dangling") or []),
    }
    return r


def l3적용가능(cur, 사업명: str | None) -> bool:
    """이 사업에 **우리 L3 룰**을 붙여도 되는가. 🔴 비목 축이 맞아야 한다.

    L1 통과 조건(`base_룰` ②)와 **이유가 다르다.** L1 은 위임 계통 문제였다 —
    통합관리지침이 TIPS 를 규율하지 않는다. L3 는 그렇지 않다: 주관기관 규정이
    자기 TIPS 과제를 규율하는 건 현실에서 참이다.

    막아야 하는 건 **비목 축**이다. 우리 L3 룰은 `l3_load` 가 `corpus.item_vocab`
    (계통='창업')으로 키를 걸어 조립한다. TIPS 비목 체계는 R&D 계통(연구활동비·
    연구수당·학생인건비·간접비…)이라, 이름이 겹치는 '인건비' 매칭은 **의미 일치가
    아니라 문자열 충돌**이다 — TIPS 는 연구수당이 별도 비목이라 뜻이 다르다.

    실제로 붙고 있었다 (2026-09-01 G 세션, TIPS × org 2 × 비목 10 전수):
        TIPS 인건비  / 대전과기원  → 허용=가능 · 적용층=L3
        B4문장: "인건비 룰상 집행 가능한 항목이다. 적용된 규범은 주관기관 내부규정이다."
    l3_게이팅(4-L3가능 → need_upper=True)과 `verified=False` 가 최종 판정은 막고
    있었지만, **그 문장이 ④ 조립 프롬프트에 실린다.** 근거 붙은 오답이 더 나쁘다.

    계통이 늘거나 L3 가 R&D 비목으로 키를 걸게 되면 그때 이 함수만 고치면 된다.

    🔴 2026-09-02 — 사업명을 줬는데 `corpus.programs` 에 없으면(`비목계통()`이
    `None`) **여기도 `False` 다.** 모르는 사업명에 L3 를 붙이지 않는다 — §base_룰 ③
    과 같은 이유다.
    """
    계통 = 비목계통(cur, 사업명)
    if 사업명 is not None and 계통 is None:
        return False
    return 계통 == "창업"


def _org문자열(기관ID: Any) -> str | None:
    """🔴 `corpus.rules.기관id` 는 **text** 인데 `tenant.orgs.org_id` 는 **uuid** 다.

    UUID 객체를 그대로 바인딩하면 Postgres 가 `operator does not exist: text = uuid`
    로 죽는다. 드문 엣지가 아니다 — A 는 org_id 를 `tenant.orgs` 에서 그대로 받아
    넘기고, 이 경로는 **그 기관 L3 에 해당 비목이 없을 때마다** 탄다(기관당 조문
    6~11개 vs 비목 10종이면 대부분이 여기다). 문자열로 맞춰서 넘긴다.
    (2026-09-01 G 세션 발견. 타입을 맞추는 근본 수정은 DDL 이라 D 소관 — 대장행)
    """
    if 기관ID is None or 기관ID == "":
        return None
    return str(기관ID)


def l3_룰_행(cur, 기관ID: Any, 비목: str | None, 사업명: str | None = None) -> dict | None:
    """L3 룰 한 행. **`l3_load.l3룰()` 이 기준 문서이고 `corpus.rules` 는 대체 경로이다.**

    A 는 `effective_rule(..., l3룰=l3_load.l3룰(...))` 로 주입하는 게 정상 경로다
    (E 가 `tenant.l3_articles` 에서 조립한다). 주입이 없을 때만 여기가 돈다.

    ⚠️ **설계가 두 갈래로 공존한다** — CLAUDE.md 는 "L3 는 `tenant.l3_articles`" 라 하고
    `rule_base.md` §3-1 은 overlay 를 `rules WHERE layer='L3' AND 기관=?` 로 뽑는다고
    한다. 실질 기준 문서는 전자다(`corpus.rules` 의 `layer='L3'` 는 **0행**이고
    `seed_rules.py` 가 L3 행을 아예 만들지 않는다). 어느 쪽으로 정리할지는 목록에 올렸고,
    그때까지 후자를 지운 게 아니라 **타입만 맞춰 두었다.**
    """
    org = _org문자열(기관ID)
    if not org or not 비목:
        return None
    if 사업명 is not None and not l3적용가능(cur, 사업명):
        return None                         # 비목 축이 안 맞는 사업(TIPS) — §l3적용가능
    try:
        import l3_load
    except ImportError:                     # E 모듈이 아직 없는 워킹트리
        l3_load = None
    if l3_load is not None:
        # 🔴 여기서 예외를 삼키지 않는다. `l3_load.l3룰()` 은 RLS org 컨텍스트를 세우는데,
        #    그게 실패한 걸 조용히 넘기면 **남의 기관 규정으로 판정**할 수 있다.
        #    실패는 A 의 실패 경로(§8)가 받아 판단불가로 닫는 게 맞다.
        r = _l3정규화(l3_load.l3룰(cur, org, 비목))
        if r:
            return r
    rows = _행(cur, f"SELECT {_룰컬럼} FROM corpus.rules "
                    "WHERE layer='L3' AND 기관id = %s::text AND 비목 = %s ORDER BY rule_id",
               [org, 비목])
    return _l3정규화(rows[0]) if rows else None


def _precedence(cur, 사업명: str | None) -> list[dict]:
    if not 사업명:
        return []
    cur.execute("SELECT 사업명, 우선계층, 열위계층, 범위, 우선규범, 근거, 원문, 해석, verified "
                "FROM corpus.precedence_rules WHERE 사업명 = %s", [사업명])
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        근거 = d.get("근거")
        d["근거"] = json.loads(근거) if isinstance(근거, str) else (근거 or [])
        out.append(d)
    return out


def _우선규범(cur, 사업명: str | None) -> str | None:
    """B6 — 초격차·모두의창업의 상위는 통합관리지침이 **아니다**.

    `corpus.programs.우선규범` 이 기준 문서(D 적재). 없으면 precedence_rules 로 대체한다.
    """
    if not 사업명:
        return None
    try:
        cur.execute("SELECT 우선규범 FROM corpus.programs WHERE 사업명 = %s", [사업명])
        r = cur.fetchone()
        if r and r[0]:
            return r[0]
    except Exception:                       # programs 가 아직 없는 워킹트리
        cur.connection.rollback()
    for p in _precedence(cur, 사업명):
        if p.get("우선규범"):
            return p["우선규범"]
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4. 동결 인터페이스 ① 금지적중 — 통과 조건 A (B1)
# ══════════════════════════════════════════════════════════════════════════════

_최소핵길이 = 4          # 정규형 기준. '기프티콘'(5) 은 통과, 2~3자 조각은 막는다


def 금지후보(cur, 품목: str | None, 용도: str | None, 사업명: str | None,
             비목: str | None) -> dict[str, list[dict]]:
    """진단용 — 즉답 대상(`무조건`)과 예외단서가 달린 근접 항목(`조건부`)을 함께 돌려준다.

    `금지적중()` 은 `무조건` 만 쓴다. `조건부` 는 감사·보고용이다.
    """
    본문 = _norm(f"{품목 or ''} {용도 or ''}")
    품목n = _norm(품목 or "")
    결과: dict[str, list[dict]] = {"무조건": [], "조건부": []}
    if not 본문:
        return 결과

    for r in base_룰(cur, 사업명, 비목):
        for 예시 in r["금지예시"]:
            h = 금지예시_해부(예시)
            핵n = h["핵_정규형"]
            if len(핵n) < _최소핵길이:
                continue
            if 품목n and 품목n == 핵n:
                방식 = "정확일치"
            elif 핵n in 본문:
                방식 = "구절포함"
            else:
                continue
            항목 = {
                "예시": h["핵"],
                "예시_원문": _nfkc(예시),
                "근거": r["근거"],
                "rule_id": r["rule_id"],
                "비목": r["비목"],
                "사업명": r["사업명"],
                "layer": r["layer"],
                "verified": r["verified"],
                "매칭": 방식,
                "매칭문자열": 핵n,
                "예외단서": h["예외단서"],
            }
            결과["무조건" if h["무조건"] else "조건부"].append(항목)

    for k in 결과:
        결과[k].sort(key=lambda x: (x["매칭"] != "정확일치", -len(x["매칭문자열"])))
    return 결과


def 금지적중(cur, 품목: str | None, 용도: str | None, 사업명: str | None,
             비목: str | None) -> dict | None:
    """통과 조건 A — 적중하면 LLM 0회로 "불가" 를 즉답할 수 있다.

    정확일치(정규화 후 품목 == 금지예시 핵) 또는 구절포함(금지예시 핵이 품목+용도
    안에 문자 그대로 들어 있음)만 인정한다. 유사매칭은 하지 않는다 — §B1.

    예외단서가 붙은 금지예시('귀금속·보석·원석(사전승인 시 예외)')는 **적중으로 치지
    않는다.** 그건 조건부이지 즉답 불가가 아니다. `effective_rule()` 의 정상 경로로 간다.

    반환: {"예시","근거","rule_id", ...부가키} | None
    """
    후보 = 금지후보(cur, 품목, 용도, 사업명, 비목)
    return 후보["무조건"][0] if 후보["무조건"] else None


# ══════════════════════════════════════════════════════════════════════════════
# 5. 동결 인터페이스 ② 비목확정 (B3·B4)
# ══════════════════════════════════════════════════════════════════════════════

_모델 = None
_MODEL_NAME = "nlpai-lab/KURE-v1"


def warmup() -> None:
    """CPU KURE-v1 상주화. A 의 오케 기동 시 한 번 부르면 첫 판정이 안 튄다.

    🔴 GPU 를 열지 않는다 — 별칭 200쌍 규모는 CPU 로 수초다 (§5).
    """
    global _모델
    if _모델 is None:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(_MODEL_NAME, device="cpu")
        m.max_seq_length = 128            # 상품명은 짧다. 1024 로 두면 헛돈다
        _모델 = m
        _모델.encode(["워밍업"], normalize_embeddings=True)


def 임베딩(texts: Sequence[str]):
    warmup()
    return _모델.encode(list(texts), normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=False)


def _vecstr(v) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


def 비목확정(cur, 품목: str | None, 사업명: str | None, *,
             임계치: float = 0.75, top_n: int = 3,
             벡터허용: bool = True) -> list[dict]:
    """[{"비목","신뢰도","출처":"alias|vector"}] — 신뢰도 내림차순.

    정확조회(`item_alias`)를 먼저 본다. 사업 전용 별칭이 공통 별칭을 이긴다.
    실패하면 벡터로 넘어간다. 임계치는 인자다 — 통과 조건 C(비목 갈림)의 폭을
    A 가 조절할 수 있어야 한다.

    🔴 **임계치 0.75 는 실측으로 정했다** (`--calibrate`, 별칭 228쌍 leave-one-out:
    자기 자신을 뺀 최근접 이웃의 비목이 맞는 비율).

        임계 0.62  발화 86.8%  비목일치 87.4%      ← 처음 잡았던 값
        임계 0.70  발화 60.5%  비목일치 94.2%
        임계 0.75  발화 45.6%  비목일치 97.1%      ← 채택
        임계 0.85  발화 13.2%  비목일치 100.0%

    0.75 아래의 오분류는 의미가 아니라 **자소 유사**다 — 텀블러→태블릿(0.658),
    임금→임차료(0.645), 외장하드→외주(0.644), 센서→캠코더(0.627). KURE-v1 이
    2~4자 한국어 명사에서 표면형에 끌린다. 틀린 비목은 없는 비목보다 나쁘다:
    `effective_rule()` 이 엉뚱한 룰 행을 집고 엉뚱한 조문을 인용한다.
    """
    품목n = _norm(품목 or "")
    if not 품목n:
        return []
    out: list[dict] = []
    본 = set()

    # ── 정확조회 ────────────────────────────────────────────────────────────
    cur.execute("SELECT 상품명, 비목, 사업명, 출처 FROM corpus.item_alias "
                "WHERE 사업명 = %s OR 사업명 IS NULL", [사업명])
    for 상품명, 비목, 별칭사업, 출처 in cur.fetchall():
        if _norm(상품명) != 품목n:
            continue
        신뢰도 = 1.0 if 별칭사업 else 0.95
        if 비목 not in 본 or 신뢰도 > next(o["신뢰도"] for o in out if o["비목"] == 비목):
            out = [o for o in out if o["비목"] != 비목]
            out.append({"비목": 비목, "신뢰도": 신뢰도, "출처": "alias",
                        "매칭": 상품명, "별칭사업명": 별칭사업, "별칭출처": 출처})
            본.add(비목)

    # ── 비목 정본명·별칭 자체와의 일치 (용어 사전) ──────────────────────────────
    if not out:
        for 비목, 신뢰도 in _용어사전_직결(cur, 품목n):
            if 비목 not in 본:
                out.append({"비목": 비목, "신뢰도": 신뢰도, "출처": "alias",
                            "매칭": 품목, "별칭사업명": None, "별칭출처": "item_vocab"})
                본.add(비목)

    if out or not 벡터허용:
        return sorted(out, key=lambda o: -o["신뢰도"])[:top_n]

    # ── 벡터 ────────────────────────────────────────────────────────────────
    cur.execute("SELECT count(*) FROM corpus.item_alias WHERE embedding IS NOT NULL")
    if not cur.fetchone()[0]:
        return []
    q = _vecstr(임베딩([품목])[0])
    cur.execute(
        "SELECT 비목, 상품명, 1 - (embedding <=> %s::vector) AS sim FROM corpus.item_alias "
        "WHERE embedding IS NOT NULL AND (사업명 = %s OR 사업명 IS NULL) "
        "ORDER BY embedding <=> %s::vector LIMIT 30", [q, 사업명, q])
    for 비목, 상품명, sim in cur.fetchall():
        sim = float(sim)
        if sim < 임계치 or 비목 in 본:
            continue
        out.append({"비목": 비목, "신뢰도": round(sim, 4), "출처": "vector",
                    "매칭": 상품명, "별칭사업명": None, "별칭출처": None})
        본.add(비목)
    return sorted(out, key=lambda o: -o["신뢰도"])[:top_n]


def _용어사전_직결(cur, 품목n: str) -> list[tuple[str, float]]:
    """품목이 비목 정본명·별칭·하위항목 그 자체일 때 (사용자가 비목명을 그대로 쓴 경우)."""
    try:
        cur.execute("SELECT 비목, 별칭, 하위항목 FROM corpus.item_vocab WHERE 계통='창업'")
    except Exception:
        cur.connection.rollback()
        return []
    hits = []
    for 비목, 별칭, 하위 in cur.fetchall():
        if _norm(비목) == 품목n:
            hits.append((비목, 1.0))
        elif any(_norm(a) == 품목n for a in (별칭 or [])):
            hits.append((비목, 0.98))
        elif any(_norm(a) == 품목n for a in (하위 or [])):
            hits.append((비목, 0.96))
    return hits


# ══════════════════════════════════════════════════════════════════════════════
# 6. 금액 비교 (B7) 와 B4 문장 (B8)
# ══════════════════════════════════════════════════════════════════════════════

_한정 = re.compile(r"([가-힣A-Za-z·]+)\s*한정")


def _괄호밖(s: str) -> str:
    """괄호 **밖** 문자열만. 중첩 괄호를 깊이로 세어 통째로 걷어낸다."""
    out, 깊이 = [], 0
    for ch in s:
        if ch in "(（":
            깊이 += 1
        elif ch in ")）":
            깊이 = max(0, 깊이 - 1)
        elif 깊이 == 0:
            out.append(ch)
    return "".join(out).strip()


def _괄호안(s: str) -> str:
    """괄호 **안** 문자열 전부 (중첩 포함). `_괄호밖` 의 여집합."""
    out, 깊이 = [], 0
    for ch in s:
        if ch in "(（":
            깊이 += 1
            continue
        if ch in ")）":
            깊이 = max(0, 깊이 - 1)
            out.append(" ")
            continue
        if 깊이 > 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _단위해부(단위: str | None) -> dict[str, Any]:
    """'원/인/일(멘토링비 한정, 시간당 …)' → {기본:'원', 분모:['인','일'], 한정:'멘토링비'}."""
    if not 단위:
        return {"기본": None, "분모": [], "한정": None, "부연": None}
    원문 = _nfkc(단위)
    부연 = _괄호안(원문) or None
    # 🔴 괄호가 중첩된다 — 재도전 지급수수료가 실제로 이렇다:
    #   "원/인/일(멘토링비 한정 … (예비)재창업자별 … 총액 500만원(사업기간 중 합산) …)"
    #   `_괄호`(비중첩)로 지우면 안쪽 `(예비)` 에서 끊겨 뒤가 산문째 남고, 그게 `/` 분해에
    #   섞여 분모가 "일재창업자별 멘토링비 총액 500만원 …" 이 된다. 깊이를 세어 지운다.
    핵 = _괄호밖(원문)
    조각 = [p.strip() for p in 핵.split("/") if p.strip()]
    m = _한정.search(부연 or "")
    return {"기본": 조각[0] if 조각 else None, "분모": 조각[1:],
            "한정": m.group(1) if m else None, "부연": 부연}


_분모키 = {"인": "인원", "일": "일수", "월": "월수", "박": "박수", "건": "건수", "대": "인원"}

# 🔴 한 행의 `한도_값` 은 스칼라 하나인데, 실제 조문은 한 비목에 한도를 여럿 건다.
#    나머지가 `한도_단위` 안에 **산문으로** 들어 있어 구조화 비교에 안 잡힌다 (실측 5행):
#      재도전 지급수수료  30만/인/일  + "별도 한도 2건 — 총액 500만원 · 세무회계 월 20만원"
#      모두의창업 여비    10만/인/박  + "광역시 8만원, 그 밖의 지역 7만원"   ← 더 낮다
#      예비·초기·모두 지급수수료      + "시간당 10만원 초과 불가"
#    이걸 모르고 "한도 이내" 라고 답하면 **더 엄한 한도를 놓친 «틀린 가능»** 이 된다.
#    그래서 미파싱 신호가 있으면 `초과=False` 를 `None` 으로 낮춘다. `True` 는 그대로 둔다
#    (이미 넘었다는 결론은 다른 한도가 있어도 안 뒤집힌다).
#    🔴 근본 해결은 한도를 행으로 쪼개는 것이다 — `rules`(G)·DDL(D) 소관이라 오늘 안 건드렸다.
_분모어 = re.compile(r"(계약총액|전체\s*용역비|선급금|용역비)")
_추가한도 = re.compile(r"(초과\s*불가|별도\s*한도|광역시|그\s*밖의\s*지역|총액\s*[0-9]|"
                       r"[0-9][0-9만천,]*\s*원\s*(이내|한도|까지))")
# "12만5천원 초과 시 기타소득세 공제" 는 한도가 아니라 원천징수 규칙이다 — 신호가 아니다
_세금 = re.compile(r"초과\s*시.{0,20}(공제|소득세)")


_신호말 = [("초과 불가", "별도의 상한"), ("별도 한도", "별도의 한도"),
           ("광역시", "지역별로 다른 한도"), ("그 밖의 지역", "지역별로 다른 한도"),
           ("총액", "총액 한도"), ("이내", "별도의 상한"), ("한도", "별도의 한도"),
           ("까지", "별도의 상한")]


def 미파싱한도(단위: str | None) -> list[str]:
    """`한도_단위` 산문 안에 숨은 **추가 한도**의 신호. 없으면 빈 리스트.

    반환값은 그대로 사용자에게 보이는 문장(`B4문장`)에 들어가므로 정규식이 집은
    조각("총액 5")이 아니라 읽을 수 있는 말("총액 한도")로 바꿔서 돌려준다.
    """
    if not 단위:
        return []
    본문 = _분모어.sub(" ", _세금.sub(" ", _nfkc(단위)))
    raw = [m[0] if isinstance(m, tuple) else m for m in _추가한도.findall(본문)]
    if not raw:
        return []
    말: list[str] = []
    for 조각 in raw:
        납작 = re.sub(r"\s+", "", 조각)
        for 키, 라벨 in _신호말:
            if re.sub(r"\s+", "", 키) in 납작 and 라벨 not in 말:
                말.append(라벨)
                break
    return 말 or ["별도의 한도"]


def 금액비교(rule: dict, 수치: dict | None, 하위항목: str | None = None) -> dict | None:
    """{"초과": bool|None, "사유": str} | None.

    비율은 F1 협약총액(또는 계약총액)이 있어야 잰다 — 없으면 `초과=None` 으로 넘겨
    A 가 전제로 만든다(§4 · Agent.md §4-b). **추측으로 채우지 않는다.**
    """
    유형 = rule.get("한도_유형")
    if not 유형 or rule.get("한도_값") is None:
        return None
    수치 = dict(수치 or {})
    한도 = float(rule["한도_값"])
    u = _단위해부(rule.get("한도_단위"))

    # 하위항목 한정 — '멘토링비 한정' 한도를 사무실임차료에 걸면 틀린 초과가 된다
    if u["한정"]:
        if 하위항목 is None:
            return {"초과": None,
                    "사유": f"이 한도는 {u['한정']} 에만 걸리는데 하위항목이 확정되지 않았다"}
        if _norm(하위항목) != _norm(u["한정"]):
            return None

    if 유형 == "비율":
        분자 = 수치.get("선급금") or 수치.get("금액")
        분모 = 수치.get("계약총액") or 수치.get("협약총액")
        if 분자 is None or 분모 in (None, 0):
            부족 = "계약총액" if 분자 is not None else "선급금과 계약총액"
            return {"초과": None, "사유": f"비율 비교에 필요한 {부족} 이 입력되지 않았다"}
        비율 = float(분자) / float(분모) * 100
        return {"초과": 비율 > 한도, "사유": f"선급 비율이 한도 비율을 "
                + ("초과한다" if 비율 > 한도 else "넘지 않는다")}

    if 유형 == "개수":
        수량 = 수치.get("수량")
        if 수량 is None:
            return {"초과": None, "사유": "수량이 입력되지 않았다"}
        인원 = 수치.get("인원")
        if "인" in u["분모"] and 인원 is None:
            return {"초과": None, "사유": "1인당 한도인데 인원이 입력되지 않았다"}
        허용수 = 한도 * float(인원 if 인원 is not None else 1)
        return {"초과": float(수량) > 허용수,
                "사유": ("허용 대수를 초과한다" if float(수량) > 허용수 else "허용 대수 이내다")}

    if 유형 == "금액":
        금액 = 수치.get("금액")
        if 금액 is None:
            return {"초과": None, "사유": "금액이 입력되지 않았다"}
        분모값 = 1.0
        모자란 = []
        for d in u["분모"]:
            k = _분모키.get(d)
            v = 수치.get(k) if k else None
            if v is None:
                모자란.append(k or d)
            else:
                분모값 *= float(v)
        if 모자란:
            return {"초과": None,
                    "사유": f"단가 환산에 필요한 {'·'.join(모자란)} 이(가) 입력되지 않았다"}
        단가 = float(금액) / (분모값 or 1.0)
        return {"초과": 단가 > 한도,
                "사유": ("단가가 한도를 초과한다" if 단가 > 한도 else "단가가 한도 이내다")}

    return {"초과": None, "사유": f"알 수 없는 한도 유형 '{유형}'"}


def _한도전수(한도목록: Sequence[dict], 수치: dict | None,
              하위항목: str | None = None) -> dict | None:
    """한도가 여럿이면 **전부** 재고 하나로 접는다. 하나라도 넘으면 초과다.

    단위가 다른 한도는 서로 다른 제약이라 min 으로 접을 수 없다 — 「1인 1대」와
    「월 50만원」은 둘 다 지켜야 한다. 하나라도 초과면 초과, 하나도 초과가 아닌데
    못 잰 게 있으면 `초과=None`(전제로 넘긴다), 전부 이내여야 이내다.
    """
    결과 = [x for x in (금액비교(r, 수치, 하위항목) for r in 한도목록) if x is not None]
    if not 결과:
        return None
    넘음 = [x for x in 결과 if x["초과"] is True]
    if 넘음:
        # 이미 넘었다는 결론은 다른 한도가 더 있어도 안 뒤집힌다
        return {"초과": True, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 넘음))}
    미상 = [x for x in 결과 if x["초과"] is None]
    if 미상:
        return {"초과": None, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 미상))}

    # 🔴 "한도 이내" 라고 말하기 전에, 산문에 숨은 추가 한도가 없는지 본다.
    #    있으면 이내라고 단정하지 않는다 — 더 엄한 한도를 놓친 «틀린 가능» 이 된다.
    숨은 = sorted({s for r in 한도목록 for s in 미파싱한도(r.get("한도_단위"))})
    if 숨은:
        return {"초과": None,
                "사유": "구조화된 한도는 넘지 않으나, 같은 조문에 코드가 못 읽는 추가 한도가 "
                        f"있다({'·'.join(숨은)}) — 이내라고 단정할 수 없다"}
    return {"초과": False, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 결과))}


# 🔴 2026-09-06(레인 K) — 토큰 예산 방어. ai-8c 실측(ai-d9 원 실측): 이미 최악결합
# 프롬프트가 42,856토큰(104.6%, 한도 40,960)이다. 배관을 열어 예시를 실으면
# (실측 80조합 중 최댓값 21+13개, B4문장 1,260자) 문항에 따라 한도를 밀 수 있다.
# 항목 «개수» 를 자른다 — 어느 걸 자를지(중요도 순)는 룰 데이터 판단이라 이 레인
# 밖이다(코드만 본다). 그래서 그냥 «원래 순서 그대로 앞에서부터» 자른다.
_예시_최대개수 = 8


def _예시_문장(라벨: str, 목록: Sequence[str] | None, 최대: int = _예시_최대개수) -> str | None:
    """"{라벨}: 항목1 · 항목2 · … 외 N종." 최대 개수를 넘으면 자르고 남은 개수를 적는다.

    🔴 자르는 기준(순서)에 아무 의미를 담지 않는다 — DB 가 준 순서 그대로 앞부터
    쓴다. "중요한 걸 남긴다" 는 판단은 룰 데이터를 읽어야 하는 일이라 이 함수의
    권한 밖이다.
    """
    if not 목록:
        return None
    보임 = list(목록[:최대])
    문장 = f"{라벨}: " + " · ".join(보임)
    남음 = len(목록) - len(보임)
    if 남음 > 0:
        문장 += f" 외 {남음}종"
    return 문장 + "."


def _실체없음(금지예시: Sequence[str] | None, 허용예시: Sequence[str] | None,
             비교: dict | None, 사전승인: bool, 증빙: Sequence[str]) -> bool:
    """"이 비목의 룰은 있으나 «이 지출을 구체적으로 다루는» 내용이 없다" 를 가르는 조건.

    2026-09-06(레인 K) — B4 3단계의 세 번째 상태. `_참조만()`(1077행 부근)과 같은
    발상이다 — "형식만 있고 실체가 없는 행" 을 이미 그 함수가 개별 행 단위로
    가리고 있었다. 여기서는 **병합이 끝난 뒤**(L1+L2+L3 다 합친 뒤) 같은 잣대를
    다시 댄다 — 병합 전엔 실체가 없던 두 층이 합쳐지며 실체가 «생길» 수 있어서다.

    🔴 **품목 단위 매칭이 아니다.** "이 지출이 금지예시 문구와 글자로 겹치는가"
    를 묻지 않는다 — 그건 `금지적중()`(통과 조건 A, 즉답 불가 경로)의 몫이고,
    그 문서 자체가 "유사매칭을 넣지 않았다" 고 못박은 자리다(왜 유사매칭을 넣지
    않았나, 16행). 여기서 다시 그 매칭을 쓰면 자기재현 264/264(100%) vs 골든셋
    0/77(0%) 이라는 같은 함정을 3단계 판정에도 옮겨 심는 것이다 — 실측이 이미
    "안 붙는다" 고 말한 매칭을 "구체 규정이 있다/없다" 를 가르는 데 다시 쓰면,
    거의 «항상» 3번째 상태로 떨어져 지금보다 더 나빠질 수 있다.

    대신 **이 비목의 병합 결과 자체에 구체적인 재료가 하나라도 있는가**(금지예시·
    허용예시·비교 가능한 한도·사전승인·증빙)만 본다 — 매칭이 아니라 존재 여부다.
    """
    return not (금지예시 or 허용예시 or (비교 is not None) or 사전승인 or 증빙)


def B4문장(비목: str | None, 허용: str | None, 사전승인: bool, 증빙: Sequence[str],
           비교: dict | None, 적용층: str | None, 우선규범: str | None,
           참고_L3: dict | None = None, *,
           금지예시: Sequence[str] | None = None,
           허용예시: Sequence[str] | None = None,
           사전승인_조건: str | None = None) -> str:
    """🔴 **원시 한도값을 쓰지 않는다.** 비교가 끝난 문장만 만든다.

    "한도 30만원" 을 그대로 흘리면 사용자가 자기 사례에 잘못 적용한다(1인·1일 기준을
    떼고 총액으로 읽는다). 비교는 코드가 끝내고, 문장은 그 결과만 말한다.

    2026-09-06(레인 K) — «2단계» 였던 걸 «3단계» 로 늘린다.
        없다                          -> "명시되어 있지 않다" (그대로, 아래 else)
        있고 이 비목을 구체적으로 다룬다  -> 그 내용(금지예시·허용예시)을 싣는다
        🔴 있는데 구체 내용이 없다        -> "조건을 붙여 허용한다" 대신 그 사실을 말한다
    세 번째가 없으면 룰 행 하나 있다는 이유만으로 "조건을 붙여 허용한다" 같은 확정
    문장이 나가는데, 정작 무슨 조건인지는 어디에도 없다 — 그 자체로 거짓에 가깝다.
    """
    비목 = 비목 or "해당 비목"
    조각: list[str] = []
    실체없음 = 허용 in 허용_강도 and _실체없음(금지예시, 허용예시, 비교, 사전승인, 증빙)
    if 실체없음:
        조각.append(f"{비목} 룰은 있으나 이 지출을 구체적으로 다루는 금지예시·허용예시·"
                    "한도가 없다. 아래 검색 근거로 판단하라.")
    elif 허용 == "불가":
        조각.append(f"{비목} 룰은 이 집행을 허용하지 않는다.")
    elif 허용 == "가능":
        조각.append(f"{비목} 룰상 집행 가능한 항목이다.")
    elif 허용 == "조건부":
        조각.append(f"{비목} 룰은 조건을 붙여 허용한다.")
    else:
        조각.append(f"{비목} 의 허용 여부가 룰에 명시되어 있지 않다.")

    if 비교 is not None:
        if 비교["초과"] is True:
            조각.append(f"한도 비교 결과 한도를 넘는다({비교['사유']}).")
        elif 비교["초과"] is False:
            조각.append(f"한도 비교 결과 한도 이내다({비교['사유']}).")
        else:
            조각.append(f"한도 비교는 아직 못 한다 — {비교['사유']}.")

    if 사전승인:
        조각.append("집행 전 사전승인이 필요하다."
                    + (f" ({사전승인_조건})" if 사전승인_조건 else ""))
    if 증빙:
        조각.append(f"증빙 {len(증빙)}종을 갖춰야 한다.")
    # 🔴 2026-09-06(레인 K) — 「배관을 연다」: 지금까지 금지예시·허용예시는 eff/반환
    #    dict 에 있어도 B4문장엔 «한 번도» 안 실렸다(카운트조차 없었다). 여기서 실제
    #    내용을 싣는다 — «몇 종» 이 아니라 «무엇» 인지 알아야 LLM 이 구체적으로 쓴다.
    #    단, 토큰 예산 때문에 개수는 `_예시_문장()` 이 자른다(위 주석 참조).
    금지문 = _예시_문장("금지 예시", 금지예시)
    if 금지문:
        조각.append(금지문)
    허용문 = _예시_문장("허용 예시", 허용예시)
    if 허용문:
        조각.append(허용문)
    if 적용층:
        층말 = {"L1": "국가 통합관리지침", "L2": "사업 세부관리기준",
                "L3": "주관기관 내부규정",
                # 위임 관계라 두 층이 같이 구속하는 게 정상이다 — 어느 하나로 못 줄인다
                "L1+L2": "국가 통합관리지침과 사업 세부관리기준",
                "L1+L3": "국가 통합관리지침과 주관기관 내부규정",
                "L2+L3": "사업 세부관리기준과 주관기관 내부규정",
                "L1+L2+L3": "국가 통합관리지침·사업 세부관리기준·주관기관 내부규정"}.get(적용층, 적용층)
        조각.append(f"적용된 규범은 {층말}이다.")
    if 우선규범:
        조각.append(f"이 사업의 상위 규범은 「{우선규범}」이다.")
    if 참고_L3:
        조각.append("귀 기관 규정은 더 엄격하나, 본 사업 관리기준이 우선한다."
                    if 참고_L3.get("L3더엄격")
                    else "귀 기관 규정에도 관련 조항이 있으나, 본 사업 관리기준이 우선한다.")
    return " ".join(조각)


# ══════════════════════════════════════════════════════════════════════════════
# 7. 동결 인터페이스 ③ effective_rule — 효력 결정 (B5·B6)
# ══════════════════════════════════════════════════════════════════════════════

def _엄격병합(rows: Sequence[dict], *, 허용층우선: str | None = None) -> dict:
    """필드별로 병합한다. **"이긴 층으로 통째 갈아끼우기" 가 틀린 모델이다.**

    🔴 L1 과 L2 는 충돌 관계가 아니라 **위임 관계**다. 지침 제36조②가 "제1항에서
    명시되지 않은 증빙서류는 …사업별 세부관리기준에 따라" 라고 직접 위임한다 —
    L2 는 L1 을 뒤집는 게 아니라 구체화한다. 그래서 필드마다 따로 고른다.

        허용        엄격한 쪽 (불가 > 조건부 > 가능)
                    단, `허용층우선` 이 주어지면 그 층의 값 — unspecified_only 전용
        한도        🔴 NULL 이 이기면 절대 안 된다. 같은 단위끼리 min,
                    단위가 다르면 **둘 다 남긴다** (`한도목록`)
        사전승인    OR
        증빙        층 무관 합집합
        금지예시    층 무관 합집합
        근거        합집합 — 두 층의 조문이 다 인용돼야 한다
        적용층      실제로 값을 준 층. 섞였으면 'L1+L2'

    한도가 NULL 로 덮이면 **한도 없음 = 무제한 허용**이라 조용히 "틀린 가능" 이 된다.
    2026-09-01 G 의 L1 9행이 들어오자 실제로 났다 — L1(한도 NULL)이 L2(1대/인)를
    가려서 "PC 몇 대까지" 문항에 한도가 안 붙었다. 잠복 결함이 데이터로 드러난 것이다.
    """
    강도 = lambda a: 허용_강도.get(a, 0)
    허용후보 = [r for r in rows if r["layer"] == 허용층우선] if 허용층우선 else []
    허용원 = 허용후보 or list(rows)
    허용 = max((r["허용"] for r in 허용원), key=강도)
    허용층 = [r["layer"] for r in 허용원 if r["허용"] == 허용]

    # 한도 — 단위별로 묶어 min. 단위가 다르면 서로 다른 제약이라 둘 다 살린다
    단위별: dict[str, dict] = {}
    for r in rows:
        if r.get("한도_값") is None:
            continue
        키 = f'{r.get("한도_유형")}|{_nfkc(r.get("한도_단위") or "")}'
        if 키 not in 단위별 or r["한도_값"] < 단위별[키]["한도_값"]:
            단위별[키] = r
    한도목록 = list(단위별.values())
    주한도 = min(한도목록, key=lambda r: r["한도_값"]) if 한도목록 else None

    def _union(키: str) -> tuple[list[str], list[str]]:
        벌: list[str] = []
        층: list[str] = []
        for r in rows:
            for e in (r.get(키) or []):
                if e not in 벌:
                    벌.append(e)
                    if r["layer"] not in 층:
                        층.append(r["layer"])
        return 벌, 층

    증빙, 증빙층 = _union("증빙")
    금지예시, 금지층 = _union("금지예시")
    # 🔴 2026-09-06(레인 K) — 허용예시는 지금까지 union 이 안 됐다. `금지예시`와
    #    같은 층 무관 합집합으로 더한다. B4문장 3단계 판정(§K)의 재료 중 하나다.
    허용예시, 허용예시층 = _union("허용예시")
    승인층 = [r["layer"] for r in rows if r["사전승인"]]

    # 적용층 = 구속력 있는 값을 실제로 준 층. 화면 7 이 "이 한도는 국가 지침이 아니라
    # 사업 세부관리기준입니다" 를 그리는 근거라, 여기가 틀리면 인용이 틀린다.
    # A 계약: "실제로 값을 준 층. 한 층만이면 그 층, 섞였으면 'L1+L2'".
    # 한도·허용뿐 아니라 증빙·금지예시·사전승인을 준 층도 구속에 기여한 것이다 —
    # 초격차 지급수수료처럼 한도가 없고 허용이 L1 로 정해져도 L2 가 금지예시 9건을
    # 얹으면 그건 L2 도 구속하는 것이다.
    # 🔴 허용예시도 «구속에 기여한 것» 이다 — 금지예시와 같은 이유(위 주석).
    기여 = sorted({r["layer"] for r in 한도목록} | set(허용층) | set(증빙층)
                  | set(금지층) | set(승인층) | set(허용예시층))
    적용층 = "+".join(기여) if len(기여) > 1 else (기여[0] if 기여 else rows[0]["layer"])

    return {
        "허용": 허용,
        "사전승인": any(r["사전승인"] for r in rows),
        "사전승인_조건": next((r["사전승인_조건"] for r in rows if r["사전승인_조건"]), None),
        "한도_유형": 주한도["한도_유형"] if 주한도 else None,
        "한도_값": 주한도["한도_값"] if 주한도 else None,
        "한도_단위": 주한도["한도_단위"] if 주한도 else None,
        "한도목록": 한도목록,
        "증빙": 증빙,
        "금지예시": 금지예시,
        "허용예시": 허용예시,
        "verified": all(r["verified"] for r in rows),
        "근거": [g for r in rows for g in r["근거"]],
        "적용층": 적용층,
        "적용층_기여": sorted({r["layer"] for r in rows}),
        "필드출처": {"허용": 허용층, "한도": [r["layer"] for r in 한도목록],
                    "사전승인": 승인층, "증빙": 증빙층, "금지예시": 금지층,
                    "허용예시": 허용예시층},
    }


def _오버레이(eff: dict, l3: dict) -> dict:
    """L2>L3 우선 조항이 **없는** 사업에서 L3 를 병합해 넣는다 (초격차·TIPS).

    🔴 대체 경로는 "상위가 무조건 이긴다" 가 아니다. `rule_base.md` §3-1 이 대체 경로에
    `허용: 불가>조건부>가능 / 한도: min / 사전승인: OR / 증빙: UNION` 을 명시했는데,
    상위가 통째로 이긴다면 그 연산들이 대체 경로에 있을 이유가 없다.

    같은 대체 경로 규칙을 L1↔L2 에서는 엄격병합으로 쓰면서 L2↔L3 에서만 "base 승" 으로
    두면 **내 코드 안에서 같은 규칙이 두 갈래로 갈린다.** 게다가 그 갈래는
    L3 `불가` → 최종 `조건부` 로 **관대해지는 방향**이라 «틀린 가능» 계열이다.
    (2026-09-01 G 세션 지적. 예비창업으로만 재현하면 안 걸린다 — 그 사업은 조항이 있다)

    ⚠️ `unspecified_only`(L1>L2)는 **L1 대 L2** 를 정하는 조항이지 L3 와는 무관하다.
    그래서 base 쪽 허용은 이미 정해진 것을 쓰고, 여기서는 L3 와만 엄격 비교한다.
    """
    강도 = lambda a: 허용_강도.get(a, 0)
    합 = dict(eff)
    L3더셈 = 강도(l3.get("허용")) > 강도(eff.get("허용"))
    if L3더셈:
        합["허용"] = l3["허용"]

    한도목록 = list(eff.get("한도목록") or [])
    if l3.get("한도_값") is not None:
        키 = lambda r: f'{r.get("한도_유형")}|{_nfkc(r.get("한도_단위") or "")}'
        같은 = next((r for r in 한도목록 if 키(r) == 키(l3)), None)
        if 같은 is None:
            한도목록.append(l3)
        elif l3["한도_값"] < 같은["한도_값"]:
            한도목록[한도목록.index(같은)] = l3
    합["한도목록"] = 한도목록
    주 = min(한도목록, key=lambda r: r["한도_값"]) if 한도목록 else None
    합["한도_유형"] = 주["한도_유형"] if 주 else None
    합["한도_값"] = 주["한도_값"] if 주 else None
    합["한도_단위"] = 주["한도_단위"] if 주 else None

    합["사전승인"] = bool(eff.get("사전승인")) or bool(l3.get("사전승인"))
    합["사전승인_조건"] = eff.get("사전승인_조건") or l3.get("사전승인_조건")
    # 🔴 2026-09-06(레인 K) — 허용예시도 증빙·금지예시와 같은 층 무관 합집합이다.
    for 키2 in ("증빙", "금지예시", "허용예시"):
        합[키2] = list(eff.get(키2) or []) + [x for x in (l3.get(키2) or [])
                                             if x not in (eff.get(키2) or [])]
    합["근거"] = list(eff.get("근거") or []) + list(l3.get("근거") or [])
    합["verified"] = bool(eff.get("verified")) and bool(l3.get("verified"))

    기여했나 = (L3더셈 or 주 is l3 or bool(l3.get("사전승인"))
               or bool(l3.get("증빙")) or bool(l3.get("금지예시"))
               or bool(l3.get("허용예시")))
    if 기여했나:
        층 = sorted(set((eff.get("적용층") or "").split("+")) | {"L3"} - {""})
        합["적용층"] = "+".join(층)
        합["적용층_기여"] = sorted(set(eff.get("적용층_기여") or []) | {"L3"})
        출처 = dict(eff.get("필드출처") or {})
        if L3더셈:
            출처["허용"] = ["L3"]
        if 주 is l3:
            출처["한도"] = ["L3"]
        합["필드출처"] = 출처
    return 합


def _참고L3(l3: dict, 안내: str, eff: dict | None = None) -> dict:
    """화면 7 병기용. 🔴 `기관ID`·기관명은 담지 않는다 — 프롬프트로 새면 TENANT_LEAK 이다.

    `L3더엄격` 은 화면 7 이 `rule_base.md` §3-1 이 정한 문장을 그릴 때 쓴다 —
    "귀 기관 규정은 더 엄격하나, 본 사업 관리기준 제3조에 따라 관리기준이 우선합니다".
    이 문장이 필요한 건 **L3 가 지고 있는데 L3 가 더 엄한 경우**뿐이라 여기서 가른다.
    """
    더엄격 = None
    if eff:
        a, b = 허용_강도.get(l3.get("허용"), 0), 허용_강도.get(eff.get("허용"), 0)
        낮은한도 = (l3.get("한도_값") is not None
                    and (eff.get("한도_값") is None or l3["한도_값"] < eff["한도_값"]))
        더엄격 = a > b or (a == b and 낮은한도)
    return {"rule_id": l3.get("rule_id"), "article_id": l3.get("article_id"),
            "허용": l3.get("허용"), "한도_유형": l3.get("한도_유형"),
            "한도_값": l3.get("한도_값"), "한도_단위": l3.get("한도_단위"),
            "사전승인": l3.get("사전승인"), "근거": l3.get("근거") or [],
            "verified": l3.get("verified"), "L3더엄격": 더엄격, "안내": 안내}


def _층병합(base: Sequence[dict], p_L1L2: Sequence[dict]) -> tuple[list[dict], list[dict], str | None]:
    """효력 계산에 넣을 행들을 고른다. → (골라, 적용조항, 허용층우선)

    🔴 **`unspecified_only` 를 "상위 층으로 통째 갈아끼우기" 로 처리하면 안 된다.**
    그렇게 하면 초격차의 사업별 금지예시 9건·증빙 27종이 통째로 날아간다.
    조항이 정하는 건 **허용과 우선규범뿐**이고, 금지예시·증빙·사전승인·한도는
    두 층을 합쳐야 한다 — L2 는 L1 을 뒤집는 게 아니라 구체화하기 때문이다.
    그래서 행은 늘 둘 다 넘기고, 허용만 `허용층우선` 으로 갈라 준다.

    (2026-09-01 A·G 와 합의. 그전에는 행 자체를 L1 로 갈아끼워서 L2 가 전건 가려졌다)
    """
    L1 = [r for r in base if r["layer"] == "L1"]
    L2 = [r for r in base if r["layer"] == "L2"]
    unspec = [x for x in p_L1L2 if x["범위"] == "unspecified_only"]
    if unspec and L1:
        # 초격차·모두의창업 — "지침을 우선 적용하되, 지침에 명시되지 않은 사항만 본 기준"
        return L1 + L2, list(unspec), "L1"
    return L1 + L2, [], None


def effective_rule(cur, 사업명: str | None, 비목: str | None, 기관ID: str | None = None,
                   *, 수치: dict | None = None, l3룰: dict | None = None,
                    하위항목: str | None = None) -> dict | None:
    """L1·L2·L3 를 `precedence_rules` 로 병합한다. 룰이 없으면 **None** (정상).

    키워드 인자 3개는 §4 동결 시그니처에 **덧붙인 것**이다 — 위치 인자는 그대로라
    `effective_rule(cur, 사업명, 비목)` 호출이 한 글자도 바뀌지 않는다.
    (`금액비교`·`B4문장` 이 입력 수치 없이는 만들어질 수 없어서 통로가 필요했다.)

    🔴 L3 는 자동으로 이기지 않는다. 8사업 중 6개가 적용범위 조에서 `L2 > L3` 를
    명시한다 — 주관기관 규정이 더 엄격해도 진다 (`rule_base.md` §3).
    """
    base = base_룰(cur, 사업명, 비목)
    # 주입된 L3 도 정규화를 태운다 — E 의 산출물은 `corpus.rules` 행이 아니라 키가 다르다
    l3 = _l3정규화(l3룰) if l3룰 is not None else l3_룰_행(cur, 기관ID, 비목, 사업명)
    # 🔴 비목계통 통과 조건은 L1 뿐 아니라 **L3 에도** 걸린다. 주입 경로(A 가 직접
    #    l3_load 로 뽑아 넘기는 길)가 `l3_룰_행` 을 우회하므로 여기서 한 번 더 본다.
    if l3 and not l3적용가능(cur, 사업명):
        l3 = None
    if not base and not l3:
        return None

    p = _precedence(cur, 사업명)
    p_L1L2 = [x for x in p if x["우선계층"] == "L1" and x["열위계층"] == "L2"]
    p_L2L3 = [x for x in p if x["우선계층"] == "L2" and x["열위계층"] == "L3"]
    적용조항: list[dict] = []

    골라, 적용조항, 허용층우선 = _층병합(base, p_L1L2)
    eff = _엄격병합(골라, 허용층우선=허용층우선) if 골라 else None

    # ── ② base 대 L3 ───────────────────────────────────────────────────────
    참고_L3 = None
    if l3:
        if eff is None:
            # 🔴 L3 단독인데 그 L3 가 **자기 허용을 안 가진** 경우가 있다 — `참조만`
            #    ("지침 제N조에 따른다") 이 대표적이다. 가리키는 상위가 없으면
            #    (TIPS 처럼 base 0행) 따라갈 곳도, 스스로 말하는 바도 없다.
            #    그때 `허용=None` 을 만들어 내보내면 계약 enum(가능|조건부|불가) 밖
            #    값이 A 로 흘러간다. 룰이 없는 것이므로 **None 이 맞다** — 실패의
            #    기본값은 판단불가다. (2026-09-01 `--smoke` 가 첫 실행에서 잡았다)
            if l3.get("허용") not in 허용_강도:
                return None
            eff = _엄격병합([l3])
            적용조항 += [{"해석": "상위에 해당 비목 룰이 없어 주관기관 규정을 적용한다"}]
        elif p_L2L3 and any(x["범위"] == "all" for x in p_L2L3):
            참고_L3 = _참고L3(l3, "귀 기관 규정은 참고로 병기한다. 본 사업 관리기준이 우선한다", eff)
            적용조항 += [x for x in p_L2L3 if x["범위"] == "all"]
        else:
            # 🔴 조항 없음(초격차·TIPS) → 대체 경로는 **엄격한 쪽**이다. base 가 자동으로
            #    이기지 않는다 — L1↔L2 대체 경로와 같은 규칙을 써야 한다. §_오버레이 참조.
            참고_L3 = _참고L3(l3, "이 사업에는 L2>L3 우선순위 조항이 없다. "
                                  "상위 규범과 기관 규정 중 엄격한 쪽을 적용한다", eff)
            eff = _오버레이(eff, l3)

    if eff is None:
        return None

    비교 = _한도전수(eff.get("한도목록") or [], 수치, 하위항목)

    우선규범 = _우선규범(cur, 사업명)
    강등권고 = [] if eff["verified"] else ["UNVERIFIED_RULE"]

    return {
        "허용": eff["허용"],
        "적용층": eff["적용층"],
        "우선규범": 우선규범,
        "사전승인": eff["사전승인"],
        "증빙": eff["증빙"],
        "verified": eff["verified"],
        "근거": eff["근거"],
        "금액비교": 비교,
        # 🔴 2026-09-06(레인 K) — 금지예시·허용예시·사전승인_조건을 B4문장에 «싣는다».
        #    지금까지 eff/반환 dict 엔 있었는데 B4문장 인자로 «안 넘어갔다» — 그래서
        #    구체 내용이 하나도 없어도 "조건을 붙여 허용한다" 라는 확정 문장만 나갔다.
        "B4문장": B4문장(비목, eff["허용"], eff["사전승인"], eff["증빙"], 비교,
                        eff["적용층"], 우선규범, 참고_L3,
                        금지예시=eff["금지예시"], 허용예시=eff["허용예시"],
                        사전승인_조건=eff["사전승인_조건"]),
        "룰들": [{"verified": r["verified"], "layer": r["layer"], "rule_id": r["rule_id"]}
                 for r in 골라] + ([{"verified": l3["verified"], "layer": "L3",
                                     "rule_id": l3.get("rule_id"),
                                     "article_id": l3.get("article_id")}] if l3 else []),
        # ── 부가 (A 가 무시해도 무방) ──
        "사전승인_조건": eff["사전승인_조건"],
        "참고_L3": 참고_L3,
        "적용조항": [{"해석": x.get("해석"), "원문": x.get("원문"), "근거": x.get("근거")}
                    for x in 적용조항],
        "강등권고": 강등권고,
        # 화면 7 이 "이 한도는 국가 지침이 아니라 사업 세부관리기준입니다" 를 그리는 재료.
        # `적용층` 이 'L1+L2' 로 뭉뚱그려질 때 어느 필드가 어느 층에서 왔는지 여기서 푼다
        "필드출처": eff["필드출처"],
        "적용층_기여": eff["적용층_기여"],
        "금지예시": eff["금지예시"],      # 층 무관 합집합. 통과 조건 A 는 별도로 base 를 훑는다
        "허용예시": eff["허용예시"],      # 🔴 2026-09-06(레인 K) 신설 — 지금까지 안 돌아갔다
        "한도목록": [{"유형": r["한도_유형"], "값": r["한도_값"], "단위": r["한도_단위"],
                     "layer": r["layer"], "rule_id": r["rule_id"]}
                    for r in (eff.get("한도목록") or [])],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 8. 동결 인터페이스 ④ l3_게이팅 (B9)
# ══════════════════════════════════════════════════════════════════════════════

def _참조만(r: dict) -> bool:
    """"지침 제N조에 따른다" 만 있는 행 — 자기 내용이 없다.

    스키마에 `참조만` 컬럼이 없어 모양으로 판정한다. E 가 픽스처에 명시 플래그를
    달아 주면 그쪽을 먼저 본다.
    """
    if r.get("참조만") is not None:
        return bool(r["참조만"])
    실체 = (r.get("금지예시") or []) + (r.get("허용예시") or []) + (r.get("증빙") or [])
    return (not 실체 and r.get("한도_값") is None and not r.get("사전승인")
            and bool(r.get("근거")))


def l3_게이팅(l3룰: dict | None) -> dict:
    """상위(L1·L2)를 봐야 하는가 — 코드가 가른다. LLM 에게 묻지 않는다.

    🔴 **(4) L3 가 "가능" 이면 need_upper=True 를 강제한다.** 주관기관 규정은 국가
    지침이 있다고 전제하고 그 위에 얹는 문서라 구조적으로 자족하지 않는다. L3 의
    "총장 승인만 받으면 됩니다" 만 보고 답하면 상위의 제약을 놓친 **틀린 "가능"** 이
    된다. "불가" 는 단독으로 안전하지만 "가능" 은 절대 L3 단독으로 확정하지 않는다.
    """
    if not l3룰:
        return {"need_upper": True, "seed_refs": [],
                "갈래": "1-미규정", "사유": "L3 에 해당 비목 규정이 없다 = 정하지 아니한 사항"}
    근거 = list(l3룰.get("근거") or [])
    if _참조만(l3룰):
        return {"need_upper": True, "seed_refs": 근거,
                "갈래": "2-참조만", "사유": "L3 가 상위 조문을 가리키기만 한다"}
    허용 = l3룰.get("허용")
    if 허용 in ("불가", "조건부"):
        return {"need_upper": False, "seed_refs": 근거,
                "갈래": "3-L3제약", "사유": "L3 가 막거나 조건을 건다 — 상위가 관대해도 결론 불변"}
    if 허용 == "가능":
        return {"need_upper": True, "seed_refs": 근거,
                "갈래": "4-L3가능", "사유": "L3 단독 '가능' 은 확정하지 않는다 (오답 비대칭)"}
    return {"need_upper": True, "seed_refs": 근거,
            "갈래": "0-불명", "사유": f"허용값 '{허용}' 을 해석할 수 없다 — 안전측으로 상위를 본다"}


# ══════════════════════════════════════════════════════════════════════════════
# 9. CLI — 자기검사와 정답셋 3분류
# ══════════════════════════════════════════════════════════════════════════════

def _connect():
    """🔴 `autocommit=True` — 8세션 병렬에서 조회용 커넥션의 기본값이다.

    2026-08-31 실제로 교착났다. 이 모듈의 조회 하나가 `item_alias` 에 읽기락을
    쥔 채 idle in transaction 으로 17분을 앉아 있었고, 그 사이 D 의 DDL 이
    `corpus.rules`·`tenant.f_exec` 에 AccessExclusiveLock 을 쥔 채 그 읽기락을
    기다리면서 **8세션 전체의 rules 읽기가 멈췄다**. 읽기만 하는 커넥션은
    트랜잭션을 열지 않는다.
    """
    return db.connect(autocommit=True)


def _self_test() -> int:
    실패 = []

    def eq(이름, got, want):
        if got != want:
            실패.append(f"{이름}: {got!r} != {want!r}")

    # 정규화 대칭 — 조사가 붙든 말든 같은 정규형
    eq("조사1", _norm("기프티콘을"), _norm("기프티콘"))
    eq("조사2", _norm("유니폼 제작은"), _norm("유니폼제작"))
    eq("조사3", _norm("손잡이를"), _norm("손잡이"))
    eq("공백", _norm("광고 선전비"), _norm("광고선전비"))
    eq("한글자보호", _norm("회의"), "회의")

    # 금지예시 해부 — 예외단서 판별이 오늘의 핵심
    eq("예외1", 금지예시_해부("귀금속·보석·원석(사전승인 시 예외)")["무조건"], False)
    eq("예외2", 금지예시_해부("범용성 사무용 소프트웨어(사전검토 시 예외)")["무조건"], False)
    eq("예외3", 금지예시_해부("전대차 계약 사무실임차료(공유오피스 월단위 제외)")["무조건"], False)
    eq("예외4", 금지예시_해부("시제품 제작에 직·간접적으로 활용되는 기구·비품"
                              "(기계장치비로 구매해야 함)")["무조건"], False)
    # 🔴 아래 둘은 예외가 아니다 — 정답셋 39·43번의 정답이 '불가' 다
    eq("비예외1", 금지예시_해부("특허 등록 성공보수(대행 수수료는 가능)")["무조건"], True)
    eq("비예외2", 금지예시_해부("창업기업 대표자 인건비(현물로만 계상 가능)")["무조건"], True)
    eq("비예외3", 금지예시_해부("개인 간 중고거래")["무조건"], True)
    eq("핵추출", 금지예시_해부("귀금속·보석·원석(사전승인 시 예외)")["핵"], "귀금속·보석·원석")

    # 단위 해부
    u = _단위해부("원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)")
    eq("단위기본", u["기본"], "원")
    # 🔴 중첩 괄호 — 재도전 지급수수료 실제 값. 비중첩 regex 로 지우면 분모가 산문째 오염된다
    n = _단위해부("원/인/일(멘토링비 한정. 별도 한도 2건 — (예비)재창업자별 멘토링비 "
                  "총액 500만원(사업기간 중 합산) · 세무·회계 기장대행 수수료 월 20만원)")
    eq("중첩_기본", n["기본"], "원")
    eq("중첩_분모", n["분모"], ["인", "일"])
    eq("중첩_한정", n["한정"], "멘토링비")
    eq("단위분모", u["분모"], ["인", "일"])
    eq("단위한정", u["한정"], "멘토링비")

    # 금액비교 — 입력이 없으면 추측하지 않는다
    r금액 = {"한도_유형": "금액", "한도_값": 300000.0, "한도_단위": "원/인/일"}
    eq("금액_미입력", 금액비교(r금액, None)["초과"], None)
    eq("금액_분모없음", 금액비교(r금액, {"금액": 500000})["초과"], None)
    eq("금액_초과", 금액비교(r금액, {"금액": 500000, "인원": 1, "일수": 1})["초과"], True)
    eq("금액_이내", 금액비교(r금액, {"금액": 200000, "인원": 1, "일수": 1})["초과"], False)
    r비율 = {"한도_유형": "비율", "한도_값": 50.0, "한도_단위": "%(선급금/계약총액)"}
    eq("비율_미입력", 금액비교(r비율, {"금액": 1000})["초과"], None)
    eq("비율_초과", 금액비교(r비율, {"선급금": 700, "계약총액": 1000})["초과"], True)
    r개수 = {"한도_유형": "개수", "한도_값": 1.0, "한도_단위": "대/인(PC·노트북·태블릿)"}
    eq("개수_초과", 금액비교(r개수, {"수량": 3, "인원": 2})["초과"], True)
    eq("개수_이내", 금액비교(r개수, {"수량": 2, "인원": 2})["초과"], False)
    # 하위항목 한정 — 멘토링비 한도를 사무실임차료에 걸면 안 된다.
    # 🔴 실제 corpus.rules 지급수수료 행의 단위가 이 모양이다
    r멘토 = {"한도_유형": "금액", "한도_값": 300000.0,
             "한도_단위": "원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)"}
    eq("한정_불일치", 금액비교(r멘토, {"금액": 9_000_000, "인원": 1, "일수": 1},
                              하위항목="사무실임차료"), None)
    eq("한정_미상", 금액비교(r멘토, {"금액": 9_000_000, "인원": 1, "일수": 1})["초과"], None)
    eq("한정_일치", 금액비교(r멘토, {"금액": 500000, "인원": 1, "일수": 1},
                            하위항목="멘토링비")["초과"], True)

    # B4 문장 — 원시 한도값이 새어나가면 안 된다
    s = B4문장("지급수수료", "조건부", True, ["세금계산서"],
               {"초과": True, "사유": "단가가 한도를 초과한다"}, "L2", None)
    if any(t in s for t in ("300000", "300,000", "30만")):
        실패.append(f"B4문장에 원시 한도값 노출: {s}")

    # ── 🔴 2026-09-06(레인 K) — B4 3단계 ────────────────────────────────────
    # 실체없음 — 금지예시·허용예시·한도·사전승인·증빙 중 «하나라도» 있으면 False
    eq("실체없음_전부없음", _실체없음(None, None, None, False, []), True)
    eq("실체없음_금지예시있음", _실체없음(["ㄱ"], None, None, False, []), False)
    eq("실체없음_허용예시있음", _실체없음(None, ["ㄴ"], None, False, []), False)
    eq("실체없음_한도있음",
       _실체없음(None, None, {"초과": None, "사유": "x"}, False, []), False)
    eq("실체없음_사전승인있음", _실체없음(None, None, None, True, []), False)
    eq("실체없음_증빙있음", _실체없음(None, None, None, False, ["갑"]), False)

    # 있는데 구체 내용 없음 -> "조건을 붙여 허용한다" 를 «대신한다» (거짓 확정문 방지)
    s3 = B4문장("여비", "조건부", False, [], None, "L2", None)
    if "조건을 붙여 허용한다" in s3:
        실패.append(f"실체없음인데 옛 확정 문장이 그대로 나감: {s3}")
    if "구체적으로 다루는" not in s3:
        실패.append(f"실체없음 문장이 안 나옴: {s3}")

    # 있고 구체 내용 있음 -> 실제 항목이 실린다(카운트가 아니라 내용 그 자체)
    s2 = B4문장("여비", "불가", False, [], None, "L2", None,
                금지예시=["귀금속", "성공보수"], 허용예시=["시제품용 소재"])
    eq("B4_금지예시_실림", ("귀금속" in s2) and ("성공보수" in s2), True)
    eq("B4_허용예시_실림", "시제품용 소재" in s2, True)
    if "구체적으로 다루는" in s2:
        실패.append(f"금지예시가 있는데 실체없음 문장이 나감: {s2}")

    # 🔴 2026-09-06(레인 K) — 토큰 캡. 최대개수 넘으면 자르고 "외 N종" 을 붙인다
    많음 = [f"항목{i}" for i in range(12)]
    s캡 = B4문장("여비", "불가", False, [], None, "L2", None, 금지예시=많음)
    eq("캡_8개만_실림", all(f"항목{i}" in s캡 for i in range(8)), True)
    eq("캡_9번째는_안실림", "항목8" not in s캡, True)
    eq("캡_외N종_표시", "외 4종" in s캡, True)
    적음 = [f"항목{i}" for i in range(3)]
    s무캡 = B4문장("여비", "불가", False, [], None, "L2", None, 금지예시=적음)
    eq("캡_미만이면_안잘림", "외" not in s무캡, True)

    # 사전승인_조건 도 실린다
    s4 = B4문장("여비", "조건부", True, [], None, None, None, 사전승인_조건="전문기관 승인")
    if "전문기관 승인" not in s4:
        실패.append(f"사전승인_조건이 안 실림: {s4}")

    # 🔴 회귀 방지 — 허용 자체가 None(안 정해짐)이면 실체없음 판정을 «안 탄다».
    #    옛 문장("명시되어 있지 않다")이 그대로 나가야 한다 — «없다» 와 «있는데 구체
    #    내용 없다» 는 다른 상태다.
    s5 = B4문장("여비", None, False, [], None, None, None)
    eq("허용None_기존문장유지", "명시되어 있지 않다" in s5, True)

    # 🔴 층병합 — L1 이 0행이라 오늘은 발화하지 않는 분기다. G3 이후 처음 돈다
    def R(layer, 허용, 한도=None, rid=0, ver=True):
        return {"layer": layer, "허용": 허용, "한도_유형": "금액" if 한도 else None,
                "한도_값": 한도, "한도_단위": "원", "사전승인": False,
                "사전승인_조건": None, "증빙": [], "근거": [], "verified": ver,
                "rule_id": rid}
    unspec = [{"우선계층": "L1", "열위계층": "L2", "범위": "unspecified_only"}]
    l1 = {**R("L1", "조건부", None, 1), "금지예시": ["ㄱ"], "증빙": ["갑"]}
    l2 = {**R("L2", "가능", 100.0, 2), "금지예시": ["ㄴ"], "증빙": ["을"], "사전승인": True}

    # 🔴 unspecified_only 는 **허용·우선규범에만** 걸린다. 행은 늘 둘 다 넘어간다 —
    #    상위로 통째 갈아끼우면 초격차의 사업별 금지예시·증빙이 날아간다
    골, 조항, 우선 = _층병합([l1, l2], unspec)
    eq("층병합_unspec_행", sorted(r["rule_id"] for r in 골), [1, 2])
    eq("층병합_unspec_허용층", 우선, "L1")
    eq("층병합_unspec_조항", len(조항), 1)
    m = _엄격병합(골, 허용층우선=우선)
    eq("unspec_허용은L1", m["허용"], "조건부")          # L1 이 정한다
    eq("unspec_한도는L2", m["한도_값"], 100.0)          # 🔴 NULL 이 이기면 안 된다
    eq("unspec_금지합집합", sorted(m["금지예시"]), ["ㄱ", "ㄴ"])
    eq("unspec_증빙합집합", sorted(m["증빙"]), ["갑", "을"])
    eq("unspec_사전승인OR", m["사전승인"], True)
    eq("unspec_적용층", m["적용층"], "L1+L2")

    # 조항 없음(6사업) — 허용은 엄격한 쪽, 나머지는 합집합
    골2, 조항2, 우선2 = _층병합([l1, l2], [])
    eq("층병합_폴백_행", sorted(r["rule_id"] for r in 골2), [1, 2])
    eq("층병합_폴백_우선없음", 우선2, None)
    eq("층병합_폴백_조항", 조항2, [])
    m2 = _엄격병합(골2)
    eq("폴백_허용엄격", m2["허용"], "조건부")
    eq("폴백_한도유지", m2["한도_값"], 100.0)
    eq("폴백_적용층", m2["적용층"], "L1+L2")
    eq("폴백_필드출처_한도", m2["필드출처"]["한도"], ["L2"])

    # 한 층만이면 적용층도 한 층
    eq("단층_적용층", _엄격병합([l2])["적용층"], "L2")
    eq("층병합_빈입력", _층병합([], [])[0], [])

    # 🔴 2026-09-06(레인 K) — 허용예시도 금지예시·증빙과 같은 층 무관 합집합인지
    l1c = {**l1, "허용예시": ["ㄷ"]}
    l2c = {**l2, "허용예시": ["ㄹ"]}
    mc = _엄격병합([l1c, l2c])
    eq("허용예시_합집합", sorted(mc["허용예시"]), ["ㄷ", "ㄹ"])
    eq("허용예시_필드출처", sorted(mc["필드출처"]["허용예시"]), ["L1", "L2"])

    # 오버레이(L2↔L3, 초격차·TIPS 대체 경로)에서도 허용예시가 합쳐지는지
    l3fx = {**R("L3", "조건부", None, 3), "허용예시": ["ㅁ"], "금지예시": [], "증빙": []}
    ov = _오버레이(mc, l3fx)
    eq("오버레이_허용예시_합쳐짐", sorted(ov["허용예시"]), ["ㄷ", "ㄹ", "ㅁ"])

    # 🔴 산문에 숨은 추가 한도 — "이내" 라고 단정하면 «틀린 가능» 이 된다
    eq("숨은한도_별도", 미파싱한도("원/인/일(멘토링비 한정. 별도 한도 2건 — 총액 500만원 "
                                  "· 세무·회계 기장대행 수수료 월 20만원)") != [], True)
    eq("숨은한도_지역", 미파싱한도("원/인/박(국내 숙박비 · 특별시 기준. 광역시 8만원, "
                                  "그 밖의 지역 7만원)") != [], True)
    eq("숨은한도_시간당", 미파싱한도("원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)") != [], True)
    # 원천징수 규칙은 한도가 아니다 · 비율 분모 이름도 아니다
    eq("숨은한도_세금아님", 미파싱한도("원/인/일(멘토링비 한정, 12만5천원 초과 시 "
                                      "기타소득세 8.8% 공제)"), [])
    eq("숨은한도_분모아님", 미파싱한도("%(선급금/계약총액)"), [])
    eq("숨은한도_없음", 미파싱한도("대/인(PC·노트북·태블릿)"), [])
    숨김행 = {**R("L2", "조건부", 300000.0, 9), "한도_유형": "금액",
              "한도_단위": "원/인/일(시간당 10만원 초과 불가)"}
    eq("숨은한도_이내를미상으로", _한도전수([숨김행], {"금액": 100, "인원": 1, "일수": 1})["초과"], None)
    eq("숨은한도_초과는유지", _한도전수([숨김행], {"금액": 9e6, "인원": 1, "일수": 1})["초과"], True)

    # 🔴 한도 NULL 이 한도 있는 층을 가리면 안 된다 (2026-09-01 회귀의 본체)
    가림 = _엄격병합([R("L1", "조건부", None, 1), R("L2", "조건부", 1.0, 2)])
    eq("한도NULL_안가림", 가림["한도_값"], 1.0)
    eq("한도NULL_적용층", 가림["적용층"], "L1+L2")

    # 단위가 다른 한도는 둘 다 산다 — min 으로 접으면 하나가 사라진다
    개 = {**R("L2", "조건부", 1.0, 3), "한도_유형": "개수", "한도_단위": "대/인"}
    원 = {**R("L2", "조건부", 500000.0, 4), "한도_유형": "금액", "한도_단위": "원/월"}
    다단위 = _엄격병합([개, 원])
    eq("다단위_보존", len(다단위["한도목록"]), 2)
    eq("다단위_초과", _한도전수(다단위["한도목록"],
                              {"수량": 3, "인원": 1, "금액": 100, "월수": 1})["초과"], True)
    eq("다단위_이내", _한도전수(다단위["한도목록"],
                              {"수량": 1, "인원": 1, "금액": 100, "월수": 1})["초과"], False)
    eq("다단위_미상", _한도전수(다단위["한도목록"], {"수량": 1, "인원": 1})["초과"], None)

    # L3 정규화 — E 의 산출물은 corpus.rules 행이 아니다 (rule_id 없음 · article_id 로 인용)
    e3 = _l3정규화({"layer": "L3", "비목": "재료비", "허용": "불가", "참조만": False,
                    "사전승인": True, "한도_값": None, "verified": False,
                    "근거": [{"article_id": 24, "doc_id": "x", "조번호": "제5조"}]})
    eq("L3정규화_rule_id없음", e3["rule_id"], None)
    eq("L3정규화_article_id", e3["article_id"], 24)
    eq("L3정규화_기관끊김", e3["기관id"], None)
    eq("L3정규화_빈배열", (e3["증빙"], e3["금지예시"]), ([], []))
    eq("L3정규화_None통과", _l3정규화(None), None)
    # 참고_L3 — L3 가 지지만 더 엄격할 때만 rule_base §3-1 문장을 쓴다
    eq("참고L3_더엄격", _참고L3(e3, "안내", {"허용": "조건부", "한도_값": None})["L3더엄격"], True)
    eq("참고L3_안엄격", _참고L3({**e3, "허용": "가능"}, "안내",
                               {"허용": "조건부", "한도_값": None})["L3더엄격"], False)
    eq("참고L3_기관없음", "기관id" in _참고L3(e3, "안내"), False)

    # 🔴 _오버레이 — L2>L3 조항이 **없는** 사업(초격차·TIPS)의 대체 경로.
    #    같은 대체 경로 규칙을 L1↔L2 에선 엄격병합, L2↔L3 에선 base 승으로 두면 규칙이 갈린다.
    #    그 갈래는 L3 불가 → 조건부 로 관대해지는 방향이라 «틀린 가능» 계열이다.
    base쪽 = _엄격병합([R("L1", "조건부", None, 1), R("L2", "조건부", 300000.0, 2)])
    L3불가 = _l3정규화({"허용": "불가", "verified": False,
                        "근거": [{"article_id": 24, "doc_id": "x", "조번호": "제5조"}]})
    ov = _오버레이(base쪽, L3불가)
    eq("오버레이_L3불가승", ov["허용"], "불가")
    eq("오버레이_적용층", ov["적용층"], "L1+L2+L3")
    eq("오버레이_허용출처", ov["필드출처"]["허용"], ["L3"])
    # L3 가 더 관대하면 base 가 유지된다 — 오버레이는 완화하지 않는다
    ov2 = _오버레이(base쪽, _l3정규화({"허용": "가능", "verified": True, "근거": []}))
    eq("오버레이_완화안함", ov2["허용"], "조건부")
    eq("오버레이_적용층불변", ov2["적용층"], base쪽["적용층"])
    # 한도는 더 낮은 쪽 — L3 20만 < L2 30만
    ov3 = _오버레이(base쪽, _l3정규화({"허용": "조건부", "한도_유형": "금액", "한도_값": 200000,
                                      "한도_단위": "원", "verified": False, "근거": []}))
    eq("오버레이_한도낮은쪽", ov3["한도_값"], 200000.0)
    eq("오버레이_한도출처", ov3["필드출처"]["한도"], ["L3"])
    eq("오버레이_verified_AND", ov3["verified"], False)

    # 🔴 org_id 타입 — corpus.rules.기관id 는 text, tenant.orgs.org_id 는 uuid.
    #    UUID 객체를 그대로 바인딩하면 "operator does not exist: text = uuid" 로 죽는다
    import uuid as _uuid
    u = _uuid.UUID("1d6be2e1-7296-5492-a24b-c0838b431a7f")
    eq("org문자열_UUID", _org문자열(u), "1d6be2e1-7296-5492-a24b-c0838b431a7f")
    eq("org문자열_str", _org문자열(str(u)), str(u))
    eq("org문자열_None", _org문자열(None), None)
    eq("org문자열_빈문자", _org문자열(""), None)

    # L3 게이팅 4갈래
    eq("게이팅1", l3_게이팅(None)["need_upper"], True)
    eq("게이팅2", l3_게이팅({"허용": "가능", "근거": [{"doc_id": "x", "조번호": "제1조"}],
                            "금지예시": [], "허용예시": [], "증빙": [],
                            "한도_값": None, "사전승인": False})["갈래"], "2-참조만")
    eq("게이팅3", l3_게이팅({"허용": "불가", "근거": [], "금지예시": ["a"],
                            "허용예시": [], "증빙": [], "한도_값": None,
                            "사전승인": False})["need_upper"], False)
    eq("게이팅4", l3_게이팅({"허용": "가능", "근거": [], "금지예시": [],
                            "허용예시": ["a"], "증빙": [], "한도_값": None,
                            "사전승인": False})["need_upper"], True)

    for f in 실패:
        print("  ✗", f)
    print(f"self-test {'실패 ' + str(len(실패)) if 실패 else '통과'}")
    return 1 if 실패 else 0


def 비목추정_문장(cur, 문장: str, 사업명: str | None) -> list[dict]:
    """⚠️ **평가 전용.** 판정 경로에서 부르지 마라 — A 는 `비목확정()` 을 쓴다.

    (1) 정규화 LLM 이 뽑아 줄 `품목` 을 LLM 없이 흉내 낸다: 별칭 상품명이 문장
    안에 문자 그대로 들어 있으면 그중 가장 긴 것을 고른다. 드라이런 카운트를
    내려고 만든 것이고, 이 함수의 정확도는 지표가 아니다.
    """
    본문 = _norm(문장)
    cur.execute("SELECT 상품명, 비목 FROM corpus.item_alias "
                "WHERE 사업명 = %s OR 사업명 IS NULL", [사업명])
    어휘: list[tuple[str, str]] = list(cur.fetchall())
    # 비목 정본명 자체("인건비를 줘도 되나요")도 잡아야 한다 — `비목확정()` 의
    # `_용어사전_직결` 과 같은 재료를 쓰지 않으면 대역이 실제보다 못해 보인다
    try:
        cur.execute("SELECT 비목, 별칭, 하위항목 FROM corpus.item_vocab WHERE 계통='창업'")
        for 비목, 별칭, 하위 in cur.fetchall():
            어휘 += [(비목, 비목)] + [(a, 비목) for a in (별칭 or []) + (하위 or [])]
    except Exception:
        cur.connection.rollback()

    hits = []
    for 상품명, 비목 in 어휘:
        n = _norm(상품명)
        # 2자 별칭(맥북·책상·외주·특허)이 정답셋 품목의 상당수다. 여기서 3자로 끊으면
        # 비목확정 능력이 아니라 이 대역 함수의 임계가 지표로 잡힌다
        if len(n) >= 2 and n in 본문:
            hits.append({"비목": 비목, "신뢰도": 0.9, "출처": "alias",
                         "매칭": 상품명, "길이": len(n)})
    hits.sort(key=lambda h: -h["길이"])
    본: set[str] = set()
    out = []
    for h in hits:
        if h["비목"] not in 본:
            본.add(h["비목"])
            out.append(h)
    return out[:3]


def _calibrate() -> int:
    """별칭 leave-one-out — `비목확정()` 벡터 임계치의 실측 근거."""
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT alias_id, 상품명, 비목 FROM corpus.item_alias "
                    "WHERE embedding IS NOT NULL ORDER BY alias_id")
        rows = cur.fetchall()
        res = []
        for aid, nm, bm in rows:
            cur.execute(
                "SELECT 비목, 상품명, 1 - (embedding <=> "
                "  (SELECT embedding FROM corpus.item_alias WHERE alias_id=%s)) "
                "FROM corpus.item_alias WHERE alias_id <> %s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> "
                "  (SELECT embedding FROM corpus.item_alias WHERE alias_id=%s) LIMIT 1",
                [aid, aid, aid])
            r = cur.fetchone()
            res.append({"sim": float(r[2]), "일치": r[0] == bm, "질의": nm,
                        "정답비목": bm, "이웃": r[1], "이웃비목": r[0]})
    표 = []
    for th in (0.55, 0.60, 0.62, 0.65, 0.68, 0.70, 0.72, 0.75, 0.78, 0.80, 0.85):
        sel = [x for x in res if x["sim"] >= th]
        if not sel:
            continue
        표.append({"임계": th, "발화율": round(len(sel) / len(res), 4),
                   "비목일치": round(sum(x["일치"] for x in sel) / len(sel), 4),
                   "n": len(sel)})
        print(f"  임계 {th:.2f}  발화 {len(sel)/len(res):5.1%}  "
              f"비목일치 {sum(x['일치'] for x in sel)/len(sel):5.1%}  (n={len(sel)})")
    out = ROOT / "scripts" / "_work" / "_B_비목확정_임계교정.json"
    out.write_text(json.dumps({"표본": len(res), "표": 표,
                               "오분류": [x for x in res if not x["일치"]]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")
    return 0


def _audit() -> int:
    """금지매칭 감사 — 금지예시 자기재현 · 괄호 26종 분류 · 정답셋 발화."""
    괄호분류: dict[str, dict] = {}
    자기재현 = {"전체": 0, "무조건": 0, "예외단서": 0, "정확일치복원": 0}
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT rule_id, 사업명, 비목, unnest(금지예시) FROM corpus.rules")
        for rule_id, 사업명, 비목, 예시 in cur.fetchall():
            h = 금지예시_해부(예시)
            자기재현["전체"] += 1
            자기재현["무조건" if h["무조건"] else "예외단서"] += 1
            for m in _괄호.findall(_nfkc(예시)):
                괄호분류.setdefault(m, {"건수": 0, "예외로_판정": bool(
                    _예외단서.search(m) or _비목재분류.search(m))})["건수"] += 1
            # 핵을 그대로 품목으로 넣으면 반드시 자기 자신에 적중해야 한다
            if h["무조건"] and 금지적중(cur, h["핵"], None, 사업명, 비목):
                자기재현["정확일치복원"] += 1

        골든 = {"적중": 0, "조건부근접": 0, "전체": 0, "적중목록": []}
        cur.execute("SELECT gold_id, 사업명, 질문, 정답판정 FROM eval.golden_set ORDER BY gold_id")
        for gold_id, 사업명, 질문, 정답 in cur.fetchall():
            골든["전체"] += 1
            후보 = 비목추정_문장(cur, 질문, 사업명)
            비목 = 후보[0]["비목"] if 후보 else None
            r = 금지후보(cur, 질문, None, 사업명, 비목)
            if r["무조건"]:
                골든["적중"] += 1
                골든["적중목록"].append({"gold_id": gold_id, "정답": 정답,
                                        "예시": r["무조건"][0]["예시"]})
            elif r["조건부"]:
                골든["조건부근접"] += 1

    무조건수 = 자기재현["무조건"]
    print(f"금지예시 {자기재현['전체']}개 — 무조건 {무조건수} / 예외단서 {자기재현['예외단서']}")
    print(f"자기재현(핵을 품목으로 넣으면 적중): {자기재현['정확일치복원']}/{무조건수}")
    print(f"골든셋 {골든['전체']}문항 중 금지적중 {골든['적중']} · 조건부근접 {골든['조건부근접']}")
    for x in 골든["적중목록"]:
        print(f"   #{x['gold_id']} 정답={x['정답']}  ← {x['예시']}")
    out = ROOT / "scripts" / "_work" / "_B_금지매칭_감사.json"
    out.write_text(json.dumps({"자기재현": 자기재현, "골든셋": 골든,
                               "괄호_분류": 괄호분류},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")
    return 0 if 자기재현["정확일치복원"] == 무조건수 else 1


def _smoke() -> int:
    """🔴 **실 커넥션 스모크.** `--self-test` 가 구조적으로 못 잡는 것을 잡는다.

    self-test 는 DB 없이 도는 순수 함수만 덮는다. 그래서 **타입 불일치는 원리적으로
    안 걸린다** — 2026-09-01 에 `corpus.rules.기관id`(text) 대 `tenant.orgs.org_id`(uuid)
    로 실제로 터졌고, self-test 는 60건이 다 통과하고 있었다.

    그래서 여기서는 **DB 에서 꺼낸 값을 가공 없이 그대로** 넘긴다. `str()` 로 감싸는
    순간 이 스모크는 자기가 잡아야 할 버그를 스스로 숨긴다.
    """
    실패: list[str] = []
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT 사업명 FROM corpus.programs WHERE 활성 ORDER BY 사업명")
        사업들 = [r[0] for r in cur.fetchall()] or [None]
        cur.execute("SELECT 비목 FROM corpus.item_vocab WHERE 계통='창업' ORDER BY 정렬")
        비목들 = [r[0] for r in cur.fetchall()]
        # 🔴 2026-09-02 — 전에는 `SELECT org_id FROM tenant.orgs` 였다. `tenant.orgs`
        #    가 2행일 땐 조합이 270 이라 즉시 끝났는데, 기관 명부 413행이 적재되자
        #    9×10×414 ≈ 37,000 이 되어 **스모크가 몇 분을 넘겨도 안 끝났다.**
        #    이 스모크가 재려는 것은 «L3 가 붙었을 때 effective_rule 이 형태를 지키나»
        #    이지 «기관이 몇 곳이나 있나» 가 아니다. L3 조문을 실제로 가진 기관만 돈다 —
        #    나머지 기관은 l3 가 0행이라 `None` 과 같은 경로를 되풀이할 뿐이다.
        cur.execute("SELECT DISTINCT org_id FROM tenant.l3_articles")
        orgs = [r[0] for r in cur.fetchall()]        # 🔴 UUID 객체 그대로. str() 금지

        n = 0
        for 사업 in 사업들 + [None]:
            for 비목 in 비목들:
                for org in orgs + [None]:
                    n += 1
                    try:
                        e = effective_rule(cur, 사업, 비목, org,
                                           수치={"금액": 100000, "인원": 1, "일수": 1})
                        if e is not None:
                            if e["허용"] not in 허용_강도:
                                실패.append(f"{사업}/{비목}/{org}: 허용 '{e['허용']}'")
                            for k in ("적용층", "B4문장", "룰들", "증빙", "근거"):
                                if k not in e:
                                    실패.append(f"{사업}/{비목}/{org}: '{k}' 누락")
                            # 🔴 기관 식별자가 반환에 섞이면 TENANT_LEAK 이다
                            if org and str(org) in json.dumps(e, ensure_ascii=False,
                                                              default=str):
                                실패.append(f"{사업}/{비목}: 반환에 org_id 누출")
                        l3_게이팅(l3_룰_행(cur, org, 비목))
                        금지적중(cur, "노트북", "업무용", 사업, 비목)
                        비목확정(cur, "맥북", 사업, 벡터허용=False)
                    except Exception as ex:
                        실패.append(f"{사업}/{비목}/{org}: "
                                    f"{type(ex).__name__}: {str(ex).splitlines()[0][:70]}")
    for f in 실패[:20]:
        print("  ✗", f)
    print(f"스모크 {n}조합 (사업 {len(사업들)+1} × 비목 {len(비목들)} × org {len(orgs)+1}) "
          f"— {'실패 ' + str(len(실패)) if 실패 else '전건 통과'}")
    return 1 if 실패 else 0


def _golden() -> int:
    """정답셋 전건 3분류 카운트. `_B_골든셋_룰커버.json` 으로 남긴다."""
    카운트 = {"룰있음": 0, "룰없음(공통)": 0, "룰없음(사업지정)": 0, "금지적중": 0}
    행: list[dict] = []
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT gold_id, 세트, 사업명, 질문, 정답판정, 비목 "
                    "FROM eval.golden_set ORDER BY gold_id")
        for gold_id, 세트, 사업명, 질문, 정답, 비목_ in cur.fetchall():
            후보 = 비목추정_문장(cur, 질문, 사업명)      # LLM 없는 드라이런용 대역
            비목 = 비목_ or (후보[0]["비목"] if 후보 else None)
            hit = 금지적중(cur, 질문, None, 사업명, 비목)
            eff = effective_rule(cur, 사업명, 비목) if 비목 else None
            if hit:
                카운트["금지적중"] += 1
            if eff:
                카운트["룰있음"] += 1
            elif 사업명 is None:
                카운트["룰없음(공통)"] += 1
            else:
                카운트["룰없음(사업지정)"] += 1
            행.append({"gold_id": gold_id, "세트": 세트, "사업명": 사업명, "정답": 정답,
                       "비목": 비목, "비목출처": (후보[0]["출처"] if 후보 else None),
                       "금지적중": bool(hit),
                       "금지예시": hit["예시"] if hit else None,
                       "룰": bool(eff), "허용": eff["허용"] if eff else None,
                       "적용층": eff["적용층"] if eff else None,
                       "B4문장": eff["B4문장"] if eff else None})
    out = ROOT / "scripts" / "_work" / "_B_골든셋_룰커버.json"
    out.write_text(json.dumps({"카운트": 카운트, "행": 행}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(카운트, ensure_ascii=False))
    print(f"→ {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--golden", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="실 커넥션 스모크 (타입·예외)")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.calibrate:
        return _calibrate()
    if a.audit:
        return _audit()
    if a.smoke:
        return _smoke()
    if a.golden:
        return _golden()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
