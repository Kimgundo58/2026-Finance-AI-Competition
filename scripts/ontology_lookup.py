# -*- coding: utf-8 -*-
"""온톨로지 조회 함수(읽기 전용, docs/5_룰/5-8_저장과_조회.md). 판정 · 질문 생성의 새 경로가 쓴다.

노드_결정 · 적용_규범 · 근거_조문 · 새경로_대상 · 근거경로_문장. 쓰지 않는다. 어떤 예외도 호출자에게 그대로 올린다
(호출자 orchestrate 가 판단불가로 바꾼다). 없는 노드 ID 는 LookupError 다.
L3 는 여기서 다루지 않는다. 읽는 테이블: corpus.concepts · concept_labels · concept_relations · norms · provenance ·
documents · doc_articles.

    python scripts/ontology_lookup.py --cq-test ontology/테스트/능력질문.json     # 전부 일치해야 0 으로 끝난다(DB 필요)
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

from ontology_norm import 조문ID_풀이  # noqa: E402
from rule_lookup import _norm  # noqa: E402

모든비목 = "비목:창업/*"
준용_관계 = ("준용", "위임")
_스키마_경로 = ROOT / "ontology" / "schema.json"

# 조문 ID 에서 항 · 호 · 표 · 별지 행을 뗀 조 단위 ID. 조 전체를 준용한 관계가 그 조의 항에도 걸리게 한다.
_조단위_식 = (r"regexp_replace({0}, '^(조:[^#]+#(?:제[0-9]+조(?:의[0-9]+)?|(?:별지|별표|붙임|부칙)\s*[0-9]*"
             r"|(?:참고[0-9]+-)?p[0-9]+-[0-9]+)).*$', '\1')")


@functools.lru_cache(maxsize=1)
def _스키마() -> dict:
    """ontology/schema.json. 문서키 ↔ doc_id 표의 기준이다. 없으면 예외(호출자가 판단불가로 바꾼다)."""
    return json.loads(_스키마_경로.read_text(encoding="utf-8"))


def 노드_결정(cur, 표현: str, 사업: str | None) -> list[dict]:
    """rule_lookup._norm 으로 정규화 → 별칭 정확 일치. 사업한정이 있으면 그것만, 없으면 공통. 없으면 빈 목록."""
    키 = _norm(표현 or "")
    if not 키:
        return []
    행들 = cur.execute(
        """SELECT c.concept_id, c.종류, c.pref_label, c.정의, c.적용사업, c.검수단계, l.적용사업 IS NULL AS 공통
             FROM corpus.concept_labels l JOIN corpus.concepts c USING (concept_id)
            WHERE l.정규화 = %s AND (l.적용사업 IS NULL OR l.적용사업 = %s)
            ORDER BY 공통, c.concept_id""", (키, 사업)).fetchall()
    사업한정 = [r for r in 행들 if not r[6]]
    return [{"concept_id": r[0], "종류": r[1], "pref_label": r[2], "정의": r[3], "적용사업": r[4], "검수단계": r[5]}
            for r in (사업한정 or 행들)]


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


def 근거_조문(cur, 규범들: list[dict], 깊이: int = 3, 대상_id: str | None = None, *, 사업: str | None = None) -> list[dict]:
    """시작 집합 = 규범 근거 + 대상 · 상위 노드의 개념 근거. 나가는 준용 · 위임을 깊이까지 따라간다.

    시작 조문을 dst 로 갖는 준용 · 위임의 src 는 경로 앞에 붙이되 그 src 의 다른 dst 로는 퍼지지 않는다.
    반환 [{doc_id, 조번호, 항호, article_id, 깊이, 출처, 경로:[조문 ID...]}]. 출처는 규범근거 · 개념근거 · 준용 · 위임 ·
    준용_앞 · 위임_앞. 활성 문서에 없거나 조문을 특정하지 못한 대상은 article_id 가 None 이고 `건너뜀` 에 사유가 있다
    (호출자는 article_id 가 있는 행만 문맥에 넣는다). 경로에는 그대로 남는다.
    관계의 적용사업은 사업 또는 규범들의 적용사업으로 거른다. 둘 다 없으면 공통 관계만 본다.
    """
    schema = _스키마()
    키표 = {v: k for k, v in (schema.get("문서키") or {}).items()}
    사업들 = sorted({n["적용사업"] for n in 규범들 if n.get("적용사업")} | ({사업} if 사업 else set()))

    시작: dict[str, str] = {}                       # 조문 ID → 출처
    건너뛴_시작: list[dict] = []

    def 시작_넣기(doc_id: str, 조번호: str, 항호: str | None, 출처: str) -> None:
        i = _조문ID_만들기(doc_id, 조번호, 항호, 키표)
        if i is None:
            건너뛴_시작.append({"doc_id": doc_id, "조번호": 조번호, "항호": 항호, "article_id": None, "깊이": 0,
                             "출처": 출처, "경로": [f"{doc_id}#{조번호}{항호 or ''}"], "건너뜀": "문서키 없음"})
        else:
            시작.setdefault(i, 출처)

    for n in 규범들:
        for g in n.get("근거") or []:
            시작_넣기(g["doc_id"], g["조번호"], g["항호"], "규범근거")
    if 대상_id:
        _노드_확인(cur, 대상_id)
        노드들 = [대상_id, *{p for _, p in _상위_경로(cur, 대상_id, 사업들)}]
        for r in cur.execute(
                """SELECT DISTINCT doc_id, 조번호, 항호 FROM corpus.provenance
                    WHERE owner_kind = '개념' AND owner_id = ANY(%s)
                    ORDER BY doc_id, 조번호, 항호""", (노드들,)).fetchall():
            시작_넣기(r[0], r[1], r[2], "개념근거")

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

    조문들 = set(시작목록) | set(앞_출처) | {r[1] for r in 사슬 if r[1].startswith("조:")}
    정보 = _조문_정보(cur, sorted(조문들), schema)

    def 앞_경로(s: str) -> list[str]:
        return [앞[s], s] if s in 앞 else [s]

    결과: dict[str, dict] = {}

    def 넣기(i: str, 깊이값: int, 출처: str, 경로: list[str]) -> None:
        if i in 결과:
            # 시작 조문이 사슬로도 닿으면 깊이 · 출처는 그대로 두고 화면 "근거 경로" 에 더 긴 경로를 쓴다
            if len(경로) > len(결과[i]["경로"]):
                결과[i]["경로"] = 경로
            return
        v = 정보[i]
        행 = {"doc_id": v["doc_id"], "조번호": v["조번호"], "항호": v["항호"], "article_id": v["article_id"],
             "깊이": 깊이값, "출처": 출처, "경로": 경로}
        if v["건너뜀"]:
            행["건너뜀"] = v["건너뜀"]
        결과[i] = 행

    for s in 시작목록:
        넣기(s, 0, 시작[s], 앞_경로(s))
    for src, 출처 in sorted(앞_출처.items()):
        넣기(src, 0, 출처, [src])
    끝: list[dict] = []
    for 시작id, dst, 관계, d, 경로 in 사슬:
        전체경로 = ([앞[시작id]] if 시작id in 앞 else []) + list(경로)
        if dst.startswith("조:"):
            넣기(dst, d, 관계, 전체경로)
        else:                                       # 미특정 · 행위자(위임). 조문이 아니라 문맥에 넣지 못한다
            끝.append({"doc_id": None, "조번호": None, "항호": None, "article_id": None, "깊이": d, "출처": 관계,
                      "경로": 전체경로, "건너뜀": "조문 미특정" if dst.startswith("미특정") else "조문 아님"})
    return [*건너뛴_시작, *결과.values(), *끝]


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
            글 = f"{이름} {풀이['조번호']}" + (" " + 항호.replace("/", " ").strip() if 항호.startswith("/") else 항호)
        elif i.startswith("미특정:"):
            글 = f"{i.split(':', 1)[1]}(조문 미특정)"
        else:
            continue
        if not 조각 or 조각[-1] != 글:
            조각.append(글)
    return " → ".join(조각)


def 레이블_목록(cur, 사업명: str | None) -> list[dict]:
    """질의 변환 · 분류 확인 화면에 쓰는 분류 목록 [{표시문, 비목, 세목, 별칭:[]}]. 내부 ID 를 넣지 않는다.

    사업이 `사용` 관계로 쓰는 비목과 그 아래 세목만 낸다. 사업키를 모르면 빈 목록이다.
    """
    사업키 = next((k for k, v in (_스키마().get("사업키") or {}).items() if v == 사업명), None)
    if not 사업키:
        return []
    사업들 = [사업명]
    행들 = cur.execute(
        """SELECT b.concept_id, b.pref_label, NULL::text, NULL::text
             FROM corpus.concept_relations r JOIN corpus.concepts b ON b.concept_id = r.dst_id
            WHERE r.관계 = '사용' AND r.src_id = %(사업id)s AND b.종류 = '비목'
              AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
           UNION ALL
           SELECT s.concept_id, b.pref_label, s.pref_label, b.concept_id
             FROM corpus.concept_relations r
             JOIN corpus.concepts b ON b.concept_id = r.dst_id
             JOIN corpus.concept_relations h ON h.관계 = '상위' AND h.dst_id = b.concept_id
                  AND (h.적용사업 IS NULL OR h.적용사업 = ANY(%(사업들)s))
             JOIN corpus.concepts s ON s.concept_id = h.src_id AND s.종류 = '세목'
            WHERE r.관계 = '사용' AND r.src_id = %(사업id)s AND b.종류 = '비목'
              AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
            ORDER BY 2, 3 NULLS FIRST""", {"사업id": f"사업:{사업키}", "사업들": 사업들}).fetchall()
    별칭: dict[str, list[str]] = {}
    for cid, 글 in cur.execute("""SELECT concept_id, alt_label FROM corpus.concept_labels
                                  WHERE concept_id = ANY(%s) AND (적용사업 IS NULL OR 적용사업 = %s)
                                  ORDER BY concept_id, alt_label""", ([r[0] for r in 행들], 사업명)).fetchall():
        별칭.setdefault(cid, []).append(글)
    return [{"표시문": f"{r[1]} > {r[2]}" if r[2] else r[1], "비목": r[1], "세목": r[2], "별칭": 별칭.get(r[0], []),
             "노드": r[0]} for r in 행들]


def 별칭_후보(cur, 표현: str | None, 사업명: str | None, 레이블: list[dict]) -> list[dict]:
    """품목 표현의 별칭 정확 일치 → 그 노드(품목이면 가장 가까운 상위 세목 · 비목)의 분류 후보. 없으면 빈 목록."""
    표 = {x["노드"]: x for x in 레이블}
    사업들 = [사업명] if 사업명 else []
    out: list[dict] = []
    for 행 in 노드_결정(cur, 표현 or "", 사업명):
        차례 = [행["concept_id"], *(p for _, p in _상위_경로(cur, 행["concept_id"], 사업들))]
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


def 하위_제약_있음(cur, 사업명: str | None, 대상_id: str) -> bool:
    """대상 아래 노드에 금지 · 한도 · 임계값 규범이 있나. 비목만 확정했을 때 세목을 고르라고 알리는 데 쓴다."""
    사업들 = [사업명] if 사업명 else []
    행 = cur.execute(
        """WITH RECURSIVE 하위(id, 방문) AS (
             SELECT r.src_id, ARRAY[r.dst_id, r.src_id] FROM corpus.concept_relations r
              WHERE r.관계 = '상위' AND r.dst_id = %(대상)s AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s))
             UNION ALL
             SELECT r.src_id, h.방문 || r.src_id FROM 하위 h JOIN corpus.concept_relations r ON r.dst_id = h.id
              WHERE r.관계 = '상위' AND (r.적용사업 IS NULL OR r.적용사업 = ANY(%(사업들)s)) AND NOT r.src_id = ANY(h.방문))
           SELECT 1 FROM corpus.norms n
            WHERE n.종류 IN ('금지', '한도', '임계값') AND (n.적용사업 IS NULL OR n.적용사업 = ANY(%(사업들)s))
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

_기대_키 = {"규범ID_포함", "규범ID_제외", "제외규범ID_포함", "경로쌍_포함", "조문ID_포함", "조문ID_제외", "조문경로_포함",
            "노드ID_포함", "노드없음", "문장", "값"}


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
        결과 = 노드_결정(cur, 인자["표현"], 인자.get("사업"))
        포함("노드ID_포함", {r["concept_id"] for r in 결과})
        if 기대.get("노드없음") and 결과:
            틀림.append(f"노드없음: {[r['concept_id'] for r in 결과]}")
    elif 함수 == "적용_규범":
        결과 = 적용_규범(cur, 인자.get("사업"), 인자["대상_id"])
        ids = {n["norm_id"] for n in 결과["규범"]}
        포함("규범ID_포함", ids)
        불포함("규범ID_제외", ids)
        포함("제외규범ID_포함", {a["제외"] for a in 결과["우선순위_적용"] if a["조건"].get("종류") == "대체"})
        for 쌍 in 기대.get("경로쌍_포함", []):
            if tuple(쌍) not in 결과["경로"]:
                틀림.append(f"경로쌍_포함: {쌍} 가 없다")
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="온톨로지 조회 · 능력 질문 테스트")
    p.add_argument("--cq-test", required=True, help="능력 질문 JSON 경로")
    a = p.parse_args(argv)
    from _lib import db
    with db.connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            return 1 if CQ_시험(cur, a.cq_test) else 0


if __name__ == "__main__":
    sys.exit(main())
