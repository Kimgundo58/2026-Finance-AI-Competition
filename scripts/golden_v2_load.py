# -*- coding: utf-8 -*-
"""정답셋 v2(`golden_v2/`)를 `eval.golden_set` 에 적재한다. eval 스키마만 쓴다. 코퍼스 · 검색 색인은 건드리지 않는다.

`eval.run_items.gold_id` 가 `eval.golden_set` 을 참조하므로 v2 도 여기에 넣는다(docs/5_룰/5-9_검증과_측정.md).
세트 이름은 `v2_dev` · `v2_test` 이고, 기존 평가 스크립트는 `eval_store.기존세트_SQL` 로 이 행을 건너뛴다.
재실행하면 (세트, no) 가 같은 행을 그 자리에서 고친다. gold_id 를 유지해 run_items 연결이 끊기지 않는다.

칸 대응 (읽는 곳이 있는 값만 넣는다. 읽는 곳: eval_retrieval.py):
  no = id, 질문 = 대화.첫질문(화면 첫 입력과 같은 짧은 질문), 정답판정 = 판정, 비목 = 비목, verified = false(AI 라벨, 사람 미검수),
  사업명 = corpus.programs 에 있는 값(없으면 NULL, 원표기는 적용범위), 정답근거 = 근거[]의 역할 · doc_id · 조번호 · 항호 · layer · 원문(구절),
  문항메타 = {"L3": 기관 규정 반사실 문항 여부}
대화의 추가질문과 L3 기관규정문은 넣지 않는다. 판정 효과 측정(P5-2)을 만들 때 그 스크립트와 같이 더한다(docs/9_백로그.md).

문항 본문과 정답은 출력하지 않는다. 개수만 출력한다.

실행: PYTHONIOENCODING=utf-8 python scripts/golden_v2_load.py [--분할 dev|test] [--dry]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
폴더 = ROOT / "golden_v2"
판정값 = {"가능", "조건부", "불가", "판단불가"}
_L3_문서 = "L3_기관규정문"


def 파일_읽기(분할: str) -> list[dict]:
    """golden_v2/<분할>.json 을 읽는다. 미추적 폴더라 없을 수 있다. 없거나 형식이 다르면 오류로 끝낸다."""
    경로 = 폴더 / f"{분할}.json"
    if not 경로.exists():
        sys.exit(f"오류: {경로} 가 없다. golden_v2/ 는 커밋하지 않는 폴더다. 작업본을 레포 루트에 두고 다시 실행한다.")
    문항들 = json.loads(경로.read_text(encoding="utf-8"))
    if not isinstance(문항들, list) or not 문항들:
        sys.exit(f"오류: {경로} 가 문항 배열이 아니거나 비어 있다.")
    return 문항들


def 행_만들기(x: dict, 세트: str, 사업들: set[str]) -> dict:
    """문항 1개 → golden_set 행. 필수 값이 없으면 ValueError."""
    첫질문 = ((x.get("대화") or {}).get("첫질문") or "").strip()
    if not x.get("id") or not 첫질문 or x.get("판정") not in 판정값 or not x.get("근거"):
        raise ValueError(f"필수 값이 없다: {x.get('id')!r}")
    사업 = x.get("사업명")
    return {
        "세트": 세트, "no": x["id"],
        "사업명": 사업 if 사업 in 사업들 else None,
        "적용범위": None if 사업 in 사업들 else 사업,
        "질문": 첫질문, "정답판정": x["판정"], "비목": x.get("비목"),
        "정답근거": json.dumps(
            [{"역할": g.get("역할"), "doc_id": g.get("doc_id"), "조번호": g.get("조번호"), "항호": g.get("항호"),
              "layer": g.get("layer"), "원문": g.get("구절")} for g in x["근거"]], ensure_ascii=False),
        "문항메타": json.dumps({"L3": bool(x.get("L3"))}, ensure_ascii=False),
    }


def 적재(cur, 행들: list[dict]) -> Counter:
    """(세트, no) 기준으로 넣거나 고친다. 같은 값이면 쓰지 않는다. 제약이 없는 표라 잠그고 한 번에 처리한다."""
    cur.execute("LOCK TABLE eval.golden_set IN SHARE ROW EXCLUSIVE MODE")
    집계 = Counter()
    for r in 행들:
        cur.execute("SELECT gold_id FROM eval.golden_set WHERE 세트 = %(세트)s AND no = %(no)s", r)
        있음 = cur.fetchall()
        if len(있음) > 1:
            raise ValueError(f"같은 (세트, no) 가 {len(있음)}행이다: {r['no']!r}")
        if not 있음:
            cur.execute(
                """INSERT INTO eval.golden_set (세트, no, 사업명, 적용범위, 질문, 정답판정, 비목, 정답근거, 문항메타, verified)
                   VALUES (%(세트)s, %(no)s, %(사업명)s, %(적용범위)s, %(질문)s, %(정답판정)s, %(비목)s,
                           %(정답근거)s::jsonb, %(문항메타)s::jsonb, false)""", r)
            집계["추가"] += 1
            continue
        cur.execute(
            """UPDATE eval.golden_set
                  SET 사업명 = %(사업명)s, 적용범위 = %(적용범위)s, 질문 = %(질문)s, 정답판정 = %(정답판정)s,
                      비목 = %(비목)s, 정답근거 = %(정답근거)s::jsonb, 문항메타 = %(문항메타)s::jsonb, verified = false
                WHERE gold_id = %(gold_id)s
                  AND (사업명, 적용범위, 질문, 정답판정, 비목, 정답근거, 문항메타, verified)
                      IS DISTINCT FROM
                      (%(사업명)s::text, %(적용범위)s::text, %(질문)s::text, %(정답판정)s::text, %(비목)s::text,
                       %(정답근거)s::jsonb, %(문항메타)s::jsonb, false)""",
            {**r, "gold_id": 있음[0][0]})
        집계["변경" if cur.rowcount else "그대로"] += 1
    return 집계


def 근거_대조(cur, 행들: list[dict]) -> Counter:
    """정답근거가 가리키는 조문이 코퍼스에 있는지 센다. L3 기관 규정은 가상 문서라 따로 센다."""
    cur.execute("SELECT doc_id, 조번호 FROM corpus.doc_articles")
    조문 = {(a, b) for a, b in cur.fetchall()}
    cur.execute("SELECT doc_id, status FROM corpus.documents")
    상태 = dict(cur.fetchall())
    집계 = Counter()
    for r in 행들:
        for g in json.loads(r["정답근거"]):
            if g["doc_id"] == _L3_문서:
                집계["L3 가상 규정"] += 1
            elif (g["doc_id"], g["조번호"]) not in 조문:
                집계["조문 없음"] += 1
            elif 상태.get(g["doc_id"]) != "active":
                집계["active 아닌 문서"] += 1
            else:
                집계["active 조문"] += 1
    return 집계


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--분할", choices=("dev", "test"), default="dev",
                    help="기본 dev. test 는 최종 측정 전까지 넣지 않는다(만드는 동안 평가 문항을 쓰지 않는다)")
    ap.add_argument("--dry", action="store_true", help="검사와 집계만 하고 되돌린다")
    a = ap.parse_args()

    문항들 = 파일_읽기(a.분할)
    다른분할 = sum(1 for x in 문항들 if x.get("분할") != a.분할)
    if 다른분할:
        sys.exit(f"오류: {a.분할}.json 에 분할이 다른 문항이 {다른분할}개 있다.")
    if len({x.get("id") for x in 문항들}) != len(문항들):
        sys.exit(f"오류: {a.분할}.json 에 id 가 겹치는 문항이 있다.")

    세트 = f"v2_{a.분할}"
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 사업명 FROM corpus.programs")
        사업들 = {r[0] for r in cur.fetchall()}
        행들 = [행_만들기(x, 세트, 사업들) for x in 문항들]
        집계 = 적재(cur, 행들)
        cur.execute("SELECT count(*) FROM eval.golden_set WHERE 세트 = %s AND no <> ALL(%s)",
                    (세트, [r["no"] for r in 행들]))
        파일에없음 = cur.fetchone()[0]
        cur.execute("""SELECT 사업명, count(*), count(*) FILTER (WHERE (문항메타->>'L3')::boolean)
                         FROM eval.golden_set WHERE 세트 = %s GROUP BY 1 ORDER BY 1 NULLS LAST""", (세트,))
        사업별 = cur.fetchall()
        cur.execute("SELECT count(*) FROM eval.golden_set WHERE left(세트, 3) <> 'v2_'")
        기존 = cur.fetchone()[0]
        대조 = 근거_대조(cur, 행들)
        if a.dry:
            conn.rollback()
        else:
            conn.commit()

    print(f"{세트}: 파일 {len(문항들)}문항 · {dict(집계)}{' (되돌림)' if a.dry else ''}")
    print(f"  기존 세트 {기존}행 · 파일에 없는 {세트} 행 {파일에없음}")
    print(f"  정답근거 대조: {dict(대조)}")
    for 사업, n, l3 in 사업별:
        print(f"  {사업 or '(사업 지정 없음)'}: {n}문항 (L3 {l3})")


if __name__ == "__main__":
    main()
