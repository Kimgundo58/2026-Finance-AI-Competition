# -*- coding: utf-8 -*-
"""온톨로지 조회 함수(읽기 전용, docs/5_룰/5-8_저장과_조회.md). 판정 · 질문 생성의 새 경로가 쓴다.

노드_결정 · 품목_노드 · 경로밖_품목 · 품목_후보 · 노드결정_적용 · 상위_세목 · 경로밖_규범 · 상황_후보 · 상황_규범 · 비율_규범 · 비목_품목금지 · 조문_행 · 세목_한도_있음 · 적용_규범 · 근거_조문 · 새경로_대상 · 근거경로_문장. 쓰지 않는다. 어떤 예외도 호출자에게 그대로 올린다
(호출자 orchestrate 가 판단불가로 바꾼다). 없는 노드 ID 는 LookupError 다.
L3 는 여기서 다루지 않는다(능력 질문 시험의 `문맥_구성` 호출만 판정과 같은 문맥 구성 함수로 L3 연결을 본다). 읽는 테이블: corpus.concepts · concept_labels · concept_relations · norms · provenance ·
documents · doc_articles.

    python scripts/ontology_lookup.py --cq-test ontology/테스트/능력질문.json     # 전부 일치해야 0 으로 끝난다(DB 필요)
    python scripts/ontology_lookup.py --self-test   # 조문 ID 풀이 · 근거 경로 문장 · 조 단위 식(식은 DB 필요, 없으면 건너뜀)
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import paths  # noqa: E402

ROOT = paths.ensure_on_path()

from ontology_norm import 모든비목, 조문ID_풀이, 조번호_표시  # noqa: E402
from rule_lookup import _norm  # noqa: E402

준용_관계 = ("준용", "위임")
_스키마_경로 = ROOT / "ontology" / "schema.json"

# 조문 ID 에서 항 · 호 · 표 · 별지 행을 뗀 조 단위 ID. 조 전체를 준용한 관계가 그 조의 항에도 걸리게 한다.
_조단위_식 = (r"regexp_replace({0}, '^(조:[^#]+#(?:제[0-9]+조(?:의[0-9]+)?|(?:별지|별표|붙임)\s*[0-9]*"
             r"|부칙(?:\s*제[0-9]+조(?:의[0-9]+)?)?|(?:참고[0-9]+-)?p[0-9]+-[0-9]+"
             r"|제[0-9]+편(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:-[0-9]+)?)?|표지)).*$', '\1')")


@functools.lru_cache(maxsize=1)
def _스키마() -> dict:
    """ontology/schema.json. 문서키 ↔ doc_id 표의 기준이다. 없으면 예외(호출자가 판단불가로 바꾼다)."""
    return json.loads(_스키마_경로.read_text(encoding="utf-8"))


def _별칭_일치(cur, 표현: str, 사업: str | None) -> list[tuple]:
    키 = _norm(표현 or "")
    if not 키:
        return []
    return cur.execute(
        """SELECT c.concept_id, c.종류, c.pref_label, c.정의, c.적용사업, c.검수단계, l.적용사업 IS NULL AS 공통
             FROM corpus.concept_labels l JOIN corpus.concepts c USING (concept_id)
            WHERE l.정규화 = %s AND (l.적용사업 IS NULL OR l.적용사업 = %s)
            ORDER BY 공통, c.concept_id""", (키, 사업)).fetchall()


def 노드_결정(cur, 표현: str, 사업: str | None) -> list[dict]:
    """rule_lookup._norm 으로 정규화 → 별칭 정확 일치. 사업한정을 먼저, 공통을 뒤에 전부 돌려준다. 없으면 빈 목록.

    사업한정이 공통을 가리면 재료비로 고른 귀금속이 공통 품목에 닿지 못한다. 어느 노드를 쓸지는 호출자가 확정 경로 안에서 고른다.
    """
    return [{"concept_id": r[0], "종류": r[1], "pref_label": r[2], "정의": r[3], "적용사업": r[4], "검수단계": r[5]}
            for r in _별칭_일치(cur, 표현, 사업)]


def _상위_경로(cur, 대상_id: str, 사업들: list[str]) -> list[tuple[str, str]]:
    """대상에서 위로 올라가는 (하위, 상위) 목록. 가까운 것부터."""
    행들 = cur.execute(
        """WITH RECURSIVE 상위경로(하위, 상위, 깊이, 방문) AS (
             SELECT r.src_id, r.dst_id, 1, ARRAY[r.src_id, r.dst_id]
               FROM corpus.concept_relations r
              WHERE r.관계 = '상위' AND r.src_id = %(대상)s AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             UNION ALL
             SELECT r.src_id, r.dst_id, p.깊이 + 1, p.방문 || r.dst_id
               FROM 상위경로 p JOIN corpus.concept_relations r ON r.src_id = p.상위
              WHERE r.관계 = '상위' AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
                AND NOT r.dst_id = ANY(p.방문))
           SELECT DISTINCT 하위, 상위, 깊이 FROM 상위경로 ORDER BY 깊이, 하위, 상위""",
        {"대상": 대상_id, "사업들": 사업들}).fetchall()
    return [(r[0], r[1]) for r in 행들]


def _노드_확인(cur, 대상_id: str) -> None:
    if cur.execute("SELECT 1 FROM corpus.concepts WHERE concept_id = %s", (대상_id,)).fetchone() is None:
        raise LookupError(f"없는 노드 ID: {대상_id}")


_규범_컬럼 = ("norm_id", "종류", "계층", "적용사업", "대상", "수범자", "승인주체", "조건", "수치", "결과",
             "예외", "해석미결", "검수단계", "verified")


def _근거_붙이기(cur, 규범들: list[dict]) -> None:
    묶음: dict[str, list[dict]] = {n["norm_id"]: [] for n in 규범들}
    for r in cur.execute(
            """SELECT owner_id, 칸, doc_id, 조번호, 항호, 발췌 FROM corpus.provenance
                WHERE owner_kind = '규범' AND owner_id = ANY(%s)
                ORDER BY owner_id, 칸, doc_id, 조번호, 항호, 발췌""", (list(묶음),)).fetchall():
        묶음[r[0]].append({"칸": r[1], "doc_id": r[2], "조번호": r[3], "항호": r[4], "발췌": r[5]})
    for n in 규범들:
        n["근거"] = 묶음[n["norm_id"]]


def _우선순위_찾기(우선순위들: list[dict], 채택: dict, 제외: dict) -> dict | None:
    """채택 · 제외 규범의 계층과 맞는 우선순위 규범. 원천 조건의 상위 · 하위 값만 본다."""
    for r in 우선순위들:
        조건 = r.get("조건") or {}
        if {조건.get("상위"), 조건.get("하위")} == {채택.get("계층"), 제외.get("계층")}:
            return r
    return None


def _우선순위_적용(규범들: list[dict]) -> tuple[list[dict], list[dict]]:
    """원천에 적힌 값으로만 중복을 정리한다. 텍스트로 추측하지 않는다.

    결과.대체 = [규범 ID]: 그 규범이 대상 규범을 대신한다. 대상은 규범 목록에서 빠지고 우선순위_적용 에 남는다.
    해석미결 규범은 코드 판정에 쓰지 않으므로 다른 규범을 대체하지 못한다.
    예외 = [규범 ID]: 단서는 조건이 맞을 때만 원칙을 덮어쓰므로 원칙을 목록에서 빼지 않는다. 빼면 원칙(금지)이 문맥에서
    사라져 틀린 '가능' 이 될 수 있다. 관계만 우선순위_적용 에 남긴다(조건.규범에_남음 = true).
    """
    표 = {n["norm_id"]: n for n in 규범들}
    우선순위들 = [n for n in 규범들 if n["종류"] == "우선순위"]
    적용: list[dict] = []
    제외됨: set[str] = set()
    for n in 규범들:
        결과 = n.get("결과") if isinstance(n.get("결과"), dict) else {}
        대체 = 결과.get("대체") or []
        for 대상 in ([대체] if isinstance(대체, str) else 대체):
            if 대상 not in 표 or n.get("해석미결"):
                continue
            r = _우선순위_찾기(우선순위들, n, 표[대상])
            제외됨.add(대상)
            적용.append({"채택": n["norm_id"], "제외": 대상, "채택규범": n, "제외규범": 표[대상], "조건": {
                "종류": "대체", "우선순위규범": r["norm_id"] if r else None,
                "범위": (r.get("조건") or {}).get("범위") if r else None}})
    for n in 규범들:
        for 단서 in n.get("예외") or []:
            if 단서 in 표:
                적용.append({"채택": 단서, "제외": n["norm_id"], "조건": {
                    "종류": "예외", "규범에_남음": True, "단서조건": 표[단서].get("조건")}})
    return [n for n in 규범들 if n["norm_id"] not in 제외됨], 적용


def 적용_규범(cur, 사업: str | None, 대상_id: str) -> dict:
    """대상 + 상위 노드 규범(상속) + `비목:창업/*` 공통 규범. 적용사업이 NULL 이거나 그 사업인 것만.

    반환 {규범: [...], 경로: [(하위, 상위)...], 우선순위_적용: [{채택, 제외, 조건}...]}. L3 는 다루지 않는다.
    """
    _노드_확인(cur, 대상_id)
    사업들 = [사업] if 사업 else []
    경로 = _상위_경로(cur, 대상_id, 사업들)
    노드들 = [대상_id, *{p for _, p in 경로}, 모든비목]
    컬럼 = ", ".join(_규범_컬럼)
    행들 = cur.execute(
        f"""SELECT {컬럼} FROM corpus.norms
             WHERE 대상 && %s::text[] AND (적용사업 IS NULL OR 적용사업 = ANY(%s))
             ORDER BY 계층, norm_id""", (노드들, 사업들)).fetchall()
    규범들 = [dict(zip(_규범_컬럼, r)) for r in 행들]
    _근거_붙이기(cur, 규범들)
    남은, 적용 = _우선순위_적용(규범들)
    return {"규범": 남은, "경로": 경로, "우선순위_적용": 적용}


# 근거 조문 ────────────────────────────────────────────────────────────────

def _조문ID_만들기(doc_id: str, 조번호: str, 항호: str | None, 키표: dict[str, str]) -> str | None:
    문서키 = 키표.get(doc_id)
    return f"조:{문서키}#{조번호}{항호 or ''}" if 문서키 else None


def _조문_정보(cur, 조문들: list[str], schema: dict) -> dict[str, dict]:
    """조문 ID → {doc_id, 조번호, 항호, article_id, 건너뜀}. 활성 문서에 있는 조문만 article_id 가 있다."""
    문서키 = schema.get("문서키") or {}
    풀이들: dict[str, dict] = {}
    for i in 조문들:
        풀이 = 조문ID_풀이(i)
        풀이들[i] = {"doc_id": 문서키.get(풀이["문서키"]), "조번호": 풀이["조번호"], "항호": 풀이["항호"]}
    doc_ids = sorted({v["doc_id"] for v in 풀이들.values() if v["doc_id"]})
    상태 = dict(cur.execute("SELECT doc_id, status FROM corpus.documents WHERE doc_id = ANY(%s)", (doc_ids,)).fetchall())
    글 = {(r[0], r[1]): r[2] for r in cur.execute(
        "SELECT doc_id, 조번호, article_id FROM corpus.doc_articles WHERE doc_id = ANY(%s)", (doc_ids,)).fetchall()}
    out: dict[str, dict] = {}
    for i, v in 풀이들.items():
        건너뜀 = None
        article = None
        if not v["doc_id"]:
            건너뜀 = "문서키 없음"
        elif 상태.get(v["doc_id"]) != "active":
            건너뜀 = "활성 문서 아님"
        else:
            article = 글.get((v["doc_id"], v["조번호"]))
            if article is None:
                건너뜀 = "조문 없음"
        out[i] = {**v, "article_id": article, "건너뜀": 건너뜀}
    return out


def _다른사업_문서(사업들: list[str]) -> list[str]:
    """이 사업들 밖 사업 전용 문서의 doc_id(schema.json `문서사업`). 공통 개념의 근거에는 여러 사업의 문서가 합쳐져 있어,
    판정 문맥에 다른 사업 조문이 들지 않게 개념 근거에서 뺀다. 표에 없는 문서는 공통이다."""
    schema = _스키마()
    문서, 사업표 = schema.get("문서키") or {}, schema.get("사업키") or {}
    return [문서[k] for k, 키 in (schema.get("문서사업") or {}).items() if k in 문서 and 사업표.get(키) not in 사업들]


def 근거_조문(cur, 규범들: list[dict], 깊이: int = 3, 대상_id: str | None = None, *, 사업: str | None = None) -> list[dict]:
    """시작 집합 = 규범 근거 + 대상 · 상위 노드의 개념 근거. 나가는 준용 · 위임을 깊이까지 따라간다.

    시작 조문을 dst 로 갖는 준용 · 위임의 src 는 경로 앞에 붙이되 그 src 의 다른 dst 로는 퍼지지 않는다.
    반환 [{doc_id, 조번호, 항호, article_id, 깊이, 출처, 경로:[조문 ID...]}]. 출처는 규범근거 · 개념근거 · 준용 · 위임 ·
    준용_앞 · 위임_앞. 활성 문서에 없거나 조문을 특정하지 못한 대상은 article_id 가 None 이고 `건너뜀` 에 사유가 있다
    (호출자는 article_id 가 있는 행만 문맥에 넣는다). 경로에는 그대로 남는다.
    확정 노드에서 시작한 행에는 `우선경로` · `경로순위`(0 = 대상 노드, 커질수록 상위)가 더 있다. 화면 "근거 경로" 가 먼저 쓴다.
    우선순위 규범의 근거 조문은 시작 집합에 넣지 않고(준용 · 위임을 따라가지 않는다) 맨 뒤에 `우선순위근거: True` 인
    규범근거 행으로 따로 낸다. 문맥에는 들어가고 화면 "근거 경로" 에는 쓰지 않는다.
    관계의 적용사업은 사업 또는 규범들의 적용사업으로 거른다. 둘 다 없으면 공통 관계만 본다.
    """
    schema = _스키마()
    키표 = {v: k for k, v in (schema.get("문서키") or {}).items()}
    사업들 = sorted({n["적용사업"] for n in 규범들 if n.get("적용사업")} | ({사업} if 사업 else set()))

    시작: dict[str, str] = {}                       # 조문 ID → 출처
    시작순위: dict[str, int] = {}                   # 조문 ID → 확정 노드에서 얼마나 가까운가. 공통(`비목:창업/*`)에서만 온 조문은 없다
    건너뛴_시작: list[dict] = []

    def 시작_넣기(doc_id: str, 조번호: str, 항호: str | None, 출처: str, 순위: int | None) -> None:
        i = _조문ID_만들기(doc_id, 조번호, 항호, 키표)
        if i is None:
            건너뛴_시작.append({"doc_id": doc_id, "조번호": 조번호, "항호": 항호, "article_id": None, "깊이": 0,
                             "출처": 출처, "경로": [f"{doc_id}#{조번호}{항호 or ''}"], "건너뜀": "문서키 없음"})
            return
        시작.setdefault(i, 출처)
        if 순위 is not None and 순위 < 시작순위.get(i, 순위 + 1):
            시작순위[i] = 순위

    사슬노드: list[str] = []
    if 대상_id:
        _노드_확인(cur, 대상_id)
        사슬노드 = list(dict.fromkeys([대상_id, *(p for _, p in _상위_경로(cur, 대상_id, 사업들))]))
    노드순위 = {v: i for i, v in enumerate(사슬노드)}

    def 규범_순위(n: dict) -> int | None:
        대상들 = n.get("대상") or []
        가까움 = [노드순위[v] for v in 대상들 if v in 노드순위]
        if 가까움:
            return min(가까움)
        return None if 모든비목 in 대상들 else len(사슬노드)     # 사슬 밖 대상(경로 밖 품목 규범)은 확정 노드 뒤, 공통 앞

    우선순위_근거: dict[str, None] = {}             # 우선순위 규범의 근거 조문. 문맥 후보로만 넣고 사슬 시작점으로 쓰지 않는다
    for n in 규범들:
        if n.get("종류") == "우선순위":
            # 제33조② 처럼 모든 비목의 준용을 나열한 조문에서 시작하면 다른 비목의 조까지 퍼진다.
            # 그 조문은 대상 비목의 조(지침 제42조)를 dst 로 가질 때 경로 앞에 붙는다.
            # 근거 조문 자체는 4-4 ② 3번(규범의 근거)대로 문맥 후보에 넣는다(아래 `우선순위근거` 행)
            for g in n.get("근거") or []:
                i = _조문ID_만들기(g["doc_id"], g["조번호"], g["항호"], 키표)
                if i is not None:
                    우선순위_근거.setdefault(i)
            continue
        순위 = 규범_순위(n)
        for g in n.get("근거") or []:
            시작_넣기(g["doc_id"], g["조번호"], g["항호"], "규범근거", 순위)
    if 대상_id:
        for r in cur.execute(
                """SELECT DISTINCT doc_id, 조번호, 항호, owner_id FROM corpus.provenance
                    WHERE owner_kind = '개념' AND owner_id = ANY(%s) AND doc_id <> ALL(%s)
                    ORDER BY doc_id, 조번호, 항호, owner_id""", (사슬노드, _다른사업_문서(사업들))).fetchall():
            시작_넣기(r[0], r[1], r[2], "개념근거", 노드순위[r[3]])

    시작목록 = sorted(시작)
    조단위_시작 = _조단위_식.format("시작.id")
    앞_행들 = cur.execute(
        f"""SELECT DISTINCT 시작.id, r.src_id, r.관계
              FROM unnest(%(시작)s::text[]) AS 시작(id)
              JOIN corpus.concept_relations r
                ON r.관계 = ANY(%(관계)s) AND (r.dst_id = 시작.id OR r.dst_id = {조단위_시작})
             WHERE r.src_id LIKE '조:%%' AND r.src_id <> 시작.id
               AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             ORDER BY 시작.id, r.src_id""",
        {"시작": 시작목록, "관계": list(준용_관계), "사업들": 사업들}).fetchall()
    앞: dict[str, str] = {}
    앞_출처: dict[str, str] = {}
    for s, src, 관계 in 앞_행들:
        앞.setdefault(s, src)                       # 여럿이면 첫 번째를 경로에 쓴다. 나머지도 아래에서 행으로 나온다
        앞_출처.setdefault(src, f"{관계}_앞")

    사슬: list[tuple] = []
    if 깊이 > 0:
        조단위_s = _조단위_식.format("s.dst_id")
        사슬 = cur.execute(
            f"""WITH RECURSIVE 사슬(시작id, dst_id, 관계, 깊이, 경로) AS (
                  SELECT 시작.id, r.dst_id, r.관계, 1, ARRAY[시작.id, r.dst_id]
                    FROM unnest(%(시작)s::text[]) AS 시작(id)
                    JOIN corpus.concept_relations r
                      ON r.관계 = ANY(%(관계)s) AND (r.src_id = 시작.id OR r.src_id = {조단위_시작})
                   WHERE r.dst_id <> 시작.id AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
                  UNION ALL
                  SELECT s.시작id, r.dst_id, r.관계, s.깊이 + 1, s.경로 || r.dst_id
                    FROM 사슬 s JOIN corpus.concept_relations r
                      ON r.관계 = ANY(%(관계)s) AND (r.src_id = s.dst_id OR r.src_id = {조단위_s})
                   WHERE s.깊이 < %(깊이)s AND NOT r.dst_id = ANY(s.경로)
                     AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s)))
                SELECT 시작id, dst_id, 관계, 깊이, 경로 FROM 사슬 ORDER BY 깊이, 시작id, dst_id""",
            {"시작": 시작목록, "관계": list(준용_관계), "사업들": 사업들, "깊이": 깊이}).fetchall()

    조문들 = set(시작목록) | set(앞_출처) | {r[1] for r in 사슬 if r[1].startswith("조:")} | set(우선순위_근거)
    정보 = _조문_정보(cur, sorted(조문들), schema)

    def 앞_경로(s: str) -> list[str]:
        return [앞[s], s] if s in 앞 else [s]

    결과: dict[str, dict] = {}

    def 우선경로_적기(행: dict, 경로: list[str], 순위: int | None) -> None:
        """확정 노드에서 시작한 경로 중 가장 가깝고 긴 것을 화면 "근거 경로" 용으로 남긴다. 공통 규범에서만 온 행에는 없다."""
        if 순위 is None:
            return
        현재 = 행.get("경로순위")
        if 현재 is None or 순위 < 현재 or (순위 == 현재 and len(경로) > len(행["우선경로"])):
            행.update(경로순위=순위, 우선경로=경로)

    def 넣기(i: str, 깊이값: int, 출처: str, 경로: list[str], 순위: int | None = None) -> None:
        if i in 결과:
            # 시작 조문이 사슬로도 닿으면 깊이 · 출처는 그대로 두고 화면 "근거 경로" 에 더 긴 경로를 쓴다
            if len(경로) > len(결과[i]["경로"]):
                결과[i]["경로"] = 경로
            우선경로_적기(결과[i], 경로, 순위)
            return
        v = 정보[i]
        행 = {"doc_id": v["doc_id"], "조번호": v["조번호"], "항호": v["항호"], "article_id": v["article_id"],
             "깊이": 깊이값, "출처": 출처, "경로": 경로}
        if v["건너뜀"]:
            행["건너뜀"] = v["건너뜀"]
        우선경로_적기(행, 경로, 순위)
        결과[i] = 행

    for s in 시작목록:
        넣기(s, 0, 시작[s], 앞_경로(s), 시작순위.get(s))
    for src, 출처 in sorted(앞_출처.items()):
        넣기(src, 0, 출처, [src])
    끝: list[dict] = []
    for 시작id, dst, 관계, d, 경로 in 사슬:
        전체경로 = ([앞[시작id]] if 시작id in 앞 else []) + list(경로)
        if dst.startswith("조:"):
            넣기(dst, d, 관계, 전체경로, 시작순위.get(시작id))
        else:                                       # 미특정 · 행위자(위임). 조문이 아니라 문맥에 넣지 못한다
            행 = {"doc_id": None, "조번호": None, "항호": None, "article_id": None, "깊이": d, "출처": 관계,
                 "경로": 전체경로, "건너뜀": "조문 미특정" if dst.startswith("미특정") else "조문 아님"}
            우선경로_적기(행, 전체경로, 시작순위.get(시작id))
            끝.append(행)
    # 이미 다른 출처로 나온 조문이어도 따로 낸다. 문맥은 같은 조 · 항을 더 높은 순위(규범 근거)로 한 번만 넣는다.
    # 화면 "근거 경로" 는 이 행을 쓰지 않는다(`우선순위근거`, ontology_context.근거경로들)
    우선행 = []
    for i in 우선순위_근거:
        v = 정보[i]
        행 = {"doc_id": v["doc_id"], "조번호": v["조번호"], "항호": v["항호"], "article_id": v["article_id"],
             "깊이": 0, "출처": "규범근거", "경로": [i], "우선순위근거": True}
        if v["건너뜀"]:
            행["건너뜀"] = v["건너뜀"]
        우선행.append(행)
    return [*건너뛴_시작, *결과.values(), *끝, *우선행]


def 근거경로_문장(경로: list[str], schema: dict) -> str:
    """화면 "근거 경로". 예 `모두의 창업 제33조② → 지침 제42조 → 모두의 창업 제36조①`.

    문서 표시명 + 조번호만 쓴다. 노드 ID · 규범 ID · 문장 번호는 넣지 않는다.
    """
    표시 = schema.get("문서 표시명") or {}
    조각: list[str] = []
    for i in 경로:
        if i.startswith("조:"):
            풀이 = 조문ID_풀이(i)
            이름 = 표시.get(풀이["문서키"])
            if not 이름:
                continue                            # 내부 문서키를 화면에 내지 않는다
            항호 = 풀이["항호"] or ""
            글 = f"{이름} {조번호_표시(풀이['조번호'])}" + (" " + 항호.replace("/", " ").strip() if 항호.startswith("/") else 항호)
        elif i.startswith("미특정:"):
            글 = i.split(":", 1)[1]                 # 규정이 조를 적지 않았다. 법령 이름만 낸다
        else:
            continue
        if not 조각 or 조각[-1] != 글:
            조각.append(글)
    return " → ".join(조각)


def 레이블_목록(cur, 사업명: str | None) -> list[dict]:
    """질의 변환 · 분류 확인 화면에 쓰는 분류 목록 [{표시문, 비목, 세목, 별칭:[]}]. 내부 ID 를 넣지 않는다.

    사업이 `사용` 관계로 쓰는 비목과 그 아래 세목만 낸다. `사용` 의 src 는 `사업:<사업키>` 이거나 그 사업에 적용되는
    개념(절차:시제품제작@모두의창업 · 절차:창업활동자금@모두의창업)이다. 사업키를 모르면 빈 목록이다.
    """
    사업키 = next((k for k, v in (_스키마().get("사업키") or {}).items() if v == 사업명), None)
    if not 사업키:
        return []
    사업들 = [사업명]
    행들 = cur.execute(
        """SELECT b.concept_id, b.pref_label, NULL::text, NULL::text
             FROM corpus.concept_relations r JOIN corpus.concepts b ON b.concept_id = r.dst_id
            WHERE r.관계 = '사용' AND b.종류 = '비목' AND (r.src_id = %(사업id)s OR r.적용사업 = ANY(%(사업들)s))
           UNION ALL
           SELECT s.concept_id, b.pref_label, s.pref_label, b.concept_id
             FROM corpus.concept_relations r
             JOIN corpus.concepts b ON b.concept_id = r.dst_id
             JOIN corpus.concept_relations h ON h.관계 = '상위' AND h.dst_id = b.concept_id
                  AND (h.적용사업 IS NULL OR h.적용사업 = ANY(%(사업들)s))
             JOIN corpus.concepts s ON s.concept_id = h.src_id AND s.종류 = '세목'
            WHERE r.관계 = '사용' AND b.종류 = '비목' AND (r.src_id = %(사업id)s OR r.적용사업 = ANY(%(사업들)s))
            ORDER BY 2, 3 NULLS FIRST""", {"사업id": f"사업:{사업키}", "사업들": 사업들}).fetchall()
    별칭: dict[str, list[str]] = {}
    for cid, 글 in cur.execute("""SELECT concept_id, alt_label FROM corpus.concept_labels
                                  WHERE concept_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
                                  ORDER BY concept_id, alt_label""", ([r[0] for r in 행들], 사업명)).fetchall():
        별칭.setdefault(cid, []).append(글)
    return [{"표시문": f"{r[1]} > {r[2]}" if r[2] else r[1], "비목": r[1], "세목": r[2], "별칭": 별칭.get(r[0], []),
             "노드": r[0]} for r in 행들]


def 별칭_후보(cur, 표현: str | None, 사업명: str | None, 레이블: list[dict]) -> list[dict]:
    """품목 표현의 별칭 정확 일치 → 그 노드(품목이면 가장 가까운 상위 세목 · 비목)의 분류 후보. 없으면 빈 목록.

    사업 한정과 공통이 같은 이름이면(외주용역비: 창업활동자금 세목과 공통 비목) 둘 다 후보로 내고 사용자가 고른다.
    """
    표 = {x["노드"]: x for x in 레이블}
    사업들 = [사업명] if 사업명 else []
    out: list[dict] = []
    for 행 in _별칭_일치(cur, 표현 or "", 사업명):
        차례 = [행[0], *(p for _, p in _상위_경로(cur, 행[0], 사업들))]
        x = next((표[i] for i in 차례 if i in 표), None)
        if x and all(o["표시문"] != x["표시문"] for o in out):
            out.append({"비목": x["비목"], "세목": x["세목"], "표시문": x["표시문"], "신뢰도": 1.0})
    return out


def 대상_노드(cur, 사업명: str | None, 비목: str, 세목: str | None = None) -> dict:
    """화면에서 확정한 비목 · 세목 표기 → 노드 ID. 세목이 주어졌는데 그 비목 아래에 없으면 LookupError(판단불가)."""
    사업들 = [사업명] if 사업명 else []
    행 = cur.execute("""SELECT concept_id FROM corpus.concepts WHERE 종류 = '비목' AND pref_label = %s
                         AND (적용사업 IS NULL OR 적용사업 = ANY(%s)) ORDER BY 적용사업 NULLS LAST LIMIT 1""",
                     (비목, 사업들)).fetchone()
    if 행 is None:
        raise LookupError(f"없는 비목: {비목}")
    비목_id = 행[0]
    if not 세목:
        return {"대상_id": 비목_id, "비목_id": 비목_id, "표시문": 비목, "비목": 비목, "세목": None}
    행 = cur.execute("""SELECT s.concept_id FROM corpus.concepts s
                          JOIN corpus.concept_relations h ON h.src_id = s.concept_id AND h.관계 = '상위' AND h.dst_id = %s
                         WHERE s.종류 = '세목' AND s.pref_label = %s AND (s.적용사업 IS NULL OR s.적용사업 = ANY(%s))
                           AND (h.적용사업 IS NULL OR h.적용사업 = ANY(%s))
                         ORDER BY s.적용사업 NULLS LAST LIMIT 1""", (비목_id, 세목, 사업들, 사업들)).fetchone()
    if 행 is None:
        raise LookupError(f"{비목} 아래에 없는 세목: {세목}")
    return {"대상_id": 행[0], "비목_id": 비목_id, "표시문": f"{비목} > {세목}", "비목": 비목, "세목": 세목}


def 품목_노드(cur, 사업명: str | None, 표현: str | None, 확정_id: str) -> str | None:
    """품목 표현이 별칭과 정확히 일치하고 사용자가 확정한 노드 아래에 있으면 그 품목 노드(5-6 ① 노드 결정).

    확정 노드 밖의 일치는 시작 노드로 쓰지 않는다(`경로밖_품목`). 금지 일치가 잘못된 불가가 되면 안 되므로 유사 일치도 하지 않는다.
    """
    사업들 = [사업명] if 사업명 else []
    for 행 in 노드_결정(cur, 표현 or "", 사업명):
        if 행["종류"] == "품목" and 확정_id in {p for _, p in _상위_경로(cur, 행["concept_id"], 사업들)}:
            return 행["concept_id"]
    return None


def 경로밖_품목(cur, 사업명: str | None, 표현: str | None, 비목_id: str) -> list[str]:
    """품목 표현이 별칭과 정확히 일치하는데 확정 경로 밖인 품목 · 세목 노드. 확정 세목과 같은 비목 또는 모든 비목(`비목:창업/*`) 아래만.

    `품목_노드` 가 None 일 때 부른다. 이 노드의 규범은 코드 불가를 내지 않고(5-6 ③ 확정 노드) 판정 문맥과 조건 문장에만 쓴다.
    다른 비목 아래 노드는 다른 자금의 기준일 수 있어 넣지 않는다. 세목 이름을 품목으로 적은 경우(여비 + 「식비」)도 같은 비목 아래면 닿는다.
    """
    사업들 = [사업명] if 사업명 else []
    return [행["concept_id"] for 행 in 노드_결정(cur, 표현 or "", 사업명)
            if 행["종류"] in ("품목", "세목") and {비목_id, 모든비목} & {p for _, p in _상위_경로(cur, 행["concept_id"], 사업들)}]


def 품목_후보(cur, 사업명: str | None, 비목_id: str, 모든비목_포함: bool = True) -> list[dict]:
    """확인 질문 LLM 이 품목 노드를 고를 후보(5-13). 비목_id 또는 모든 비목(`비목:창업/*`) 아래 세목 · 품목 노드 [{id, 정식명, 별칭:[...]}].

    `경로밖_품목` 과 같은 범위다. 창업활동자금(모두의 창업 창업활동비)은 모든 비목을 빼고 부른다(`온톨로지_조회` 품목표와 같은 규칙).
    노드 · 관계 · 별칭은 적용사업이 NULL 이거나 그 사업인 것만. 정의는 넣지 않는다(대부분 비어 있다). concept_id 순.
    """
    사업들 = [사업명] if 사업명 else []
    뿌리 = [비목_id, *([모든비목] if 모든비목_포함 and 비목_id != 모든비목 else [])]
    행들 = cur.execute(
        """WITH RECURSIVE 하위(id, 방문) AS (
             SELECT r.src_id, ARRAY[r.dst_id, r.src_id] FROM corpus.concept_relations r
              WHERE r.관계 = '상위' AND r.dst_id = ANY(%(뿌리)s) AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             UNION ALL
             SELECT r.src_id, h.방문 || r.src_id FROM 하위 h JOIN corpus.concept_relations r ON r.dst_id = h.id
              WHERE r.관계 = '상위' AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s)) AND NOT r.src_id = ANY(h.방문))
           SELECT DISTINCT c.concept_id, c.pref_label FROM 하위 h JOIN corpus.concepts c ON c.concept_id = h.id
            WHERE c.종류 IN ('세목', '품목') AND (c.적용사업 IS NULL OR c.적용사업 = ANY(%(사업들)s))
            ORDER BY c.concept_id""", {"뿌리": 뿌리, "사업들": 사업들}).fetchall()
    별칭: dict[str, list[str]] = {}
    for cid, 글 in cur.execute("""SELECT concept_id, alt_label FROM corpus.concept_labels
                                  WHERE concept_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = ANY(%s))
                                  ORDER BY concept_id, alt_label""", ([r[0] for r in 행들], 사업들)).fetchall():
        if 글 not in 별칭.setdefault(cid, []):
            별칭[cid].append(글)
    return [{"id": r[0], "정식명": r[1], "별칭": [x for x in 별칭.get(r[0], []) if x != r[1]]} for r in 행들]


def 노드결정_적용(cur, 사업명: str | None, 대상: dict, 노드결정: dict | None, 모든비목_포함: bool) -> tuple[str | None, list[str], str | None]:
    """확인 질문 LLM 이 고른 품목 노드(5-13) → (시작 품목 노드, 경로밖 노드들, 확신). 별칭 일치가 없을 때만 호출자가 부른다.

    ID 가 이번 범위의 후보(`품목_후보`)에 없거나 확신이 동일 · 유사가 아니면 쓰지 않는다(지금 길). 판정 때 다시 확인한다.
    동일 + 품목 + 확정 대상 아래 → 시작 노드(`품목_노드` 와 같은 규칙, 게이트를 켠다).
    그 밖(유사, 동일인데 확정 세목 밖이거나 세목 노드) → 경로밖 길(`경로밖_품목` 과 같다. 문맥 · 조건 문장만, 코드 불가 없음).
    고른 노드가 확정 대상 자신이면 바뀌는 것이 없다.
    """
    if not isinstance(노드결정, dict):
        return None, [], None
    nid, 확신 = 노드결정.get("id"), 노드결정.get("확신")
    if not isinstance(nid, str) or 확신 not in ("동일", "유사") or nid == 대상["대상_id"]:
        return None, [], None
    if nid not in {c["id"] for c in 품목_후보(cur, 사업명, 대상["비목_id"], 모든비목_포함)}:
        return None, [], None
    사업들 = [사업명] if 사업명 else []
    종류 = cur.execute("SELECT 종류 FROM corpus.concepts WHERE concept_id = %s", (nid,)).fetchone()[0]
    if 확신 == "동일" and 종류 == "품목" and 대상["대상_id"] in {p for _, p in _상위_경로(cur, nid, 사업들)}:
        return nid, [], 확신
    return None, [nid], 확신


def 상위_세목(cur, 사업명: str | None, 노드_id: str, 비목_id: str) -> dict | None:
    """노드 결정으로 채울 세목(5-14 A) → {id, 세목(정식명)} 또는 None. 호출자가 「동일」 · 사용자 세목 없음을 이미 확인했다.

    노드가 세목이면 그 세목, 품목이면 상위 사슬에서 가장 가까운 세목이다. 어느 쪽이든 그 세목은 `비목_id` 아래여야 한다.
    비목 아래 세목 중 가장 가까운 단계에 둘 이상이면 고르지 않는다(None). 정식명을 화면이 고르는 길(`대상_노드`)로 다시 풀어
    같은 노드가 나올 때만 돌려준다. 노드 · 관계는 적용사업이 NULL 이거나 그 사업인 것만.
    """
    사업들 = [사업명] if 사업명 else []
    행 = cur.execute("SELECT 종류 FROM corpus.concepts WHERE concept_id = %s AND (적용사업 IS NULL OR 적용사업 = ANY(%s))",
                     (노드_id, 사업들)).fetchone()
    if 행 is None or 행[0] not in ("세목", "품목"):
        return None
    경로 = _상위_경로(cur, 노드_id, 사업들)
    깊이 = {노드_id: 0}
    for 하, 상 in 경로:               # 깊이 순으로 온다. 처음 닿은 깊이가 가장 짧은 거리다
        if 하 in 깊이 and 상 not in 깊이:
            깊이[상] = 깊이[하] + 1
    후보 = [노드_id] if 행[0] == "세목" else [p for p in 깊이 if p != 노드_id]
    종류표 = dict(cur.execute("SELECT concept_id, 종류 FROM corpus.concepts WHERE concept_id = ANY(%s)", (후보,)).fetchall())
    세목들 = [s for s in 후보 if 종류표.get(s) == "세목"
             and 비목_id in {p for _, p in (경로 if s == 노드_id else _상위_경로(cur, s, 사업들))}]
    if not 세목들:
        return None
    가까움 = min(깊이[s] for s in 세목들)
    세목들 = [s for s in 세목들 if 깊이[s] == 가까움]
    if len(세목들) != 1:
        return None
    이름 = cur.execute("SELECT pref_label FROM corpus.concepts WHERE concept_id = %s", (세목들[0],)).fetchone()[0]
    비목 = cur.execute("SELECT pref_label FROM corpus.concepts WHERE concept_id = %s", (비목_id,)).fetchone()[0]
    try:
        풀이 = 대상_노드(cur, 사업명, 비목, 이름)
    except LookupError:
        return None
    return {"id": 세목들[0], "세목": 이름} if 풀이["대상_id"] == 세목들[0] and 풀이["비목_id"] == 비목_id else None


def 경로밖_규범(cur, 사업명: str | None, 품목들: list[str], 적용: dict) -> list[dict]:
    """경로 밖 품목에 걸린 규범 중 이미 적용 규범에 있는 것과 우선순위를 뺀 것. 그 품목의 상위 묶음 노드 규범도 포함한다."""
    이미 = {n["norm_id"] for n in 적용["규범"]}
    out: list[dict] = []
    for p in 품목들:
        for n in 적용_규범(cur, 사업명, p)["규범"]:
            if n["종류"] != "우선순위" and n["norm_id"] not in 이미:
                이미.add(n["norm_id"])
                out.append(n)
    return out


상황_종류 = ("절차", "시간", "주체")           # 비목 · 품목이 아닌 규범 대상 노드 종류(5-14 C)
상황_먼저_종류 = ("금지", "한도", "임계값", "요건")    # 후보를 자를 때 먼저 넣는 규범 종류(나머지는 의무 · 허용)
상황후보_상한 = 80            # 질문 프롬프트에 넣는 최대 상황 후보 수. 현재 질문 프롬프트가 2.5만~5.5만 자(중앙 3.6만)라 규칙 · 목록이 더하는 글자를 2천 자 안으로 잡았다(.claude/작업/조율/C_구현보고.md)
상황규범_상한 = 10            # 고른 상황 노드에서 판정에 더하는 최대 규범 수(금지 → 한도 → 임계값 → 요건 → 그 밖)


def 상황_후보(cur, 사업명: str | None, 상한: int | None = 상황후보_상한) -> list[dict]:
    """규범의 `대상` 에 쓰인 절차 · 시간 · 주체 노드 [{id, 정식명}](5-14 C). 질문 LLM 이 질문 글에 드러난 상황을 고르는 후보다.

    세지는 규범: `AI검증` 이상(verified) · 해석미결 아님 · 우선순위 아님 · 적용사업이 NULL 이거나 이 사업. 노드도 적용사업이 NULL 이거나 이 사업.
    비목에서 닿는 규범으로 좁히지 않는다(안 닿는 규범에 가는 길이 목적이다). 금지 · 한도 · 임계값 · 요건 규범의 대상을 먼저, 그 안에서는 그런 규범이 많은 노드를 먼저 두고
    상한(`상한`, None 이면 자르지 않음)까지 자른다. 같은 입력이면 항상 같은 순서다(판정 때 다시 만들어 ID 를 확인한다).
    """
    사업들 = [사업명] if 사업명 else []
    행들 = cur.execute(
        """SELECT c.concept_id, c.pref_label,
                  count(*) FILTER (WHERE n.종류 = ANY(%s)) AS 먼저수
             FROM corpus.norms n
             CROSS JOIN LATERAL unnest(n.대상) AS d(id)
             JOIN corpus.concepts c ON c.concept_id = d.id
            WHERE n.verified AND NOT n.해석미결 AND n.종류 <> '우선순위' AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%s))
              AND c.종류 = ANY(%s) AND (c.적용사업 IS NULL OR c.적용사업 = ANY(%s))
            GROUP BY c.concept_id, c.pref_label
            ORDER BY (count(*) FILTER (WHERE n.종류 = ANY(%s)) = 0), 3 DESC, c.concept_id""",
        (list(상황_먼저_종류), 사업들, list(상황_종류), 사업들, list(상황_먼저_종류))).fetchall()
    out = [{"id": r[0], "정식명": r[1]} for r in 행들]
    return out if 상한 is None else out[:상한]


def 상황_규범(cur, 사업명: str | None, 노드들: list[str], 이미: set[str]) -> list[dict]:
    """고른 상황 노드(5-14 C)를 `대상` 에 가진 규범(근거 포함). 수범자가 같다는 이유로는 넣지 않는다. 판정 때 다시 확인한다.

    노드 ID 가 지금의 상황 후보(`상황_후보`, 같은 상한)에 없으면 버린다. `이미` 에 든 규범(이미 적용 · 경로밖 · 공통미정)과 우선순위 규범은 뺀다.
    금지 → 한도 → 임계값 → 요건 → 그 밖 순으로 `상황규범_상한` 개까지 둔다. 코드 불가에 쓰지 않고 문맥 · 조건 문장으로만 쓴다.
    """
    노드들 = [x for x in dict.fromkeys(노드들) if isinstance(x, str)]
    허용 = {c["id"] for c in 상황_후보(cur, 사업명)}
    노드들 = [x for x in 노드들 if x in 허용]
    if not 노드들:
        return []
    사업들 = [사업명] if 사업명 else []
    행들 = cur.execute(
        f"""SELECT {", ".join(f'n."{c}"' for c in _규범_컬럼)} FROM corpus.norms n
             WHERE n.verified AND NOT n.해석미결 AND n.종류 <> '우선순위' AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%s)) AND n.대상 && %s
             ORDER BY n.norm_id""", (사업들, 노드들)).fetchall()
    규범들 = [n for n in (dict(zip(_규범_컬럼, r)) for r in 행들) if n["norm_id"] not in 이미]
    순서 = {k: i for i, k in enumerate(상황_먼저_종류)}
    규범들 = sorted(규범들, key=lambda n: (순서.get(n["종류"], len(순서)), n["norm_id"]))[:상황규범_상한]
    _근거_붙이기(cur, 규범들)
    return 규범들


def 비율_규범(cur, 사업명: str | None) -> list[dict]:
    """총사업비 대비 비율을 말하는 `AI검증` 이상 의무(근거 포함, 5-15 ②). 적용사업이 NULL 이거나 그 사업인 것.

    이 규범들은 협약 구성 규칙(「…로 구성해야」)이고 대상이 절차 노드(`절차:정부지원사업비` · `절차:자기부담현물` 등)라 비목 · 세목 · 품목에서 시작한
    조회에 닿지 않는다. 판정은 읽지 않고, 프로필 화면의 협약경고(server/routes_profile)만 쓴다. 고르는 기준은 문장 글자(총사업비 + % 또는 퍼센트)다.
    """
    사업들 = [사업명] if 사업명 else []
    행들 = cur.execute(
        f"""SELECT {", ".join(f'n."{c}"' for c in _규범_컬럼)} FROM corpus.norms n
             WHERE n.종류 = '의무' AND n.verified AND NOT n.해석미결 AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%s))
               AND (n.결과->>'문장') ~ '[0-9]\\s*(%%|퍼센트)' AND (n.결과->>'문장') ~ '총\\s*사업비'
             ORDER BY n.norm_id""", (사업들,)).fetchall()
    규범들 = [dict(zip(_규범_컬럼, r)) for r in 행들]
    _근거_붙이기(cur, 규범들)
    return 규범들


def 비목_품목금지(cur, 사업명: str | None, 비목들: list[str]) -> list[dict]:
    """비목들 아래 품목 노드에 걸린 금지 규범(근거 포함). 품목 표현이 어느 별칭과도 일치하지 않을 때 LLM 이 대조할 재료다.

    유사 일치를 코드로 하지 않는다(5-6 불변식). 코드 불가 · 조건 문장에 쓰지 않고 판정 문맥에만 넣는다.
    """
    사업들 = [사업명] if 사업명 else []
    행들 = cur.execute(
        f"""WITH RECURSIVE 하위(id, 방문) AS (
             SELECT r.src_id, ARRAY[r.dst_id, r.src_id] FROM corpus.concept_relations r
              WHERE r.관계 = '상위' AND r.dst_id = ANY(%(비목들)s) AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             UNION ALL
             SELECT r.src_id, h.방문 || r.src_id FROM 하위 h JOIN corpus.concept_relations r ON r.dst_id = h.id
              WHERE r.관계 = '상위' AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s)) AND NOT r.src_id = ANY(h.방문))
           SELECT {", ".join(f'n."{c}"' for c in _규범_컬럼)} FROM corpus.norms n
            WHERE n.종류 = '금지' AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%(사업들)s))
              AND n.대상 && (SELECT array_agg(h.id) FROM 하위 h JOIN corpus.concepts c ON c.concept_id = h.id AND c.종류 = '품목')
            ORDER BY n.norm_id""", {"비목들": 비목들, "사업들": 사업들}).fetchall()
    규범들 = [dict(zip(_규범_컬럼, r)) for r in 행들]
    _근거_붙이기(cur, 규범들)
    return 규범들


def 조문_행(cur, 조문들: list[str], 출처: str) -> list[dict]:
    """조문 ID 를 문맥 행으로 바꾼다(`근거_조문` 결과와 같은 모양). 준용을 따라가지 않는다. 활성 문서에 없는 조문은 article_id 가 None 이다."""
    if not 조문들:
        return []
    정보 = _조문_정보(cur, 조문들, _스키마())
    return [{"doc_id": 정보[i]["doc_id"], "조번호": 정보[i]["조번호"], "항호": 정보[i]["항호"], "article_id": 정보[i]["article_id"],
             "깊이": 0, "출처": 출처, "경로": [i], **({"건너뜀": 정보[i]["건너뜀"]} if 정보[i]["건너뜀"] else {})} for i in 조문들]


def 세목_한도_있음(cur, 사업명: str | None, 대상_id: str) -> bool:
    """대상 아래 세목 노드에 한도 규범이 있나. 비목만 확정했을 때 세목을 고르라고 알리는 데 쓴다(품목은 세지 않는다)."""
    사업들 = [사업명] if 사업명 else []
    행 = cur.execute(
        """WITH RECURSIVE 하위(id, 방문) AS (
             SELECT r.src_id, ARRAY[r.dst_id, r.src_id] FROM corpus.concept_relations r
               JOIN corpus.concepts c ON c.concept_id = r.src_id AND c.종류 = '세목'
              WHERE r.관계 = '상위' AND r.dst_id = %(대상)s AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             UNION ALL
             SELECT r.src_id, h.방문 || r.src_id FROM 하위 h JOIN corpus.concept_relations r ON r.dst_id = h.id
               JOIN corpus.concepts c ON c.concept_id = r.src_id AND c.종류 = '세목'
              WHERE r.관계 = '상위' AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s)) AND NOT r.src_id = ANY(h.방문))
           SELECT 1 FROM corpus.norms n
            WHERE n.종류 = '한도' AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%(사업들)s))
              AND n.대상 && (SELECT array_agg(id) FROM 하위) LIMIT 1""", {"대상": 대상_id, "사업들": 사업들}).fetchone()
    return 행 is not None


def 위임_있음(근거행: list[dict]) -> bool:
    """근거 조문 결과에 위임 관계가 있나. 규범 없이 위임만 있는 대상(창업활동자금 등)을 가린다."""
    return any(str(r.get("출처") or "").startswith("위임") for r in 근거행)


def 새경로_대상(cur, 사업명: str | None) -> bool:
    """SUDDOE_ONTOLOGY_PROGRAMS(쉼표 구분) 목록에 있고 corpus.norms 에 그 사업 행이 있으면 참(5-11). 비면 모두 옛 경로."""
    if not 사업명:
        return False
    목록 = [x.strip() for x in os.environ.get("SUDDOE_ONTOLOGY_PROGRAMS", "").split(",") if x.strip()]
    if 사업명 not in 목록:
        return False
    return cur.execute("SELECT 1 FROM corpus.norms WHERE 적용사업 = %s LIMIT 1", (사업명,)).fetchone() is not None


# 능력 질문 테스트 ─────────────────────────────────────────────────────────

_기대_키 = {"규범ID_포함", "규범ID_제외", "제외규범ID_포함", "경로쌍_포함", "경로쌍_제외", "조문ID_포함", "조문ID_제외",
            "조문경로_포함", "노드ID_포함", "노드ID_제외", "노드없음", "문장", "값", "규범값", "예외쌍_포함",
            "L3ID_포함", "L3ID_제외"}


def _칸_값(규범: dict, 경로: str):
    """`수치.값` · `조건.구조화` · `결과.시점` 처럼 점으로 이은 칸의 값. 없으면 None."""
    v = 규범
    for 조각 in 경로.split("."):
        v = v.get(조각) if isinstance(v, dict) else None
    return v


def _값_맞나(실제, 기대값) -> bool:
    """기대값이 {"포함": 글 또는 [글...]} 이면 실제 값(목록 · dict 는 JSON 글)에 그 글이 모두 들어 있나.
    목록은 순서 없이 같은가. 그 밖은 같은가."""
    if isinstance(기대값, dict) and set(기대값) == {"포함"}:
        if 실제 is None:
            return False
        글 = 실제 if isinstance(실제, str) else json.dumps(실제, ensure_ascii=False, sort_keys=True)
        조각들 = 기대값["포함"] if isinstance(기대값["포함"], list) else [기대값["포함"]]
        return all(x in 글 for x in 조각들)
    if isinstance(기대값, list) and isinstance(실제, list):
        def 키(x):
            return json.dumps(x, ensure_ascii=False, sort_keys=True)
        return sorted(map(키, 실제)) == sorted(map(키, 기대값))
    return 실제 == 기대값


def _사례_실행(cur, 사례: dict, schema: dict) -> list[str]:
    """사례 하나를 돌려 틀린 것의 설명 목록을 돌려준다."""
    호출 = 사례.get("호출") or {}
    함수, 인자 = 호출.get("함수"), 호출.get("인자") or {}
    기대 = 사례.get("기대") or {}
    if not 기대 or set(기대) - _기대_키:
        return [f"기대 키가 비었거나 모르는 키다: {sorted(set(기대) - _기대_키)}"]
    틀림: list[str] = []

    def 포함(이름: str, 실제: set) -> None:
        for x in 기대.get(이름, []):
            if x not in 실제:
                틀림.append(f"{이름}: {x} 가 없다")

    def 불포함(이름: str, 실제: set) -> None:
        for x in 기대.get(이름, []):
            if x in 실제:
                틀림.append(f"{이름}: {x} 가 있으면 안 된다")

    if 함수 == "노드_결정":
        # 표현이 목록이면(원문 표기 여럿) 표현마다 같은 기대를 본다
        for 표현 in (인자["표현"] if isinstance(인자["표현"], list) else [인자["표현"]]):
            결과 = 노드_결정(cur, 표현, 인자.get("사업"))
            ids = {r["concept_id"] for r in 결과}
            for x in 기대.get("노드ID_포함", []):
                if x not in ids:
                    틀림.append(f"노드ID_포함: {표현!r} → {x} 가 없다")
            for x in 기대.get("노드ID_제외", []):
                if x in ids:
                    틀림.append(f"노드ID_제외: {표현!r} → {x} 가 있으면 안 된다")
            if 기대.get("노드없음") and 결과:
                틀림.append(f"노드없음: {표현!r} → {sorted(ids)}")
    elif 함수 == "적용_규범":
        결과 = 적용_규범(cur, 인자.get("사업"), 인자["대상_id"])
        ids = {n["norm_id"] for n in 결과["규범"]}
        포함("규범ID_포함", ids)
        불포함("규범ID_제외", ids)
        포함("제외규범ID_포함", {a["제외"] for a in 결과["우선순위_적용"] if a["조건"].get("종류") == "대체"})
        for 쌍 in 기대.get("경로쌍_포함", []):
            if tuple(쌍) not in 결과["경로"]:
                틀림.append(f"경로쌍_포함: {쌍} 가 없다")
        for 쌍 in 기대.get("경로쌍_제외", []):
            if tuple(쌍) in 결과["경로"]:
                틀림.append(f"경로쌍_제외: {쌍} 가 있으면 안 된다")
        # 예외쌍 [원칙, 단서]: 원칙의 `예외` 에 단서가 있고 둘 다 결과에 있어 우선순위_적용 에 예외로 남는다
        예외쌍 = {(a["제외"], a["채택"]) for a in 결과["우선순위_적용"] if a["조건"].get("종류") == "예외"}
        for 원칙, 단서 in 기대.get("예외쌍_포함", []):
            if (원칙, 단서) not in 예외쌍:
                틀림.append(f"예외쌍_포함: {원칙} ← 단서 {단서} 가 없다")
        표 = {n["norm_id"]: n for n in 결과["규범"]}
        for nid, 칸들 in (기대.get("규범값") or {}).items():
            if nid not in 표:
                틀림.append(f"규범값: {nid} 가 결과에 없다")
                continue
            for 칸, 기대값 in 칸들.items():
                실제 = _칸_값(표[nid], 칸)
                if not _값_맞나(실제, 기대값):
                    틀림.append(f"규범값: {nid} {칸} 기대 {기대값!r} 실제 {실제!r}")
    elif 함수 == "문맥_구성":
        # 판정 화면과 같은 길: orchestrate.온톨로지_조회(대상 · 상위 노드 · 자금) → ontology_context.문맥_구성(L3 행 고정)
        import ontology_context
        import orchestrate
        조회 = orchestrate.온톨로지_조회(cur, 인자.get("사업"), 인자["비목"], 인자.get("세목"),
                                 orchestrate.게이트_입력(None, None, None), 확정=True, 품목=인자.get("품목"))
        문맥 = ontology_context.문맥_구성(cur, 근거행=조회["근거행"], 대상노드=조회["노드들"], l3=인자.get("l3") or [],
                                    schema=schema, 자금=조회.get("자금"))
        l3_ids = {i[1] for _, 항목 in 문맥["블록"] for i in 항목 if i[0] == "l3"}
        포함("L3ID_포함", l3_ids)
        불포함("L3ID_제외", l3_ids)
    elif 함수 == "근거_조문":
        적용 = 적용_규범(cur, 인자.get("사업"), 인자["대상_id"])
        결과 = 근거_조문(cur, 적용["규범"], 인자.get("깊이", 3), 인자["대상_id"], 사업=인자.get("사업"))
        키표 = {v: k for k, v in (schema.get("문서키") or {}).items()}
        ids = {_조문ID_만들기(r["doc_id"], r["조번호"], r["항호"], 키표) for r in 결과 if r["article_id"] is not None}
        포함("조문ID_포함", ids)
        불포함("조문ID_제외", ids)
        경로들 = [r["경로"] for r in 결과]
        for 경로 in 기대.get("조문경로_포함", []):
            if 경로 not in 경로들:
                틀림.append(f"조문경로_포함: {경로} 가 없다")
        if "문장" in 기대:
            문장들 = {근거경로_문장(p, schema) for p in 경로들}
            if 기대["문장"] not in 문장들:
                틀림.append(f"문장: {기대['문장']!r} 가 없다. 실제 {sorted(문장들)}")
    elif 함수 == "새경로_대상":
        if 새경로_대상(cur, 인자.get("사업명")) is not bool(기대.get("값")):
            틀림.append(f"값: 기대 {기대.get('값')}")
    else:
        틀림.append(f"모르는 함수: {함수}")
    return 틀림


def CQ_시험(cur, 파일: Path | str, schema: dict | None = None) -> int:
    """능력 질문 파일을 돌린다. 틀린 사례 수를 돌려준다. 사례가 없으면 통과가 아니다."""
    schema = schema or _스키마()
    묶음 = json.loads(Path(파일).read_text(encoding="utf-8"))
    사례들 = 묶음.get("사례") or []
    if not 사례들:
        print("실패: 능력 질문 사례가 없다")
        return 1
    틀림수 = 0
    for 사례 in 사례들:
        틀림 = _사례_실행(cur, 사례, schema)
        if 틀림:
            틀림수 += 1
            print(f"실패 {사례.get('id')}: {사례.get('질문')}")
            for t in 틀림:
                print(f"  {t}")
        else:
            print(f"통과 {사례.get('id')}")
    print(f"능력 질문: {len(사례들) - 틀림수}/{len(사례들)} 통과")
    return 틀림수


def _자체시험() -> int:
    표 = {"문서 표시명": {"지침": "지침", "모두의창업": "모두의 창업"}}
    t: list[tuple[str, bool]] = []
    t.append(("부칙 제1조 ID 왕복",
              _조문ID_만들기("문서", "부칙 제1조", None, {"문서": "지침"}) == "조:지침#부칙 제1조"
              and 조문ID_풀이("조:지침#부칙 제1조")["조번호"] == "부칙 제1조"))
    t.append(("근거 경로에 부칙 제1조로 읽힌다",
              근거경로_문장(["조:지침#부칙 제2조", "미특정:사업별 세부관리기준"], 표) == "지침 부칙 제2조 → 사업별 세부관리기준"))
    t.append(("근거 경로: 부칙만 있는 조, 항까지 있는 부칙",
              근거경로_문장(["조:모두의창업#부칙", "조:지침#부칙 제1조①"], 표) == "모두의 창업 부칙 → 지침 부칙 제1조①"))
    # 조 단위 식은 SQL 이라 Postgres 로 돌린다. 테이블은 읽지 않는다
    사례 = {"조:지침#부칙 제1조": "조:지침#부칙 제1조", "조:지침#부칙 제2조①": "조:지침#부칙 제2조",
          "조:지침#부칙 제1조의2①": "조:지침#부칙 제1조의2", "조:팁스#부칙": "조:팁스#부칙",
          "조:지침#제36조①": "조:지침#제36조", "조:지침#제29조의2②": "조:지침#제29조의2",
          "조:모두의창업#별지2/지급수수료/정의": "조:모두의창업#별지2", "조:안내책자#참고2-p29-1": "조:안내책자#참고2-p29-1",
          "조:집행지침#제2편Ⅱ-12": "조:집행지침#제2편Ⅱ-12", "조:집행지침#제3편": "조:집행지침#제3편", "조:집행지침#표지": "조:집행지침#표지"}
    try:
        from _lib import db
        with db.connect(connect_timeout=3, autocommit=True) as conn:
            식 = _조단위_식.format("%s::text")
            실제 = {k: conn.execute(f"SELECT {식}", (k,)).fetchone()[0] for k in 사례}
        for k, v in 사례.items():
            t.append((f"조 단위 식 {k} → {v}", 실제[k] == v))
        # 품목 · 세목 경로(적재된 온톨로지가 있을 때만). 1-1 · 2-5 · 3-6 · 2-2
        with db.connect(connect_timeout=3, autocommit=True) as conn:
            cur = conn.cursor()
            사업 = "모두의 창업 프로젝트"
            if cur.execute("SELECT 1 FROM corpus.concepts WHERE concept_id = '품목:창업활동자금/키보드@모두의창업'").fetchone():
                재료구입 = 대상_노드(cur, 사업, "창업활동비", "재료구입비")
                t.append(("노드_결정: 사업한정 먼저, 공통도 돌려준다", [r["concept_id"] for r in 노드_결정(cur, "귀금속", 사업)]
                          == ["품목:창업활동자금/귀금속@모두의창업", "품목:창업/귀금속"]))
                t.append(("품목_노드: 공통 비목으로 고르면 공통 품목", 품목_노드(cur, 사업, "귀금속", "비목:창업/재료비") == "품목:창업/귀금속"))
                t.append(("품목_노드: 세목 밖 품목은 시작 노드가 아니다", 품목_노드(cur, 사업, "키보드", 재료구입["대상_id"]) is None))
                밖 = 경로밖_품목(cur, 사업, "키보드", 재료구입["비목_id"])
                t.append(("경로밖_품목: 같은 비목 아래 품목", 밖 == ["품목:창업활동자금/키보드@모두의창업"]))
                t.append(("경로밖_품목: 다른 비목 아래 품목은 넣지 않는다", 경로밖_품목(cur, 사업, "키보드", "비목:창업/기계장치") == []))
                t.append(("경로밖_품목: 모든 비목 아래 품목은 넣는다", 경로밖_품목(cur, 사업, "부채 상환", "비목:창업/재료비") == ["품목:창업/부채상환"]))
                적용 = 적용_규범(cur, 사업, 재료구입["대상_id"])
                t.append(("경로밖_규범: 품목 금지를 가져오고 우선순위 · 이미 있는 규범은 뺀다",
                          (lambda ns: any(n["종류"] == "금지" and "키보드" in str(n["근거"]) for n in ns)
                           and not any(n["종류"] == "우선순위" or n["norm_id"] in {m["norm_id"] for m in 적용["규범"]} for n in ns))
                          (경로밖_규범(cur, 사업, 밖, 적용))))
                수수료 = "비목:창업/지급수수료"
                t.append(("5-14 A 상위_세목: 세목은 그 세목 · 품목은 가장 가까운 상위 세목 · 세목 없는 품목 · 다른 비목 · 비목 자신은 None",
                          상위_세목(cur, 사업, "세목:창업/지급수수료/멘토링비", 수수료) == {"id": "세목:창업/지급수수료/멘토링비", "세목": "멘토링비"}
                          and 상위_세목(cur, 사업, "품목:창업/공유사무공간", 수수료) == {"id": "세목:창업/지급수수료/사무실임차료", "세목": "사무실임차료"}
                          and 상위_세목(cur, 사업, "품목:창업/고영향AI인증검증비", 수수료) is None
                          and 상위_세목(cur, 사업, "세목:창업/지급수수료/멘토링비", "비목:창업/재료비") is None
                          and 상위_세목(cur, 사업, 수수료, 수수료) is None))
                t.append(("세목_한도_있음: 한도 있는 세목이 있는 비목만", 세목_한도_있음(cur, 사업, "비목:창업/지급수수료")
                          and not 세목_한도_있음(cur, 사업, "비목:창업/기계장치") and not 세목_한도_있음(cur, 사업, "비목:창업/재료비")))
                행들 = 근거_조문(cur, 적용_규범(cur, 사업, "비목:창업/재료비")["규범"], 3, "비목:창업/재료비", 사업=사업)
                우선 = [r for r in 행들 if r.get("우선경로")]
                t.append(("근거_조문: 확정 노드에서 시작한 행에만 우선경로 · 공통 행은 없음", bool(우선) and len(우선) < len(행들)
                          and all(r["경로순위"] == 0 for r in 우선 if any("제37조" in p for p in r["우선경로"]))))
                # P5-1 보강 1: 우선순위 규범의 근거 조문은 문맥 후보(규범근거)로만 나오고 준용 사슬은 그대로다
                import ontology_context
                기계 = 적용_규범(cur, 사업, "비목:창업/기계장치")["규범"]
                행들 = 근거_조문(cur, 기계, 3, "비목:창업/기계장치", 사업=사업)
                새행 = [r for r in 행들 if r.get("우선순위근거")]
                옛행 = 근거_조문(cur, [n for n in 기계 if n["종류"] != "우선순위"], 3, "비목:창업/기계장치", 사업=사업)
                t.append(("우선순위 근거: 제4조 · 제33조가 규범근거 행(깊이 0, 우선경로 없음)으로 나온다",
                          {"제4조", "제33조"} <= {r["조번호"] for r in 새행}
                          and all(r["출처"] == "규범근거" and r["깊이"] == 0 and "우선경로" not in r for r in 새행)))
                t.append(("우선순위 근거: 나머지 행은 우선순위 규범을 뺀 결과와 같다(사슬 시작점이 아니다)",
                          [r for r in 행들 if not r.get("우선순위근거")] == 옛행))
                표 = _스키마()

                def 문맥조(rs):
                    m = ontology_context.문맥_구성(cur, 근거행=rs, 대상노드=set(), schema=표, 자금="시제품제작비")
                    return {h.split("(")[0] for _, 묶음 in m["블록"] for _, _, h, _, _ in 묶음}
                t.append(("우선순위 근거 효과: 새 행을 빼면 제4조 · 제33조가 문맥에서 빠진다",
                          {"모두의 창업 제4조", "모두의 창업 제33조"} <= 문맥조(행들)
                          and not ({"모두의 창업 제4조", "모두의 창업 제33조"} & 문맥조(옛행))))
                t.append(("우선순위 근거: 화면 근거 경로는 그대로",
                          ontology_context.근거경로들(행들, 표, 상한=None, 규범들=기계) == ontology_context.근거경로들(옛행, 표, 상한=None, 규범들=기계)))
                import orchestrate
                t.append(("자금 없으면 검색 거르기 없음(옛 경로 · 자금 미정과 같은 검색)", orchestrate.자금_거르기(cur, None) is None
                          and orchestrate.자금_거르기(cur, "시제품제작비") is not None))
                # 리뷰 P5-1 보강 지적 1: 창업활동자금 경로의 공통 우선순위 규범 근거(지침 제47조 · 모두의 창업 제4조)는 B2 가 아니라 B2.6
                창 = orchestrate.온톨로지_조회(cur, 사업, "창업활동비", None, orchestrate.게이트_입력(None, None, None), 확정=True)

                def 블록조(rs):
                    m = ontology_context.문맥_구성(cur, 근거행=rs, 대상노드=창["노드들"], schema=표, 자금=창["자금"])
                    return {(제목[:5], h.split("(")[0]) for 제목, 묶음 in m["블록"] for _, _, h, _, _ in 묶음}
                창조 = 블록조(창["근거행"])
                옛창 = 블록조([{**r, "공통미정": False} if r.get("우선순위근거") else r for r in 창["근거행"]])
                t.append(("창업활동자금: 공통 우선순위 근거 조는 B2.6 에만 있고 B2 에 없다(표시를 떼면 B2 로 올라간다)",
                          {("B2.6.", "지침 제47조"), ("B2.6.", "모두의 창업 제4조")} <= 창조
                          and not {x for x in 창조 if x[0] == "B2. 이" and x[1] in ("지침 제47조", "모두의 창업 제4조")}
                          and ("B2. 이", "지침 제47조") in 옛창))
                t.append(("창업활동자금: 공통 우선순위 규범은 게이트에 그대로 쓴다(L3 적용 순서)",
                          any(n["종류"] == "우선순위" for n in 창["적용"]["규범"]) and 창["게이트"]["상한"] == "조건부"))
    except Exception as e:  # noqa: BLE001 - CI 에는 DB 가 없다
        print(f"건너뜀: 조 단위 식(DB 없음: {type(e).__name__})")
    실패 = [x for x, ok in t if not ok]
    for x, ok in t:
        print("ok  " if ok else "FAIL", x)
    print(f"selftest: {len(t) - len(실패)}/{len(t)} 통과")
    return 1 if 실패 else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="온톨로지 조회 · 능력 질문 테스트")
    p.add_argument("--cq-test", help="능력 질문 JSON 경로")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _자체시험()
    if not a.cq_test:
        p.error("--cq-test 또는 --self-test 가 필요하다")
    from _lib import db
    with db.connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            return 1 if CQ_시험(cur, a.cq_test) else 0


if __name__ == "__main__":
    sys.exit(main())
