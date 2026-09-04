# -*- coding: utf-8 -*-
"""저작본(_저작원본/<사업>.json) → `eval.golden_set` 적재 모양(<사업>.json).

컬럼 순서·enum 은 중앙(ai-c5)이 DB 에서 확인해 준 값을 따른다.
저작본의 확장 필드(판정이유·함정·가능트랩·출처메모·사업간차이·판단불가사유)는
컬럼 밖으로 새면 적재가 깨지므로 전부 `입력필드` jsonb 안으로 넣는다.
`대조방식` 은 여기서 실제로 코퍼스와 대조해 문항별로 기록한다 — 손으로 적지 않는다.

사용:  python to_db_shape.py <사업>[ <사업>...]
"""
import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(__file__))
from verify_golden import DSN, art_key, norm, 대조  # noqa: E402

세트맵 = {"직접작성": "본세트", "직접작성(적대적)": "적대적", "별첨4": "공식"}
확장키 = ["함정", "가능트랩", "출처메모", "사업간차이"]
# 규약 ③ (중앙 확정 2026-09-04): `판정이유` 는 **불가·조건부 문항에만** 5축 값 하나를 넣는다.
# 가능·판단불가 문항은 판정이유를 비우고(null) 사유를 `입력필드.사유` 에 자유 서술한다.
# 「해당없음(요건충족)」 같은 값을 이유 칸에 넣으면 그게 축의 한 값처럼 집계되기 때문이다.
_이유필요 = {"불가", "조건부"}
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def convert(사업: str):
    src = os.path.join(BASE, "_저작원본", f"{사업}.json")
    dst = os.path.join(BASE, f"{사업}.json")
    doc = json.load(open(src, encoding="utf-8"))

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("select doc_id, 조번호, 본문 from corpus.doc_articles")
        arts = {(d, a): norm(b) for d, a, b in cur.fetchall()}
        cur.execute("select doc_id, extraction from corpus.documents")
        ext = dict(cur.fetchall())

    rows = []
    for it in doc["문항"]:
        방식 = {}
        신뢰 = "A"
        for ev in it["정답근거"]:
            m, _, _ = 대조(ev["원문"], arts.get((ev["doc"], art_key(ev["조번호"])), ""))
            방식[ev["조번호"]] = m
            if ext.get(ev["doc"]) == "vlm":
                신뢰 = "B"
        입력 = {
            "층별": sorted({"L1" if e["doc"].startswith("L1_") else "L2"
                           for e in it["정답근거"]}),
            "추출": sorted({ext.get(e["doc"]) for e in it["정답근거"]}),
            "인용신뢰": 신뢰,
            "대조방식": 방식,
            "출처": it["출처"],
        }
        if it["정답판정"] in _이유필요:
            입력["판정이유"] = it["판정이유"]
        else:
            입력["판정이유"] = None
            사유 = it.get("판단불가사유") or it.get("판정이유_성격")
            if 사유:
                입력["사유"] = 사유
        for k in 확장키:
            if it.get(k):
                입력[k] = it[k]
        세트 = 세트맵[it["출처"]]
        채점 = ["판정일치율", "인용정확도"]
        if 세트 == "적대적":
            채점 = ["치명오답률", "판정일치율", "인용정확도"]
        rows.append({
            "세트": 세트,
            "no": it["no"],
            "사업명": doc["사업명"],
            "질문": it["질문"],
            "정답판정": it["정답판정"],
            "정답근거": it["정답근거"],
            "근거원문": it["근거원문"],
            "해야할일": it.get("해야할일") or [],
            "채점대상": 채점,
            "verified": False,
            "검수메모": f"W1(ai-f6) 저작 2026-09-04 · 교차검토·오너 스팟체크 전 · 인용신뢰 {신뢰}",
            "비목": it.get("비목"),
            "입력필드": 입력,
            "적용범위": None,
            "대상": it["대상"],
            "평가범위": "유효",
            "채점모드": "full",
        })

    json.dump(rows, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{사업}: {len(rows)}행 → {dst}")


if __name__ == "__main__":
    for 사업 in sys.argv[1:]:
        convert(사업)
