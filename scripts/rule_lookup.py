# -*- coding: utf-8 -*-
"""룰 조회: 금지목록 즉시 판정(게이트 A), 비목 확정, 효력 결정(L1·L2·L3 병합), L3 게이팅.

판정 파이프라인(orchestrate) 2단계가 금지적중·비목확정·effective_rule·l3_게이팅 을, 서버 기동
워밍업이 warmup 을 호출한다. corpus.programs·rules·precedence_rules·item_alias·item_vocab 을 읽기만 한다.
판정은 만들지 않는다. 룰이 없으면 None, DB 오류는 예외로 올려 판정 파이프라인이 판단불가로 반환한다.
금지목록 적중은 정규화(NFKC·공백·구두점·조사) 뒤 문자 그대로의 일치·포함만 인정한다.
자기검사·스모크·감사는 scripts/rule_lookup_check.py 에 있다.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Sequence

허용_강도 = {"불가": 3, "조건부": 2, "가능": 1}   # 클수록 엄격


# 정규화: 조사·공백·구두점·괄호만 다룬다. 의미 해석은 하지 않는다

# 후치 조사. 한 글자 명사가 잘리지 않도록 어간이 2자 이상 남을 때만 뗀다.
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
    """어간이 2자 이상 남을 때만, 더 뗄 것이 없을 때까지 반복해서 뗀다.

    한 번만 떼면 '손잡이를'과 '손잡이'의 정규형이 달라진다. 양쪽 모두 끝까지 떼야 같아진다.
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


# 금지예시 해부: 본문(괄호 밖)과 예외 단서

# 괄호 안이 이 항목을 조건부로 바꾸는 단서인 경우.
#   예외로 보는 것: 예외 / 제외 / 집행 제한 / "…비로 집행|구매해야"(비목 재분류)
#   예외가 아닌 것: "대행 수수료는 가능"(다른 대상을 가리킨다. 성공보수 자체는 불가)
#                   "현물로만 계상 가능"(현물은 지출이 아니다. 지출로는 불가)
#                   "…등", "전담조직", "선집행액 환수 대상"(서술일 뿐이다)
_예외단서 = re.compile(r"(예외|제외|집행\s*제한)")
_비목재분류 = re.compile(r"[가-힣]+비로\s*(집행|구매)")


def 금지예시_해부(예시: str) -> dict[str, Any]:
    """금지예시 한 줄 → {본문, 본문_정규형, 예외단서, 무조건}.

    괄호는 부연('법인은 …로 등재된 법인')일 수도, 예외 단서('사전승인 시 예외')일 수도 있다.
    예외 단서가 붙은 항목은 무조건=False 이고 게이트 A 의 즉시 불가 대상이 아니다.
    """
    원문 = _nfkc(예시)
    단서 = [m for m in _괄호.findall(원문)
            if _예외단서.search(m) or _비목재분류.search(m)]
    본문 = _괄호.sub(" ", 원문).strip()
    return {
        "본문": 본문,
        "본문_정규형": _norm(본문),
        "예외단서": 단서 or None,
        "무조건": not 단서,
    }


# 룰 행 조회: base 는 L1(사업명 NULL, 창업 계통만) ∪ L2(사업명 일치)

_룰컬럼 = """rule_id, layer, 기관id, 사업명, 비목, 허용, 사전승인, 사전승인_조건,
             한도_유형, 한도_값, 한도_단위, 증빙, 금지예시, 허용예시, 근거,
             출처도메인, verified"""


def _조건_리스트화(v) -> list[str]:
    """사전승인_조건 을 항상 list[str] 로 맞춘다. DB 가 str 을 주든 list 를 주든 받는다.

    문자열을 list() 로 감싸면 글자 단위로 쪼개지므로 따로 처리한다.
    """
    if isinstance(v, str):
        return [v] if v.strip() else []
    return list(v or [])


def _행(cur, sql: str, args: Sequence[Any]) -> list[dict]:
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["한도_값"] = float(d["한도_값"]) if d.get("한도_값") is not None else None
        for k in ("증빙", "금지예시", "허용예시"):
            d[k] = list(d.get(k) or [])
        d["사전승인_조건"] = _조건_리스트화(d.get("사전승인_조건"))
        근거 = d.get("근거")
        d["근거"] = json.loads(근거) if isinstance(근거, str) else (근거 or [])
        out.append(d)
    return out


def _사업정보(cur, 사업명: str | None) -> dict | None:
    """corpus.programs 에서 사업의 비목계통과 우선규범을 한 번에 읽는다. → {"계통", "우선규범"} | None

    사업명이 없으면(공통 문항) 창업 계통으로 본다. 사업명이 있는데 행이 없거나 비목계통이 비어
    있으면 None 이다. 모르는 사업을 창업 계통으로 가정하면 R&D 계통 질문에 창업 계통 지침이 근거로
    붙는다. DB 오류는 삼키지 않는다(판정 파이프라인이 판단불가로 반환한다).
    """
    if not 사업명:
        return {"계통": "창업", "우선규범": None}
    cur.execute("SELECT 비목계통, 우선규범 FROM corpus.programs WHERE 사업명 = %s", [사업명])
    r = cur.fetchone()
    if not r or not r[0]:
        return None
    return {"계통": r[0], "우선규범": r[1]}


def base_룰(cur, 사업명: str | None, 비목: str | None, 계통: str | None) -> list[dict]:
    """L1·L2 룰 행. 계통 은 _사업정보()["계통"] 이고, None(모르는 사업)이면 조회 없이 빈 리스트다.

    조건이 `layer IN ('L1','L2') AND 사업명=?` 가 아닌 이유:
    - L1 행은 사업명이 NULL 이다. `NULL = ?` 는 항상 거짓이라 L1 을 찾지 못한다.
    - L1 은 창업 계통 사업에만 붙인다. TIPS 는 상위 근거법이 다르다(「중소기업창업 지원사업
      통합관리지침」이 아니라 「중소기업기술혁신 촉진법」 등). 계통 조건이 없으면 창업 지침이
      TIPS 질문의 근거로 붙어 오답이 된다.
    """
    if 계통 is None:
        return []
    where = ["((layer='L1' AND 사업명 IS NULL AND %s) OR (layer='L2' AND 사업명 = %s))"]
    args: list[Any] = [계통 == "창업", 사업명]
    if 비목:
        where.append("비목 = %s")
        args.append(비목)
    return _행(cur, f"SELECT {_룰컬럼} FROM corpus.rules WHERE " + " AND ".join(where)
                    + " ORDER BY layer, rule_id", args)


def _l3정규화(l3: dict | None) -> dict | None:
    """l3_load.l3룰() 결과를 병합에 넣을 룰 행 모양으로 맞춘다.

    L3 룰은 corpus.rules 가 아니라 tenant.l3_articles 에서 조립되므로 rule_id 가 없다.
    인용 식별자는 근거의 article_id 다.
    """
    if l3 is None:
        return None
    article_id = next((g.get("article_id") for g in (l3.get("근거") or []) if g.get("article_id")),
                      None)
    return {
        "rule_id": l3.get("rule_id"),
        "article_id": article_id,
        "layer": "L3",
        "허용": l3.get("허용"),
        "사전승인": bool(l3.get("사전승인")),
        "사전승인_조건": _조건_리스트화(l3.get("사전승인_조건")),
        "한도_유형": l3.get("한도_유형"),
        "한도_값": float(l3["한도_값"]) if l3.get("한도_값") is not None else None,
        "한도_단위": l3.get("한도_단위"),
        "증빙": list(l3.get("증빙") or []),
        "금지예시": list(l3.get("금지예시") or []),
        "허용예시": list(l3.get("허용예시") or []),
        "verified": bool(l3.get("verified")),
    }


def _precedence(cur, 사업명: str | None) -> list[dict]:
    """corpus.precedence_rules 의 이 사업 행. 사업명이 없으면 빈 리스트."""
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


def _우선규범(정보: dict, 우선순위: Sequence[dict]) -> str | None:
    """사업의 상위 규범 이름. corpus.programs.우선규범 이 기준이고, 없으면 precedence_rules 값을 쓴다.

    초격차·모두의 창업처럼 상위 규범이 통합관리지침이 아닌 사업이 있다.
    """
    if 정보.get("우선규범"):
        return 정보["우선규범"]
    return next((p["우선규범"] for p in 우선순위 if p.get("우선규범")), None)


def _L2우선L3(우선순위: Sequence[dict]) -> bool:
    """사업 세부관리기준이 주관기관 규정보다 우선한다고 범위 'all' 로 정했는가."""
    return any(p["우선계층"] == "L2" and p["열위계층"] == "L3" and p["범위"] == "all"
               for p in 우선순위)


# 게이트 A: 금지목록 즉시 불가

_최소매칭길이 = 4          # 정규형 기준. '기프티콘'(4자)은 통과, 2~3자 조각은 막는다


def 금지후보(cur, 품목: str | None, 용도: str | None, 사업명: str | None,
             비목: str | None) -> dict[str, list[dict]]:
    """품목·용도에 걸리는 금지예시. → {"무조건": [...], "조건부": [...]}

    무조건 은 게이트 A 의 즉시 불가 대상이다. 조건부(예외 단서가 붙은 항목)는 감사용이다
    (rule_lookup_check --audit). 각 목록은 정확일치 먼저, 그다음 긴 매칭 순이다.
    """
    입력n = _norm(f"{품목 or ''} {용도 or ''}")
    품목n = _norm(품목 or "")
    결과: dict[str, list[dict]] = {"무조건": [], "조건부": []}
    if not 입력n:
        return 결과

    정보 = _사업정보(cur, 사업명)
    for r in base_룰(cur, 사업명, 비목, 정보["계통"] if 정보 else None):
        for 예시 in r["금지예시"]:
            h = 금지예시_해부(예시)
            본문n = h["본문_정규형"]
            if len(본문n) < _최소매칭길이:
                continue
            if 품목n and 품목n == 본문n:
                방식 = "정확일치"
            elif 본문n in 입력n:
                방식 = "구절포함"
            else:
                continue
            항목 = {
                "예시": h["본문"],
                "예시_원문": _nfkc(예시),
                "근거": r["근거"],
                "rule_id": r["rule_id"],
                "비목": r["비목"],
                "사업명": r["사업명"],
                "layer": r["layer"],
                "verified": r["verified"],
                "매칭": 방식,
                "매칭문자열": 본문n,
                "예외단서": h["예외단서"],
            }
            결과["무조건" if h["무조건"] else "조건부"].append(항목)

    for k in 결과:
        결과[k].sort(key=lambda x: (x["매칭"] != "정확일치", -len(x["매칭문자열"])))
    return 결과


def 금지적중(cur, 품목: str | None, 용도: str | None, 사업명: str | None,
             비목: str | None) -> dict | None:
    """게이트 A. 적중하면 판정 파이프라인이 LLM 호출 없이 "불가"를 반환한다.

    정확일치(정규화 후 품목 == 금지예시 본문) 또는 구절포함(금지예시 본문이 품목+용도 안에 문자
    그대로 들어 있음)만 인정한다. 유사 매칭은 하지 않는다. 오탐이 곧 근거 달린 틀린 "불가"가 된다.
    예외 단서가 붙은 금지예시('귀금속·보석·원석(사전승인 시 예외)')는 적중으로 보지 않는다.
    조건부 사안이므로 effective_rule() 과 LLM 판정 경로로 간다.

    반환: 가장 앞선 무조건 후보 {"예시", "예시_원문", "근거", "rule_id", "비목", "사업명", "layer",
    "verified", "매칭", "매칭문자열", "예외단서"} | None. 응답의 금지근거로 그대로 나간다.
    """
    후보 = 금지후보(cur, 품목, 용도, 사업명, 비목)
    return 후보["무조건"][0] if 후보["무조건"] else None


# 비목 확정

_모델 = None


def warmup() -> None:
    """비목확정 벡터 조회에 쓰는 KURE-v1 을 미리 올린다. 서버 기동 워밍업이 호출한다.

    retrieve.모델() 과 같은 인스턴스를 쓴다. 가중치가 같아 벡터가 동일하므로 따로 올리면 로딩
    시간과 메모리만 두 배가 된다. 모델을 올리지 못하면 예외가 그대로 올라간다.
    """
    global _모델
    if _모델 is None:
        import retrieve                                         # noqa: PLC0415
        _모델 = retrieve.모델()


def 임베딩(texts: Sequence[str]):
    warmup()
    return _모델.encode(list(texts), normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=False)


def _vecstr(v) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


def 비목확정(cur, 품목: str | None, 사업명: str | None, *,
             임계치: float = 0.75, top_n: int = 3,
             벡터허용: bool = True) -> list[dict]:
    """품목 → 비목 후보 [{"비목", "신뢰도", "출처": "alias"|"vector", "매칭", "별칭사업명", "별칭출처"}].

    신뢰도 내림차순, 최대 top_n 개. 순서대로 본다.
    1) corpus.item_alias 정확 조회(정규형 일치). 사업 전용 별칭(1.0)이 공통 별칭(0.95)보다 우선한다.
    2) 없으면 corpus.item_vocab 의 비목명·별칭·하위항목 자체와의 일치.
    3) 그래도 없고 벡터허용 이면 별칭 임베딩 최근접 조회. 유사도가 임계치 미만이면 버린다.

    임계치 0.75 의 근거: item_alias 임베딩끼리 leave-one-out 최근접 이웃의 비목이 같은 비율이
    0.75 이상에서 약 97%, 0.70 이상 약 94%, 0.60 이상 약 86% 다. 임계치 아래의 오분류는 의미가
    아니라 2~4자 한국어 명사의 표면형 유사다(예: 해외 출원→해외 출장, 텀블러→태블릿).
    틀린 비목은 없는 비목보다 나쁘다. effective_rule() 이 엉뚱한 룰 행과 조문을 인용하게 된다.
    """
    품목n = _norm(품목 or "")
    if not 품목n:
        return []
    out: list[dict] = []
    처리한_비목: set[str] = set()

    # 정확 조회
    cur.execute("SELECT 상품명, 비목, 사업명, 출처 FROM corpus.item_alias "
                "WHERE 사업명 = %s OR 사업명 IS NULL", [사업명])
    for 상품명, 비목, 별칭사업, 출처 in cur.fetchall():
        if _norm(상품명) != 품목n:
            continue
        신뢰도 = 1.0 if 별칭사업 else 0.95
        if 비목 not in 처리한_비목 or 신뢰도 > next(o["신뢰도"] for o in out if o["비목"] == 비목):
            out = [o for o in out if o["비목"] != 비목]
            out.append({"비목": 비목, "신뢰도": 신뢰도, "출처": "alias",
                        "매칭": 상품명, "별칭사업명": 별칭사업, "별칭출처": 출처})
            처리한_비목.add(비목)

    # 비목명·별칭·하위항목 자체와의 일치 (corpus.item_vocab)
    if not out:
        for 비목, 신뢰도 in _용어사전_직결(cur, 품목n):
            if 비목 not in 처리한_비목:
                out.append({"비목": 비목, "신뢰도": 신뢰도, "출처": "alias",
                            "매칭": 품목, "별칭사업명": None, "별칭출처": "item_vocab"})
                처리한_비목.add(비목)

    if out or not 벡터허용:
        return sorted(out, key=lambda o: -o["신뢰도"])[:top_n]

    # 벡터
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
        if sim < 임계치 or 비목 in 처리한_비목:
            continue
        out.append({"비목": 비목, "신뢰도": round(sim, 4), "출처": "vector",
                    "매칭": 상품명, "별칭사업명": None, "별칭출처": None})
        처리한_비목.add(비목)
    return sorted(out, key=lambda o: -o["신뢰도"])[:top_n]


def _용어사전_직결(cur, 품목n: str) -> list[tuple[str, float]]:
    """품목이 창업 계통 비목명·별칭·하위항목 그 자체일 때(사용자가 비목명을 그대로 쓴 경우)의 후보."""
    cur.execute("SELECT 비목, 별칭, 하위항목 FROM corpus.item_vocab WHERE 계통='창업'")
    hits = []
    for 비목, 별칭, 하위 in cur.fetchall():
        if _norm(비목) == 품목n:
            hits.append((비목, 1.0))
        elif any(_norm(a) == 품목n for a in (별칭 or [])):
            hits.append((비목, 0.98))
        elif any(_norm(a) == 품목n for a in (하위 or [])):
            hits.append((비목, 0.96))
    return hits


# 금액 비교

_한정 = re.compile(r"([가-힣A-Za-z·]+)\s*한정")


def _괄호밖(s: str) -> str:
    """괄호 밖 문자열만. 중첩 괄호를 깊이로 세어 전부 걷어낸다."""
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
    """괄호 안 문자열 전부(중첩 포함). _괄호밖 의 여집합."""
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
    """'원/인/일(멘토링비 한정, 시간당 …)' → {기본:'원', 분모:['인','일'], 한정:'멘토링비', 부연}."""
    if not 단위:
        return {"기본": None, "분모": [], "한정": None, "부연": None}
    원문 = _nfkc(단위)
    부연 = _괄호안(원문) or None
    # 괄호가 중첩된다. 재도전 지급수수료가 실제로 이렇다:
    #   "원/인/일(멘토링비 한정 … (예비)재창업자별 … 총액 500만원(사업기간 중 합산) …)"
    # 비중첩 정규식(_괄호)으로 지우면 안쪽 (예비) 에서 끊겨 뒤의 산문이 남고, 그것이 '/' 분해에
    # 섞여 분모가 "일재창업자별 멘토링비 총액 500만원 …" 이 된다. 그래서 깊이를 세어 지운다.
    단위본문 = _괄호밖(원문)
    조각 = [p.strip() for p in 단위본문.split("/") if p.strip()]
    m = _한정.search(부연 or "")
    return {"기본": 조각[0] if 조각 else None, "분모": 조각[1:],
            "한정": m.group(1) if m else None, "부연": 부연}


_분모키 = {"인": "인원", "일": "일수", "월": "월수", "박": "박수", "건": "건수", "대": "인원"}

# 한 행의 한도_값 은 스칼라 하나인데, 실제 조문은 한 비목에 한도를 여럿 건다. 나머지 한도는
# 한도_단위 안에 산문으로 들어 있어 구조화 비교로는 잡히지 않는다. 예:
#   지급수수료  30만/인/일  + "별도 한도 2건 — 총액 500만원 · 세무회계 월 20만원"
#   여비        10만/인/박  + "광역시 8만원, 그 밖의 지역 7만원"   (더 낮다)
#   지급수수료               + "시간당 10만원 초과 불가"
# 이를 모르고 "한도 이내"라고 답하면 더 엄한 한도를 놓친 틀린 "가능"이 된다. 그래서 산문에
# 추가 한도 신호가 있으면 초과=False 를 None 으로 낮춘다. 초과=True 는 그대로 둔다
# (이미 넘었다는 결론은 다른 한도가 있어도 뒤집히지 않는다).
_분모어 = re.compile(r"(계약총액|전체\s*용역비|선급금|용역비)")
_추가한도 = re.compile(r"(초과\s*불가|별도\s*한도|광역시|그\s*밖의\s*지역|총액\s*[0-9]|"
                       r"[0-9][0-9만천,]*\s*원\s*(이내|한도|까지))")
# "12만5천원 초과 시 기타소득세 공제" 는 한도가 아니라 원천징수 규칙이라 신호가 아니다
_세금 = re.compile(r"초과\s*시.{0,20}(공제|소득세)")


_신호말 = [("초과 불가", "별도의 상한"), ("별도 한도", "별도의 한도"),
           ("광역시", "지역별로 다른 한도"), ("그 밖의 지역", "지역별로 다른 한도"),
           ("총액", "총액 한도"), ("이내", "별도의 상한"), ("한도", "별도의 한도"),
           ("까지", "별도의 상한")]


def 미파싱한도(단위: str | None) -> list[str]:
    """한도_단위 산문 안에 있는 추가 한도의 신호. 없으면 빈 리스트.

    반환값은 사용자에게 보이는 B4문장에 들어가므로, 정규식이 잡은 조각("총액 5")이 아니라
    읽을 수 있는 말("총액 한도")로 바꿔서 반환한다.
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


def _비교_비율(한도: float, 수치: dict, u: dict) -> dict:
    분자 = 수치.get("선급금") or 수치.get("금액")
    분모 = 수치.get("계약총액") or 수치.get("협약총액")
    if 분자 is None or 분모 in (None, 0):
        부족 = "계약총액" if 분자 is not None else "선급금과 계약총액"
        return {"초과": None, "사유": f"비율 비교에 필요한 {부족} 이 입력되지 않았다"}
    비율 = float(분자) / float(분모) * 100
    return {"초과": 비율 > 한도, "사유": "선급 비율이 한도 비율을 "
            + ("초과한다" if 비율 > 한도 else "넘지 않는다")}


def _비교_개수(한도: float, 수치: dict, u: dict) -> dict:
    수량 = 수치.get("수량")
    if 수량 is None:
        return {"초과": None, "사유": "수량이 입력되지 않았다"}
    인원 = 수치.get("인원")
    if "인" in u["분모"] and 인원 is None:
        return {"초과": None, "사유": "1인당 한도인데 인원이 입력되지 않았다"}
    허용수 = 한도 * float(인원 if 인원 is not None else 1)
    return {"초과": float(수량) > 허용수,
            "사유": ("허용 대수를 초과한다" if float(수량) > 허용수 else "허용 대수 이내다")}


def _비교_금액(한도: float, 수치: dict, u: dict) -> dict:
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


_유형별_비교 = {"비율": _비교_비율, "개수": _비교_개수, "금액": _비교_금액}


def 금액비교(rule: dict, 수치: dict | None, 하위항목: str | None = None) -> dict | None:
    """룰 행 하나의 한도와 입력 수치를 비교한다. → {"초과": bool | None, "사유": str} | None

    한도가 없으면 None. 비교에 필요한 값(금액·인원·일수·계약총액 등)이 없으면 추측하지 않고
    초과=None 과 부족한 값을 사유로 반환한다. 다른 하위항목에 한정된 한도면 None 이다.
    """
    유형 = rule.get("한도_유형")
    if not 유형 or rule.get("한도_값") is None:
        return None
    수치 = dict(수치 or {})
    한도 = float(rule["한도_값"])
    u = _단위해부(rule.get("한도_단위"))

    # 하위항목 한정: '멘토링비 한정' 한도를 사무실임차료에 적용하면 틀린 초과가 된다
    if u["한정"]:
        if 하위항목 is None:
            return {"초과": None,
                    "사유": f"이 한도는 {u['한정']} 에만 걸리는데 하위항목이 확정되지 않았다"}
        if _norm(하위항목) != _norm(u["한정"]):
            return None

    비교 = _유형별_비교.get(유형)
    if 비교 is None:
        return {"초과": None, "사유": f"알 수 없는 한도 유형 '{유형}'"}
    return 비교(한도, 수치, u)


def _한도전수(한도목록: Sequence[dict], 수치: dict | None,
              하위항목: str | None = None) -> dict | None:
    """한도가 여럿이면 모두 비교해 결과 하나로 합친다. 하나라도 넘으면 초과다.

    단위가 다른 한도는 서로 다른 제약이라 min 으로 합칠 수 없다. 「1인 1대」와 「월 50만원」은
    둘 다 지켜야 한다. 하나라도 초과면 초과, 초과는 없는데 비교하지 못한 것이 있으면 초과=None
    (전제로 넘긴다), 전부 이내여야 이내다.
    """
    결과 = [x for x in (금액비교(r, 수치, 하위항목) for r in 한도목록) if x is not None]
    if not 결과:
        return None
    넘음 = [x for x in 결과 if x["초과"] is True]
    if 넘음:
        # 이미 넘었다는 결론은 다른 한도가 더 있어도 뒤집히지 않는다
        return {"초과": True, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 넘음))}
    미상 = [x for x in 결과 if x["초과"] is None]
    if 미상:
        return {"초과": None, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 미상))}

    # "한도 이내"라고 말하기 전에 산문에 추가 한도가 있는지 본다. 있으면 이내라고 단정하지 않는다.
    숨은 = sorted({s for r in 한도목록 for s in 미파싱한도(r.get("한도_단위"))})
    if 숨은:
        return {"초과": None,
                "사유": "구조화된 한도는 넘지 않으나, 같은 조문에 코드가 못 읽는 추가 한도가 "
                        f"있다({'·'.join(숨은)}) — 이내라고 단정할 수 없다"}
    return {"초과": False, "사유": " · ".join(dict.fromkeys(x["사유"] for x in 결과))}


# B4 문장

# 프롬프트 토큰 한도를 넘지 않도록 예시 개수를 제한한다. 어느 것을 자를지는 판단하지 않고
# DB 가 준 순서대로 앞에서부터 쓴다.
_예시_최대개수 = 8


def _예시_문장(라벨: str, 목록: Sequence[str] | None, 최대: int = _예시_최대개수,
             *, 조건부보존: bool = False) -> str | None:
    """"{라벨}: 항목1 · 항목2 · … 외 N종." 최대 개수를 넘으면 자르고 남은 개수를 적는다.

    조건부보존=True(금지예시 전용)면 무조건 항목만 자른다. 무조건 금지는 금지적중() 이 DB 배열을
    직접 검사하므로 프롬프트에서 잘려도 게이트 A 가 잡는다. 예외 단서가 붙은 조건부 항목은 게이트
    A 가 잡지 않으므로, 잘리면 LLM 이 "제약 없음"으로 읽는다. 그래서 전부 넣는다. "외 N종" 은
    무조건 항목만 센다. 허용예시에는 조건부/무조건 판별 기준이 없어 이 옵션을 쓰지 않는다.
    """
    if not 목록:
        return None
    if not 조건부보존:
        보임 = list(목록[:최대])
        남음 = len(목록) - len(보임)
    else:
        분류 = [(x, 금지예시_해부(x)["무조건"]) for x in 목록]
        무조건 = [x for x, 무 in 분류 if 무]
        남음 = max(0, len(무조건) - 최대)              # 잘리는 것은 무조건 항목뿐이다
        살린무조건 = 무조건[:최대]
        보임 = [x for x, 무 in 분류 if not 무 or x in 살린무조건]   # 원래 순서 유지
    문장 = f"{라벨}: " + " · ".join(보임)
    if 남음 > 0:
        문장 += f" 외 {남음}종"
    return 문장 + "."


def _실체없음(금지예시: Sequence[str] | None, 허용예시: Sequence[str] | None,
             비교: dict | None, 사전승인: bool, 증빙: Sequence[str]) -> bool:
    """이 비목의 룰은 있으나 이 지출을 구체적으로 다루는 내용이 없는가.

    병합이 끝난 뒤(L1+L2+L3 를 합친 뒤) 판단한다. 병합 전에는 내용이 없던 두 층이 합쳐지며
    내용이 생길 수 있어서다. 품목 단위 매칭은 하지 않는다(그건 금지적중() 이 한다). 병합 결과에
    금지예시·허용예시·비교 가능한 한도·사전승인·증빙 중 하나라도 있는지만 본다.
    """
    return not (금지예시 or 허용예시 or (비교 is not None) or 사전승인 or 증빙)


# 적용층 값 → B4문장의 규범 이름. 위임 관계라 두 층이 함께 구속하는 것이 정상이다.
_층_설명 = {"L1": "국가 통합관리지침", "L2": "사업 세부관리기준", "L3": "주관기관 내부규정",
           "L1+L2": "국가 통합관리지침과 사업 세부관리기준",
           "L1+L3": "국가 통합관리지침과 주관기관 내부규정",
           "L2+L3": "사업 세부관리기준과 주관기관 내부규정",
           "L1+L2+L3": "국가 통합관리지침·사업 세부관리기준·주관기관 내부규정"}


def B4문장(비목: str | None, eff: dict, 비교: dict | None, 우선규범: str | None,
           참고_L3: dict | None = None) -> str:
    """프롬프트 룰 요약 블록(B4) 문장. 원시 한도값은 넣지 않고 비교 결과만 말한다.

    "한도 30만원"을 그대로 넣으면 1인·1일 기준을 떼고 총액으로 읽는 식으로 잘못 적용된다.
    비교는 코드가 끝내고 문장은 그 결과만 전한다.

    허용 여부는 세 경우로 나눠 말한다.
        룰에 허용값이 없다              -> "명시되어 있지 않다"
        있고 이 비목을 구체적으로 다룬다  -> 그 내용(금지예시·허용예시)을 넣는다
        있는데 구체적인 내용이 없다       -> "조건을 붙여 허용한다" 대신 그 사실을 말한다
    세 번째를 따로 두지 않으면 룰 행이 있다는 이유만으로 무슨 조건인지 없는 확정 문장이 나간다.

    eff 는 _엄격병합()/_오버레이() 결과다. 참고_L3 는 base 와 L3 가 모두 있을 때 주어진다
    ({"모드", "L3더엄격", "L3반영"}, _l3_반영 참고). 기관 규정 문장은 코드가 정한 효력 결과만 말한다.
        모드 '사업우선'  세부관리기준이 L2 > L3 를 범위 all 로 정했다. L3 는 반영하지 않았다
        모드 '엄격병합'  그런 조항이 없다. L3 를 엄격한 쪽으로 합쳤고, 반영한 것이 없으면 말하지 않는다
    """
    비목 = 비목 or "해당 비목"
    허용 = eff.get("허용")
    사전승인 = eff.get("사전승인")
    증빙 = eff.get("증빙") or []
    금지예시, 허용예시 = eff.get("금지예시"), eff.get("허용예시")
    사전승인_조건 = " ".join(eff.get("사전승인_조건") or [])
    적용층 = eff.get("적용층")

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
    # 금지예시·허용예시는 개수가 아니라 내용을 넣어야 LLM 이 구체적으로 쓴다.
    # 금지예시만 조건부보존 이다. 예외 단서가 붙은 항목은 게이트 A 가 잡지 않기 때문이다.
    금지문 = _예시_문장("금지 예시", 금지예시, 조건부보존=True)
    if 금지문:
        조각.append(금지문)
    허용문 = _예시_문장("허용 예시", 허용예시)
    if 허용문:
        조각.append(허용문)
    if 적용층:
        조각.append(f"적용된 규범은 {_층_설명.get(적용층, 적용층)}이다.")
    if 우선규범:
        조각.append(f"이 사업의 상위 규범은 「{우선규범}」이다.")
    if 참고_L3:
        안내 = _L3_안내(참고_L3)
        if 안내:
            조각.append(안내)
    return " ".join(조각)


def _L3_안내(참고_L3: dict) -> str | None:
    """B4문장 의 기관 규정 문장. 실제 병합 결과와 다른 말을 하지 않는다.

    엄격병합 모드에서 "본 사업 관리기준이 우선한다" 고 쓰면, L3 의 불가가 이미 반영된 결과를 두고
    LLM 이 더 관대한 사업 기준을 따르게 된다.
    """
    if 참고_L3.get("모드") == "엄격병합":
        if 참고_L3.get("L3더엄격"):
            return "귀 기관 규정이 더 엄격해 그 기준을 함께 적용했다."
        if 참고_L3.get("L3반영"):
            return "귀 기관 규정의 요건도 함께 적용했다."
        return None
    return ("귀 기관 규정은 더 엄격하나, 본 사업 관리기준이 우선한다." if 참고_L3.get("L3더엄격")
            else "귀 기관 규정에도 관련 조항이 있으나, 본 사업 관리기준이 우선한다.")


# 효력 결정: 병합 규칙

def _문장분할(원문: str | None) -> list[str]:
    """마침표 뒤 공백에서 문장을 나눈다. 빈 조각은 버린다."""
    return [문.strip() for 문 in re.split(r"(?<=\.)\s+", 원문 or "") if 문.strip()]


def _문장키(문장: str) -> str:
    """문장 중복 판정 키. 공백을 모두 지우고 NFKC 로 맞춘 뒤 끝의 마침표를 뗀다.

    병합 결과의 문장에는 마침표를 붙이므로(_조건합치기) 마침표 유무로 같은 문장을 다르게 보면 안 된다.
    """
    return _nfkc(re.sub(r"\s+", "", 문장)).rstrip(".")


def _조건합치기(rows: Sequence[dict]) -> tuple[list[str], list[str]]:
    """여러 층의 사전승인_조건 을 문장 단위로 합친다. → (문장 목록, 새 문장을 보탠 층)

    배열 원소 하나에 문장이 여럿 들어 있을 수 있어(예: "2천만원 초과 시 사전심의. 부적정 1개
    이상이면 불가.") 원소가 아니라 문장 단위로 중복을 뺀다. 순서는 rows 순서(L1 → L2 → L3)다.
    마침표로 끝나지 않는 문장(예: "…제출하여야 한다(제38조④)")에는 마침표를 붙인다. 그대로
    이으면 다음 층의 문장과 한 문장처럼 읽혀 층 경계가 보이지 않는다.
    """
    문장들: list[str] = []
    처리한_키: set[str] = set()
    층: list[str] = []
    for r in rows:
        보탬 = False
        for 원소 in r.get("사전승인_조건") or []:
            for 문 in _문장분할(원소):
                키 = _문장키(문)
                if 키 in 처리한_키:
                    continue
                처리한_키.add(키)
                문장들.append(문 if 문.endswith(".") else 문 + ".")
                보탬 = True
        if 보탬 and r["layer"] not in 층:
            층.append(r["layer"])
    return 문장들, 층


def _한도키(r: dict) -> str:
    """같은 종류의 한도인지 가르는 키. 유형과 단위가 같아야 min 으로 비교할 수 있다."""
    return f'{r.get("한도_유형")}|{_nfkc(r.get("한도_단위") or "")}'


def _한도목록_병합(한도목록: list[dict], r: dict) -> None:
    """r 의 한도를 한도목록에 넣는다(제자리 수정). 같은 키가 있으면 더 낮은 쪽만 남긴다.

    단위가 다른 한도는 서로 다른 제약이라 둘 다 남긴다. 한도가 없는 행은 무시한다.
    """
    if r.get("한도_값") is None:
        return
    키 = _한도키(r)
    for i, 기존 in enumerate(한도목록):
        if _한도키(기존) == 키:
            if r["한도_값"] < 기존["한도_값"]:
                한도목록[i] = r
            return
    한도목록.append(r)


def _합집합(rows: Sequence[dict], 키: str) -> tuple[list, list[str]]:
    """rows 의 목록형 필드를 순서를 지켜 중복 없이 합친다. → (값, 새 값을 보탠 층)

    layer 키가 없는 dict(병합 결과)는 층 집계에서 빠진다.
    """
    값: list = []
    층: list[str] = []
    for r in rows:
        for e in r.get(키) or []:
            if e not in 값:
                값.append(e)
                if r.get("layer") and r["layer"] not in 층:
                    층.append(r["layer"])
    return 값, 층


def _엄격병합(rows: Sequence[dict], *, 허용층우선: str | None = None) -> dict:
    """룰 행들을 필드별로 병합한다. 상위 층 행으로 전체 대체하지 않는다.

    L1 과 L2 는 충돌이 아니라 위임 관계다. 지침 제36조②가 "제1항에서 명시되지 않은 증빙서류는
    …사업별 세부관리기준에 따라"라고 직접 위임한다. L2 는 L1 을 뒤집지 않고 구체화하므로 필드마다
    따로 고른다.

        허용            엄격한 쪽(불가 > 조건부 > 가능). 허용층우선 이 주어지면 그 층의 값
        한도            같은 유형·단위끼리 min, 다르면 모두 남긴다(한도목록)
        사전승인        OR
        사전승인_조건    문장 단위 합집합(문장 목록으로 두고 B4문장 이 이어 붙인다)
        증빙·금지예시·허용예시  층 무관 합집합
        적용층          값을 실제로 준 층. 여럿이면 'L1+L2'

    한도가 NULL 로 덮이면 무제한 허용이 되어 틀린 "가능"이 된다. L1(한도 NULL)이 L2(1대/인)를
    가리면 "PC 몇 대까지" 문항에 한도가 붙지 않는다.
    """
    강도 = lambda a: 허용_강도.get(a, 0)
    허용원 = ([r for r in rows if r["layer"] == 허용층우선] if 허용층우선 else []) or list(rows)
    허용 = max((r["허용"] for r in 허용원), key=강도)
    허용층 = [r["layer"] for r in 허용원 if r["허용"] == 허용]

    한도목록: list[dict] = []
    for r in rows:
        _한도목록_병합(한도목록, r)

    사전승인_조건, 조건층 = _조건합치기(rows)
    증빙, 증빙층 = _합집합(rows, "증빙")
    금지예시, 금지층 = _합집합(rows, "금지예시")
    허용예시, 허용예시층 = _합집합(rows, "허용예시")
    승인층 = [r["layer"] for r in rows if r["사전승인"]]

    # 적용층은 구속력 있는 값을 실제로 준 층이다. 한도·허용뿐 아니라 증빙·금지예시·허용예시·
    # 사전승인·사전승인_조건을 준 층도 포함한다. 초격차 지급수수료처럼 한도가 없고 허용이 L1 으로
    # 정해져도 L2 가 금지예시를 추가하면 L2 도 구속한다.
    기여 = sorted({r["layer"] for r in 한도목록} | set(허용층) | set(증빙층)
                  | set(금지층) | set(승인층) | set(허용예시층) | set(조건층))
    return {
        "허용": 허용,
        "사전승인": any(r["사전승인"] for r in rows),
        "사전승인_조건": 사전승인_조건,
        "한도목록": 한도목록,
        # 가장 낮은 한도값(단위가 달라도 최솟값). 층 간 한도 비교는 한도목록 의 같은 _한도키 끼리 한다
        "한도_값": min(r["한도_값"] for r in 한도목록) if 한도목록 else None,
        "증빙": 증빙,
        "금지예시": 금지예시,
        "허용예시": 허용예시,
        "적용층": "+".join(기여),
        "적용층_기여": sorted({r["layer"] for r in rows}),
    }


def _오버레이(eff: dict, l3: dict) -> dict:
    """L2 > L3 우선 조항이 없는 사업에서 L3 를 base 병합 결과에 엄격한 쪽으로 합친다.

    규칙은 _엄격병합 과 같다(허용: 불가 > 조건부 > 가능, 한도: min, 사전승인: OR, 목록: 합집합).
    L2↔L3 만 "base 우선"으로 두면 L3 의 "불가"가 최종 "조건부"로 완화되어 틀린 "가능" 쪽으로
    기운다. unspecified_only(L1 > L2) 조항은 L1 과 L2 사이의 규칙이라 여기서는 쓰지 않는다.
    """
    강도 = lambda a: 허용_강도.get(a, 0)
    합 = dict(eff)
    L3_허용이_엄격 = 강도(l3.get("허용")) > 강도(eff.get("허용"))
    if L3_허용이_엄격:
        합["허용"] = l3["허용"]

    한도목록 = list(eff.get("한도목록") or [])
    _한도목록_병합(한도목록, l3)
    주 = min(한도목록, key=lambda r: r["한도_값"]) if 한도목록 else None
    합["한도목록"] = 한도목록
    합["한도_값"] = 주["한도_값"] if 주 else None

    합["사전승인"] = bool(eff.get("사전승인")) or bool(l3.get("사전승인"))
    # L3 조건은 추가 제약이라 base 문장 뒤에 붙인다(일반 → 특수). base 와 같은 문장은 빼고,
    # _조건합치기 와 같이 마침표로 끝나게 한다.
    처리한_키 = {_문장키(문) for 문 in eff.get("사전승인_조건") or []}
    추가조건: list[str] = []
    for 원소 in l3.get("사전승인_조건") or []:
        for 문 in _문장분할(원소):
            키 = _문장키(문)
            if 키 in 처리한_키:
                continue
            처리한_키.add(키)
            추가조건.append(문 if 문.endswith(".") else 문 + ".")
    합["사전승인_조건"] = list(eff.get("사전승인_조건") or []) + 추가조건
    for 키 in ("증빙", "금지예시", "허용예시"):
        합[키] = _합집합([eff, l3], 키)[0]

    # 사전승인 불리언은 base 와 같고 조건 문장만 보탠 경우도 L3 가 구속한 것으로 센다.
    # L3 한도가 한도목록에 남았으면(같은 종류보다 낮거나 단위가 달라 따로 남음) 최솟값이 아니어도 구속한다.
    l3_기여 = (L3_허용이_엄격 or any(r is l3 for r in 한도목록)
              or bool(l3.get("사전승인")) or bool(추가조건)
              or bool(l3.get("증빙")) or bool(l3.get("금지예시"))
              or bool(l3.get("허용예시")))
    if l3_기여:
        합["적용층"] = "+".join(sorted(set(eff["적용층"].split("+")) | {"L3"}))
        합["적용층_기여"] = sorted(set(eff["적용층_기여"]) | {"L3"})
    return 합


def _L3더엄격(l3: dict, eff: dict) -> bool:
    """L3 가 base 병합 결과보다 엄격한가. 허용이 더 엄격하거나, 허용이 같고 한도가 더 엄격하면 True.

    한도는 유형·단위가 같은 것끼리만 비교한다(1 대/인 과 300000 원은 비교할 수 없다). base 에 같은
    종류의 한도가 없으면 L3 한도는 새 제약이므로 더 엄격한 것으로 본다.
    """
    a, b = 허용_강도.get(l3.get("허용"), 0), 허용_강도.get(eff.get("허용"), 0)
    같은종류 = [r["한도_값"] for r in eff.get("한도목록") or [] if _한도키(r) == _한도키(l3)]
    낮은한도 = (l3.get("한도_값") is not None
                and (not 같은종류 or l3["한도_값"] < min(같은종류)))
    return a > b or (a == b and 낮은한도)


def _층병합(base: Sequence[dict], 우선순위: Sequence[dict]) -> tuple[list[dict], str | None]:
    """병합할 L1·L2 행과 허용을 정할 층. → (행, 허용층우선)

    unspecified_only("지침을 우선 적용하되, 지침에 명시되지 않은 사항만 본 기준") 조항이 정하는
    것은 허용과 우선규범뿐이다. 상위 층 행으로 전체 대체하면 초격차의 사업별 금지예시·증빙이
    사라진다. 금지예시·증빙·사전승인·한도는 두 층을 합쳐야 한다(L2 는 L1 을 구체화한다).
    그래서 행은 늘 둘 다 넘기고, 허용만 허용층우선 으로 L1 값을 쓰게 한다.
    """
    L1 = [r for r in base if r["layer"] == "L1"]
    L2 = [r for r in base if r["layer"] == "L2"]
    unspec = any(p["우선계층"] == "L1" and p["열위계층"] == "L2"
                 and p["범위"] == "unspecified_only" for p in 우선순위)
    return L1 + L2, ("L1" if unspec and L1 else None)


# 효력 결정: effective_rule

def _l3_선택(l3룰: dict | None, 계통: str | None) -> dict | None:
    """병합에 넣을 L3 룰. 창업 계통 사업에만 붙인다.

    L1 의 계통 조건과 이유가 다르다. L3 룰은 corpus.item_vocab(계통='창업') 비목으로 조립되는데,
    TIPS 의 비목 체계는 R&D 계통(연구활동비·연구수당·학생인건비·간접비…)이라 '인건비'처럼 이름이
    겹쳐도 뜻이 다르다. 모르는 사업(계통 None)에도 붙이지 않는다.
    """
    if l3룰 is None or 계통 != "창업":
        return None
    return _l3정규화(l3룰)


def _l3_반영(eff: dict | None, l3: dict | None,
            우선순위: Sequence[dict]) -> tuple[dict | None, dict | None]:
    """base 병합 결과에 L3 를 반영한다. → (최종 병합 결과 | None, 참고_L3 | None)

    - base 가 없으면 L3 단독으로 병합한다. 그 L3 에 허용값이 없으면(상위 조문을 가리키기만 하는
      참조만 조문) 따라갈 상위도 스스로 정한 것도 없으므로 룰이 없는 것(None)으로 본다.
      허용=None 을 내보내면 허용 enum(가능|조건부|불가) 밖의 값이 된다.
    - 사업 세부관리기준이 L2 > L3 를 범위 'all' 로 정하면 L3 는 반영하지 않는다(모드 '사업우선').
    - 그런 조항이 없으면(예: 초격차) 엄격한 쪽을 적용한다(_오버레이, 모드 '엄격병합').
    참고_L3 = {"모드", "L3더엄격", "L3반영"}. 엄격병합 모드의 L3더엄격 은 L3 가 허용이나 한도를 실제로
    더 엄격하게 바꿨는지, L3반영 은 L3 가 조금이라도 기여했는지(적용층에 L3 포함)다.
    """
    if l3 is None:
        return eff, None
    if eff is None:
        if l3.get("허용") not in 허용_강도:
            return None, None
        return _엄격병합([l3]), None
    if _L2우선L3(우선순위):
        return eff, {"모드": "사업우선", "L3더엄격": _L3더엄격(l3, eff), "L3반영": False}
    합 = _오버레이(eff, l3)
    엄격 = 합["허용"] != eff.get("허용") or any(r is l3 for r in 합["한도목록"])
    return 합, {"모드": "엄격병합", "L3더엄격": 엄격, "L3반영": "L3" in 합["적용층_기여"]}


def _결과(비목: str | None, eff: dict, 비교: dict | None, 우선규범: str | None,
         참고_L3: dict | None, 룰행: Sequence[dict], l3: dict | None,
         L2우선L3: bool) -> dict:
    """effective_rule 반환값. 키는 판정 파이프라인과 검증 단계가 읽는 것만 둔다."""
    룰들 = [{"verified": r["verified"], "layer": r["layer"], "rule_id": r["rule_id"]}
            for r in 룰행]
    if l3:
        룰들.append({"verified": l3["verified"], "layer": "L3",
                     "rule_id": l3.get("rule_id"), "article_id": l3.get("article_id")})
    return {
        "허용": eff["허용"],
        "적용층": eff["적용층"],
        "적용층_기여": eff["적용층_기여"],
        "증빙": eff["증빙"],
        "B4문장": B4문장(비목, eff, 비교, 우선규범, 참고_L3),
        "룰들": 룰들,
        "L2우선L3": L2우선L3,
    }


def effective_rule(cur, 사업명: str | None, 비목: str | None, *,
                   수치: dict | None = None, l3룰: dict | None = None,
                   하위항목: str | None = None) -> dict | None:
    """사업·비목에 적용되는 룰을 L1·L2·L3 병합으로 결정한다.

    반환: {"허용", "적용층", "적용층_기여", "증빙", "B4문장", "룰들", "L2우선L3"} | None.
    None 은 오류가 아니다. 룰이 없는 사업·비목이 있고, corpus.programs 에 없는 사업명도 None 이다
    (서버는 모르는 사업명을 먼저 422 로 거부한다). DB 오류는 예외로 올린다.

    l3룰 은 호출자가 l3_load.l3룰() 로 읽은 현재 기관의 L3 룰이다. 수치·하위항목 은 한도 비교에
    쓴다. L2우선L3 은 사업 세부관리기준이 L2 > L3 를 범위 'all' 로 정했는지다.
    L3 는 자동으로 우선하지 않는다. 여러 사업이 적용범위 조에서 L2 > L3 를 명시하고, 그 경우
    주관기관 규정이 더 엄격해도 사업 관리기준이 우선한다.
    """
    정보 = _사업정보(cur, 사업명)
    계통 = 정보["계통"] if 정보 else None
    base = base_룰(cur, 사업명, 비목, 계통)
    l3 = _l3_선택(l3룰, 계통)
    if not base and not l3:
        return None

    우선순위 = _precedence(cur, 사업명)
    행, 허용층우선 = _층병합(base, 우선순위)
    eff = _엄격병합(행, 허용층우선=허용층우선) if 행 else None
    eff, 참고_L3 = _l3_반영(eff, l3, 우선순위)
    if eff is None:
        return None
    비교 = _한도전수(eff["한도목록"], 수치, 하위항목)
    return _결과(비목, eff, 비교, _우선규범(정보, 우선순위), 참고_L3, 행, l3,
                _L2우선L3(우선순위))


# L3 게이팅

def _참조만(r: dict) -> bool:
    """"지침 제N조에 따른다"만 있고 자기 내용이 없는 L3 룰인가.

    입력에 참조만 키가 있으면(l3_load.l3룰 결과) 그 값을 쓰고, 없으면 내용 유무로 판단한다.
    """
    if r.get("참조만") is not None:
        return bool(r["참조만"])
    실체 = (r.get("금지예시") or []) + (r.get("허용예시") or []) + (r.get("증빙") or [])
    return (not 실체 and r.get("한도_값") is None and not r.get("사전승인")
            and bool(r.get("근거")))


def l3_게이팅(l3룰: dict | None) -> dict:
    """상위(L1·L2) 규범을 확인해야 하는지 코드로 판단한다. LLM 에게 묻지 않는다.

    반환: {"need_upper": bool, "seed_refs": [...], "분기": str, "사유": str}
    L3 가 "가능"이면 need_upper=True 로 둔다. 주관기관 규정은 국가 지침을 전제로 그 위에 추가하는
    문서라 단독으로 완결되지 않는다. L3 의 "총장 승인만 받으면 됩니다"만 보고 답하면 상위의 제약을
    놓친 틀린 "가능"이 된다. "불가"는 L3 단독으로도 안전하지만 "가능"은 L3 단독으로 확정하지 않는다.
    """
    if not l3룰:
        return {"need_upper": True, "seed_refs": [],
                "분기": "1-미규정", "사유": "L3 에 해당 비목 규정이 없다 = 정하지 아니한 사항"}
    근거 = list(l3룰.get("근거") or [])
    if _참조만(l3룰):
        return {"need_upper": True, "seed_refs": 근거,
                "분기": "2-참조만", "사유": "L3 가 상위 조문을 가리키기만 한다"}
    허용 = l3룰.get("허용")
    if 허용 in ("불가", "조건부"):
        return {"need_upper": False, "seed_refs": 근거,
                "분기": "3-L3제약", "사유": "L3 가 막거나 조건을 건다 — 상위가 관대해도 결론 불변"}
    if 허용 == "가능":
        return {"need_upper": True, "seed_refs": 근거,
                "분기": "4-L3가능", "사유": "L3 단독 '가능' 은 확정하지 않는다 (오답 비대칭)"}
    return {"need_upper": True, "seed_refs": 근거,
            "분기": "0-불명", "사유": f"허용값 '{허용}' 을 해석할 수 없다 — 안전측으로 상위를 본다"}
