# -*- coding: utf-8 -*-
"""온톨로지 원천 JSON → corpus.concepts · concept_labels · concept_relations · norms · provenance 적재 (docs/5_룰/5-8_저장과_조회.md).
용어 풀이(ontology/용어풀이.json → corpus.terminology, docs/6_LLM/6-5_용어_풀이.md)도 같은 트랜잭션 · 같은 범위로 넣는다(scripts/ontology_terms.py).

순서: 원천 읽기 → ontology_check(코드 검사 + OWL + HermiT, DB 대조 포함) → 트랜잭션 안에서 범위별 upsert · 없는 행 삭제 →
참조 무결성 확인 → l3_articles.concept_ids 의 없는 ID 정리 → 판정 캐시 삭제 → 커밋 → 적재 로그. 실패하면 적재 전 상태다.
범위는 공통(적용사업 NULL)과 사업이다. 공통 행은 공통_L1.json 과 사업 파일의 적용사업 null 행을 합친 것이라, 항상 모든 파일을
읽고 검사한다. 사업키 인자는 쓰는 사업만 고른다(공통 범위는 언제나 쓴다).
개정 활성화(corpus_ingest._재검토_하향)로 `재검토` 가 된 행은 원천 행이 그대로면 재검토로 둔다(_재검토_유지).
쓰기 DSN 은 SUDDOE_INGEST_DSN(롤 suddoe_ingest), 없으면 scripts/_lib/db.py 의 로컬 기본 DSN 이다. Java 가 있어야 한다.

    python scripts/ontology_load.py --dry            # diff 만 출력한다
    python scripts/ontology_load.py                  # 전체 적재
    python scripts/ontology_load.py 모두의창업        # 공통 + 그 사업
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db, paths  # noqa: E402

ROOT = paths.ensure_on_path()

import ontology_terms  # noqa: E402
import ontology_age  # noqa: E402
from ontology_check import DB_원문, 검사오류, 복원본_읽기, 원천_기본폴더, 원천_읽기, 전체_검사, 행_해시, _목록, _원본  # noqa: E402
from ontology_norm import 모든비목, 조문ID_풀이  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from rule_lookup import _norm  # noqa: E402

_보기_상한 = 40


class 적재실패(Exception):
    """트랜잭션을 되돌려야 하는 실패."""


def 관계_소유자ID(src: str, 관계: str, dst: str, 적용사업: str | None) -> str:
    """관계에는 PK 가 없어 provenance.owner_id 를 이렇게 만든다."""
    return f"{src}|{관계}|{dst}|{적용사업 or '공통'}"


# 원천 → DB 행 ──────────────────────────────────────────────────────────────

def DB행_만들기(펼침: dict, schema: dict) -> dict:
    """펼친 원천 → 테이블별 행. 키는 범위(적용사업, None = 공통)다."""
    문서키 = schema.get("문서키") or {}
    out = {"개념": {}, "라벨": {}, "관계": {}, "규범": {}, "근거": {}}

    def 근거행(소유자: str, 종류: str, 칸: str, 근거: list) -> list[tuple]:
        행들 = []
        for g in _목록(근거):
            풀이 = 조문ID_풀이(g["조문"])
            행들.append((소유자, 종류, 칸, 문서키[풀이["문서키"]], 풀이["조번호"], 풀이["항호"], g["발췌"]))
        return 행들

    for c in 펼침["개념"]:
        i = c["id"]
        if i in out["개념"]:
            continue                                    # 같은 내용이 두 파일에 있다(검사 통과)
        사업 = c.get("적용사업")
        out["개념"][i] = {"concept_id": i, "종류": c["종류"], "pref_label": c["정식명"], "정의": c.get("정의"),
                        "적용사업": 사업, "검수단계": c["검수단계"], "원천해시": 행_해시(_원본(c))}
        본것: dict[str, str] = {}
        for 표현 in [c["정식명"], *c["별칭"]]:
            본것.setdefault(_norm(str(표현)), str(표현))     # 정식명도 조회 표현이다
        for 키, 표현 in 본것.items():
            out["라벨"][(키, 사업)] = {"concept_id": i, "alt_label": 표현, "정규화": 키, "적용사업": 사업}
        out["근거"].setdefault(사업, []).extend(근거행(i, "개념", "근거", c.get("근거")))

    for r in 펼침["관계"]:
        사업 = r.get("적용사업")
        키 = (r["src"], r["관계"], r["dst"], 사업)
        문맥 = None
        if r.get("문맥조문") or r.get("적용조문"):
            문맥 = {"문맥조문": r.get("문맥조문"), "적용조문": _목록(r.get("적용조문"))}
        행 = {"src_id": r["src"], "관계": r["관계"], "dst_id": r["dst"], "적용사업": 사업,
             "연결종류": r.get("연결종류"), "문맥": 문맥, "검수단계": r.get("검수단계") or "AI초안"}
        if 키 not in out["관계"] or not r.get("_개념에서"):
            out["관계"][키] = 행                           # 개념.상위 와 관계 행이 겹치면 관계 행이 이긴다
        if not r.get("_개념에서"):
            out["근거"].setdefault(사업, []).extend(
                근거행(관계_소유자ID(r["src"], r["관계"], r["dst"], 사업), "관계", "근거", r.get("근거")))

    for n in 펼침["규범"]:
        i = n["id"]
        if i in out["규범"]:
            continue
        사업 = n.get("적용사업")
        결과 = dict(n.get("결과") or {})
        for 칸 in ("시점", "증빙"):                       # 5-8b: 시점 · 증빙은 결과 JSONB 안에 둔다
            if n.get(칸) is not None:
                결과[칸] = n[칸]
        out["규범"][i] = {"norm_id": i, "종류": n["종류"], "계층": n["계층"], "적용사업": 사업,
                        "대상": _목록(n.get("대상")), "수범자": _목록(n.get("수범자")), "승인주체": _목록(n.get("승인주체")),
                        "조건": n.get("조건"), "수치": n.get("수치"), "결과": 결과 or None,
                        "예외": [str(x) for x in _목록(n.get("예외"))], "해석미결": bool(n.get("해석미결")),
                        "검수단계": n["검수단계"], "원천해시": 행_해시(_원본(n))}
        for 칸, 목록 in (n.get("근거") or {}).items():
            out["근거"].setdefault(사업, []).extend(근거행(i, "규범", 칸, 목록))
    return out


def _범위별(행들: dict, 범위: list) -> dict:
    return {s: {k: v for k, v in 행들.items() if v["적용사업"] == s} for s in 범위}


# 현재 DB 행 ────────────────────────────────────────────────────────────────

def _현재(cur, 범위: list) -> dict:
    사업들 = [s for s in 범위 if s is not None]
    공통포함 = None in 범위
    조건 = "(적용사업 = ANY(%s) OR (%s AND 적용사업 IS NULL))"
    인자 = (사업들, 공통포함)

    def 읽기(sql: str) -> list[tuple]:
        return cur.execute(sql.format(조건=조건), 인자).fetchall()

    개념행 = 읽기("SELECT concept_id, 원천해시, 검수단계 FROM corpus.concepts WHERE {조건}")
    규범행 = 읽기("SELECT norm_id, 원천해시, 검수단계 FROM corpus.norms WHERE {조건}")
    개념 = {r[0]: r[1] for r in 개념행}
    규범 = {r[0]: r[1] for r in 규범행}
    재검토 = {"개념": {r[0] for r in 개념행 if r[2] == "재검토"}, "규범": {r[0] for r in 규범행 if r[2] == "재검토"}}
    라벨 = {(r[0], r[1]): r[2] for r in 읽기("SELECT 정규화, 적용사업, concept_id || '|' || alt_label FROM corpus.concept_labels WHERE {조건}")}
    관계 = {(r[0], r[1], r[2], r[3]): (r[4], json.dumps(r[5], ensure_ascii=False, sort_keys=True) if r[5] is not None else None, r[6])
          for r in 읽기("SELECT src_id, 관계, dst_id, 적용사업, 연결종류, 문맥, 검수단계 FROM corpus.concept_relations WHERE {조건}")}
    소유자 = list(개념) + list(규범) + [관계_소유자ID(k[0], k[1], k[2], k[3]) for k in 관계]
    근거 = {tuple(r) for r in cur.execute(
        "SELECT owner_id, owner_kind, 칸, doc_id, 조번호, 항호, 발췌 FROM corpus.provenance WHERE owner_id = ANY(%s)", (소유자,)).fetchall()}
    return {"개념": 개념, "규범": 규범, "라벨": 라벨, "관계": 관계, "근거": 근거, "소유자": 소유자, "재검토": 재검토}


def _재검토_유지(행: dict, 현재: dict, 범위: list) -> dict[str, set[str]]:
    """개정 활성화로 `재검토` 가 된 행 중 원천 행이 그대로인 것(원천해시 같음). 원천의 검수단계로 되돌리지 않는다.

    원천에는 활성화 전 값(AI검증 등)이 그대로 있다. schema.json 문서키만 바꿔 다시 적재한다고 새 판과 대조한 것이 아니므로
    verified 를 돌려주지 않는다. 대조해서 원천 행을 고치면 해시가 달라져 원천 값을 쓴다.
    """
    return {표: {k for k, v in 행[표].items()
                if v["적용사업"] in 범위 and k in 현재["재검토"][표] and 현재[표].get(k) == v["원천해시"]}
            for 표 in ("개념", "규범")}


def _차이(새것: dict, 옛것: dict) -> dict:
    return {"추가": sorted(map(str, 새것.keys() - 옛것.keys())),
            "변경": sorted(str(k) for k in 새것.keys() & 옛것.keys() if 새것[k] != 옛것[k]),
            "삭제": sorted(map(str, 옛것.keys() - 새것.keys())),
            "동일": sum(1 for k in 새것.keys() & 옛것.keys() if 새것[k] == 옛것[k])}


def 차이_계산(행: dict, 현재: dict, 범위: list) -> dict:
    새개념 = {k: v["원천해시"] for k, v in 행["개념"].items() if v["적용사업"] in 범위}
    새규범 = {k: v["원천해시"] for k, v in 행["규범"].items() if v["적용사업"] in 범위}
    새라벨 = {k: f"{v['concept_id']}|{v['alt_label']}" for k, v in 행["라벨"].items() if v["적용사업"] in 범위}
    새관계 = {k: (v["연결종류"], json.dumps(v["문맥"], ensure_ascii=False, sort_keys=True) if v["문맥"] is not None else None, v["검수단계"])
           for k, v in 행["관계"].items() if v["적용사업"] in 범위}
    새근거 = {t for s in 범위 for t in 행["근거"].get(s, [])}
    return {"개념": _차이(새개념, 현재["개념"]), "라벨": _차이(새라벨, 현재["라벨"]),
            "관계": _차이(새관계, 현재["관계"]), "규범": _차이(새규범, 현재["규범"]),
            "근거": {"추가": sorted(map(str, 새근거 - 현재["근거"])), "변경": [],
                   "삭제": sorted(map(str, 현재["근거"] - 새근거)), "동일": len(새근거 & 현재["근거"])}}


def _변경있음(차이: dict) -> bool:
    return any(d["추가"] or d["변경"] or d["삭제"] for d in 차이.values())


def 차이_출력(차이: dict) -> None:
    for 표, d in 차이.items():
        print(f"{표}: 추가 {len(d['추가'])} · 변경 {len(d['변경'])} · 삭제 {len(d['삭제'])} · 동일 {d['동일']}")
        for 종류 in ("추가", "변경", "삭제"):
            for x in d[종류][:_보기_상한]:
                print(f"  {종류} {x}")
            if len(d[종류]) > _보기_상한:
                print(f"  {종류} … 외 {len(d[종류]) - _보기_상한}건")


# 쓰기 ─────────────────────────────────────────────────────────────────────

def _쓰기(cur, 행: dict, 현재: dict, 범위: list, 유지: dict[str, set[str]]) -> None:
    개념 = [{**v, "검수단계": "재검토"} if k in 유지["개념"] else v for k, v in 행["개념"].items() if v["적용사업"] in 범위]
    규범 = [{**v, "검수단계": "재검토"} if k in 유지["규범"] else v for k, v in 행["규범"].items() if v["적용사업"] in 범위]
    관계 = [v for v in 행["관계"].values() if v["적용사업"] in 범위]
    라벨 = [v for v in 행["라벨"].values() if v["적용사업"] in 범위]
    사업들 = [s for s in 범위 if s is not None]
    공통 = None in 범위
    범위식 = "(적용사업 = ANY(%s) OR (%s AND 적용사업 IS NULL))"

    # 근거 · 라벨은 자식 행이라 범위째 지우고 다시 넣는다. 트랜잭션이라 읽는 쪽은 중간 상태를 보지 않는다.
    cur.execute("DELETE FROM corpus.provenance WHERE owner_id = ANY(%s)", (현재["소유자"],))
    cur.execute(f"DELETE FROM corpus.concept_labels WHERE {범위식}", (사업들, 공통))
    cur.execute(f"DELETE FROM corpus.concepts WHERE {범위식} AND NOT concept_id = ANY(%s)",
                (사업들, 공통, [v["concept_id"] for v in 개념]))
    cur.execute(f"DELETE FROM corpus.norms WHERE {범위식} AND NOT norm_id = ANY(%s)",
                (사업들, 공통, [v["norm_id"] for v in 규범]))
    새키 = {(v["src_id"], v["관계"], v["dst_id"], v["적용사업"]) for v in 관계}
    for 키 in 현재["관계"].keys() - 새키:
        cur.execute("DELETE FROM corpus.concept_relations WHERE src_id=%s AND 관계=%s AND dst_id=%s "
                    "AND 적용사업 IS NOT DISTINCT FROM %s", 키)

    cur.executemany(
        """INSERT INTO corpus.concepts (concept_id, 종류, pref_label, 정의, 적용사업, 검수단계, 원천해시)
           VALUES (%(concept_id)s, %(종류)s, %(pref_label)s, %(정의)s, %(적용사업)s, %(검수단계)s, %(원천해시)s)
           ON CONFLICT (concept_id) DO UPDATE SET 종류 = EXCLUDED.종류, pref_label = EXCLUDED.pref_label,
             정의 = EXCLUDED.정의, 적용사업 = EXCLUDED.적용사업, 검수단계 = EXCLUDED.검수단계, 원천해시 = EXCLUDED.원천해시""", 개념)
    cur.executemany(
        """INSERT INTO corpus.concept_labels (concept_id, alt_label, 정규화, 적용사업)
           VALUES (%(concept_id)s, %(alt_label)s, %(정규화)s, %(적용사업)s)""", 라벨)
    cur.executemany(
        """INSERT INTO corpus.concept_relations (src_id, 관계, dst_id, 적용사업, 연결종류, 문맥, 검수단계)
           VALUES (%(src_id)s, %(관계)s, %(dst_id)s, %(적용사업)s, %(연결종류)s, %(문맥)s, %(검수단계)s)
           ON CONFLICT (src_id, 관계, dst_id, 적용사업) DO UPDATE SET
             연결종류 = EXCLUDED.연결종류, 문맥 = EXCLUDED.문맥, 검수단계 = EXCLUDED.검수단계""",
        [{**v, "문맥": Jsonb(v["문맥"]) if v["문맥"] is not None else None} for v in 관계])
    cur.executemany(
        """INSERT INTO corpus.norms (norm_id, 종류, 계층, 적용사업, 대상, 수범자, 승인주체, 조건, 수치, 결과, 예외, 해석미결, 검수단계, 원천해시)
           VALUES (%(norm_id)s, %(종류)s, %(계층)s, %(적용사업)s, %(대상)s, %(수범자)s, %(승인주체)s, %(조건)s, %(수치)s,
                   %(결과)s, %(예외)s, %(해석미결)s, %(검수단계)s, %(원천해시)s)
           ON CONFLICT (norm_id) DO UPDATE SET 종류 = EXCLUDED.종류, 계층 = EXCLUDED.계층, 적용사업 = EXCLUDED.적용사업,
             대상 = EXCLUDED.대상, 수범자 = EXCLUDED.수범자, 승인주체 = EXCLUDED.승인주체, 조건 = EXCLUDED.조건,
             수치 = EXCLUDED.수치, 결과 = EXCLUDED.결과, 예외 = EXCLUDED.예외, 해석미결 = EXCLUDED.해석미결,
             검수단계 = EXCLUDED.검수단계, 원천해시 = EXCLUDED.원천해시""",
        [{**v, **{칸: Jsonb(v[칸]) if v[칸] is not None else None for 칸 in ("조건", "수치", "결과")}} for v in 규범])
    근거 = sorted({t for s in 범위 for t in 행["근거"].get(s, [])}, key=str)
    cur.executemany(
        """INSERT INTO corpus.provenance (owner_id, owner_kind, 칸, doc_id, 조번호, 항호, 발췌)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""", 근거)


def _참조_확인(cur) -> None:
    """적재 범위 밖의 행이 지운 개념을 가리키면 되돌린다(사업 하나만 적재했는데 공통 개념이 사라진 경우)."""
    없는것 = cur.execute(
        r"""SELECT '관계 ' || src_id || ' -' || 관계 || '-> ' || dst_id, x
              FROM corpus.concept_relations r, LATERAL unnest(ARRAY[r.src_id, r.dst_id]) AS x
             WHERE x !~ '^(조:|미특정|사업:)'
               AND NOT EXISTS (SELECT 1 FROM corpus.concepts c WHERE c.concept_id = x)
            UNION ALL
            SELECT '규범 ' || norm_id, x
              FROM corpus.norms n, LATERAL unnest(n.대상 || coalesce(n.수범자, '{}') || coalesce(n.승인주체, '{}')) AS x
             WHERE x <> %s AND NOT EXISTS (SELECT 1 FROM corpus.concepts c WHERE c.concept_id = x)
            UNION ALL
            SELECT '규범 ' || norm_id, x
              FROM corpus.norms n, LATERAL unnest(coalesce(n.예외, '{}')) AS x
             WHERE x LIKE '규범:%%' AND NOT EXISTS (SELECT 1 FROM corpus.norms m WHERE m.norm_id = x)""",
        (모든비목,)).fetchall()
    if 없는것:
        raise 적재실패("적재 범위 밖의 행이 없는 ID 를 가리킨다: " + "; ".join(f"{a} → {b}" for a, b in 없는것[:10]))


def _l3_정리(cur) -> list[int]:
    """l3_articles.concept_ids 에서 없는 개념 ID 를 지운다. 비면 NULL(미확인)로 돌린다.

    '{}' 는 "사용자가 확인했고 해당 세목 없음" 이라, 개념이 사라져 빈 것을 그렇게 두면 하지 않은 확인을 한 것으로 보인다.
    """
    return [r[0] for r in cur.execute(
        """UPDATE tenant.l3_articles a
              SET concept_ids = (SELECT array_agg(x ORDER BY ord)
                                   FROM unnest(a.concept_ids) WITH ORDINALITY AS u(x, ord)
                                  WHERE EXISTS (SELECT 1 FROM corpus.concepts c WHERE c.concept_id = u.x))
            WHERE a.concept_ids IS NOT NULL AND cardinality(a.concept_ids) > 0
              AND EXISTS (SELECT 1 FROM unnest(a.concept_ids) AS y
                           WHERE NOT EXISTS (SELECT 1 FROM corpus.concepts c WHERE c.concept_id = y))
            RETURNING a.article_id""").fetchall()]


def _데이터버전(cur) -> str:
    """서버가 판정 캐시 적중을 가르는 그 값. 같은 SQL · 같은 함수를 쓴다."""
    from server.response_cache import 데이터버전_문자열, _데이터버전_해시_SQL
    return 데이터버전_문자열(cur.execute(_데이터버전_해시_SQL).fetchone())


def _행수(cur) -> dict:
    return {t: cur.execute(f"SELECT count(*) FROM corpus.{t}").fetchone()[0]
            for t in ("concepts", "concept_labels", "concept_relations", "norms", "provenance", "terminology")}


def 로그_쓰기(폴더: Path, 내용: dict) -> Path:
    경로 = Path(폴더) / "적재로그" / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    경로.parent.mkdir(parents=True, exist_ok=True)
    경로.write_text(json.dumps(내용, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 경로


def 적재(폴더: Path | str, 사업키들: list[str] | None, dry: bool, dsn: str) -> int:
    폴더 = Path(폴더)
    try:
        원천 = 원천_읽기(폴더, None)
        용어원천 = ontology_terms.원천_읽기(폴더)
    except (검사오류, ValueError) as e:
        print(f"오류: {e}")
        return 1
    사업표 = 원천["schema"].get("사업키") or {}
    쓰는사업 = list(원천["사업"])
    if 사업키들:
        쓰는사업 = []
        for k in 사업키들:
            사업 = 사업표.get(k)
            if 사업 not in 원천["사업"]:
                print(f"오류: 원천 파일이 없는 사업키: {k}")
                return 1
            쓰는사업.append(사업)
    범위 = [None, *쓰는사업]

    with db.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE corpus.documents IN SHARE MODE")
            결과, 펼침 = 전체_검사(원천, cur)
            if 용어원천 is not None:
                ontology_terms.검사(용어원천["용어"], 원천["schema"], 결과, db=DB_원문(cur), 복원본=복원본_읽기(폴더),
                                   개념ID={c["id"] for c in 펼침["개념"]})
                결과.통계["용어"] = len(용어원천["용어"])
            for e in 결과.오류:
                print(f"오류 [{e['코드']}] {e['위치']}: {e['내용']}")
            for e in 결과.경고:
                print(f"경고 [{e['코드']}] {e['위치']}: {e['내용']}")
            print("검사:", json.dumps(결과.통계, ensure_ascii=False))
            if not 결과.통과:
                print(f"검사 실패: 오류 {len(결과.오류)}건. 적재하지 않는다")
                return 1
            행 = DB행_만들기(펼침, 원천["schema"])
            현재 = _현재(cur, 범위)
            차이 = 차이_계산(행, 현재, 범위)
            용어새 = 용어옛 = None
            용어유지: set[str] = set()
            if 용어원천 is not None:
                용어새 = ontology_terms.범위_행(ontology_terms.DB행(용어원천["용어"], 원천["schema"]), 범위)
                용어옛 = ontology_terms.현재(cur, 범위)
                용어유지 = ontology_terms.재검토_유지(용어새, 용어옛)
                차이.update(ontology_terms.차이(용어새, 용어옛, 용어유지))
            else:
                print(f"{ontology_terms.원천_파일} 이 없어 용어 풀이는 건드리지 않는다")
            차이_출력(차이)
            유지 = _재검토_유지(행, 현재, 범위)
            유지수 = {표: len(v) for 표, v in 유지.items()}
            유지수["용어"] = len(용어유지)
            if any(유지수.values()):
                print(f"재검토 유지(원천 행이 그대로): 규범 {유지수['규범']} · 노드 {유지수['개념']} · 용어 {유지수['용어']}")
            if dry:
                conn.rollback()
                print("--dry: 쓰지 않았다")
                return 0

            변경 = _변경있음(차이)
            정리: list[int] = []
            끊김: list[str] = []
            삭제캐시 = 0
            try:
                with conn.transaction():
                    cur.execute("SET LOCAL lock_timeout = '10s'")
                    전 = _데이터버전(cur)
                    _쓰기(cur, 행, 현재, 범위, 유지)
                    if 용어새 is not None:
                        끊김 = ontology_terms.쓰기(cur, 용어새, 용어옛, 범위, 용어유지)
                    _참조_확인(cur)
                    정리 = _l3_정리(cur)
                    # 관계형 원천과 AGE를 한 트랜잭션으로 바꾼다. 그래프 실패 시 원천도 롤백한다.
                    try:
                        그래프행수 = ontology_age.동기화(cur)
                    except Exception as e:
                        raise 적재실패(f"AGE 그래프 동기화 실패: {type(e).__name__}") from e
                    후 = _데이터버전(cur)
                    핵심변경 = any(차이[t][k] for t in ("개념", "라벨", "관계", "규범", "용어") if t in 차이 for k in ("추가", "변경", "삭제"))
                    if 핵심변경 and 전 == 후:
                        raise 적재실패("내용이 바뀌었는데 데이터 버전 해시가 그대로다. 캐시가 무효화되지 않는다")
                    if not 변경 and 전 != 후:
                        raise 적재실패("내용이 같은데 데이터 버전 해시가 바뀌었다. 다시 적재해도 같아야 한다")
                    if 변경 or 정리:
                        # 서버는 별개 프로세스다. DB 판정 캐시는 여기서 지우고, 서버의 해시 캐시는 TTL 뒤 새 값이 된다.
                        # L3 연결(concept_ids)은 해시에 없어 정리만 일어나도 지운다.
                        삭제캐시 = cur.execute("DELETE FROM tenant.judge_cache").rowcount
                    행수 = _행수(cur)
                    용어집계 = ontology_terms.단계_집계(cur)
            except 적재실패 as e:
                print(f"적재 실패(되돌렸다): {e}")
                return 1

    from server.response_cache import 데이터버전_해시_무효화
    데이터버전_해시_무효화()                          # 같은 프로세스에서 불렀을 때만 효과가 있다
    if 정리:
        print(f"l3_articles.concept_ids 에서 없는 ID 를 지웠다: article_id {정리}")
    if 끊김:
        print(f"용어 풀이 {len(끊김)}행은 DB 에 없는 개념을 가리켜 concept_id 를 비웠다: {끊김[:_보기_상한]}")
    print(f"데이터 버전 해시: {전} → {후}" + ("" if 전 != 후 else " (변경 없음)"))
    if 변경 or 정리:
        print(f"판정 캐시 삭제 {삭제캐시}행. 서버 프로세스의 해시 캐시는 최대 "
              f"{os.environ.get('SUDDOE_DATA_VERSION_TTL', '300')}초(SUDDOE_DATA_VERSION_TTL) 뒤 새 값이 된다. "
              "메모리 캐시(게스트)도 그때 무효가 된다")
    로그 = 로그_쓰기(폴더, {
        "시각": dt.datetime.now().isoformat(timespec="seconds"), "범위": [s or "공통" for s in 범위],
        "원천파일": 원천["파일"], "행수": 행수, "검사": 결과.요약(),
        "차이": {t: {k: (len(v) if isinstance(v, list) else v) for k, v in d.items()} for t, d in 차이.items()},
        "재검토_유지": 유지수, "l3_정리_article_id": 정리, "판정캐시_삭제": 삭제캐시, "데이터버전": {"전": 전, "후": 후},
        "AGE": 그래프행수,
        "용어": {**용어집계, "원천해시": 용어원천["해시"] if 용어원천 else None, "concept_끊김": 끊김}})
    print(f"행 수: {json.dumps(행수, ensure_ascii=False)}")
    print(f"적재 로그: {로그}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="온톨로지 원천 JSON 적재")
    p.add_argument("사업키", nargs="*", help="쓸 사업(schema.json 사업키). 없으면 파일이 있는 사업 전부. 공통 범위는 언제나 쓴다")
    p.add_argument("--원천", default=str(원천_기본폴더), help="원천 폴더(기본 ontology/). 적재 로그도 이 폴더 아래에 쓴다")
    p.add_argument("--dry", action="store_true", help="diff 만 출력하고 쓰지 않는다")
    a = p.parse_args(argv)
    return 적재(a.원천, a.사업키 or None, a.dry, os.environ.get("SUDDOE_INGEST_DSN") or db.DSN)


if __name__ == "__main__":
    sys.exit(main())
