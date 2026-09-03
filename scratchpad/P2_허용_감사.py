# -*- coding: utf-8 -*-
"""P2 — 조건부 73행의 근거 조문을 열어 (가)(나)(다)로 가른다. 읽기 전용.

`docs/9-4` ①: `corpus.rules` 허용 분포가 조건부 73 · 가능 1 · 불가 0 이다. 9-4 는 원인을
「행의 `허용` 값이 어떻게 채워졌나」로 지목하고 **아직 아무도 안 봤다**고 적어 뒀다. 그걸 본다.

## 갈래 — ai-e8 지시로 (다)도 수로 남긴다

    (가) 조건 없이 「집행 가능/인정」이 명문      → 오버레이로 '가능' 승격 후보
    (나) 조건이 있다                              → 조건부가 맞다. 손대지 않는다
    (다) 근거가 한도표·판독본이라 못 정한다        → 원인이 룰이 아니라 코퍼스라는 신호

(다)가 다수면 「조건부 쏠림의 원인은 룰 테이블」이라는 가설 자체가 틀린 것이다.

## 🔴 이 스크립트는 갈래를 «정하지» 않는다

포섭은 코드가 할 일이 아니다(`docs/5_룰/5-1` §1 — "직접 연관성"·"사업 목적 부합"은 룰로
만들지 않는다). 여기서 하는 일은 **사람이 읽을 자리를 좁혀 주는 것**뿐이다:
근거 조문에서 그 비목이 나오는 문장만 뽑고, 기계로 확실히 가를 수 있는 (다)만 자동 배정한다.

    (다) 자동 조건 — 둘 중 하나라도:
        · 근거 문서의 `corpus.documents.extraction` 이 'vlm' (스캔 판독본. A등급 인용 금지)
        · 근거 조문이 하나도 `corpus.doc_articles` 에 없다 (근거가 별표·한도표 쪽이다)

    🔴 처음엔 `chunks.parse_quality == 'vlm'` 으로 짰다가 (다) 0 이 나왔다. 그 컬럼의 값은
       'high'/'low' 뿐이라 **규칙이 아예 발화할 수 없었다** — 조건이 틀린 게 아니라 안 도는
       조건이었다. vlm 태그는 `corpus.documents.extraction` 에 있다. 0 이 너무 깨끗하면
       규칙이 실제로 도는지부터 본다.
    나머지는 `갈래=None` 으로 두고 사람이 채운다. 자동으로 (가)를 만들지 않는다 —
    '가능' 승격은 「틀린 가능」을 만드는 방향이라 기계 추정으로 올리면 안 된다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_허용_감사.py
산출: scratchpad/P2_허용_감사.json (사람이 읽는 검토표) · 표준출력에 (다) 계수
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg                                                       # noqa: E402
from _lib import db                                                  # noqa: E402

_문장 = re.compile(r"(?<=[.。])\s+|\n+")
_명문 = re.compile(r"가능|인정|집행할 수 있|사용할 수 있|계상할 수 있|지출할 수 있|인정한다")
_조건 = re.compile(r"사전\s*승인|사전\s*검토|사전\s*심의|승인을 받|소명|한도|이내|초과|"
                   r"경우에 한|한하여|다만|단서|증빙|제출|협의|인정하는 경우")


def main() -> int:
    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT rule_id, layer, 사업명, 비목, 허용, 사전승인, 사전승인_조건, "
                    "한도_유형, 한도_값, 한도_단위, 근거 FROM corpus.rules "
                    "WHERE 허용 = '조건부' ORDER BY 사업명 NULLS FIRST, 비목")
        rules = cur.fetchall()

        표 = []
        for (rid, layer, 사업, 비목, 허용, 사전승인, 사전조건,
             한유, 한값, 한단, 근거) in rules:
            근거 = json.loads(근거) if isinstance(근거, str) else (근거 or [])
            조문들 = []
            for g in 근거:
                doc, 조 = g.get("doc_id"), g.get("조번호")
                cur.execute("SELECT article_id, 조제목, 본문 FROM corpus.doc_articles "
                            "WHERE doc_id=%s AND 조번호=%s AND NOT 삭제", (doc, 조))
                a = cur.fetchone()
                if not a:
                    조문들.append({"doc_id": doc, "조번호": 조, "없음": True})
                    continue
                aid, 제목, 본문 = a
                cur.execute("SELECT extraction, parse_quality FROM corpus.documents "
                            "WHERE doc_id=%s", (doc,))
                d = cur.fetchone() or (None, None)
                pq = [x for x in d if x]
                문장 = [s.strip() for s in _문장.split(본문 or "") if s.strip()]
                관련 = [s for s in 문장 if 비목 and 비목[:4] in s] or 문장[:3]
                조문들.append({
                    "doc_id": doc, "조번호": 조, "조제목": 제목, "출처태그": pq,
                    "관련문장": 관련[:6],
                    "명문표지": bool(any(_명문.search(s) for s in 관련)),
                    "조건표지": bool(any(_조건.search(s) for s in 관련)),
                })

            없음 = [x for x in 조문들 if x.get("없음")]
            vlm = [x for x in 조문들 if "vlm" in (x.get("출처태그") or [])]
            갈래 = "(다) 못 정함" if (len(없음) == len(조문들) or vlm) else None
            표.append({
                "rule_id": rid, "layer": layer, "사업명": 사업, "비목": 비목,
                "사전승인": 사전승인, "사전승인_조건": 사전조건,
                "한도": {"유형": 한유, "값": float(한값) if 한값 is not None else None,
                        "단위": 한단},
                "근거없음": len(없음), "근거수": len(조문들),
                "vlm근거": len(vlm), "갈래": 갈래, "조문": 조문들,
            })

    요약 = {
        "조건부행": len(표),
        "갈래": dict(Counter(r["갈래"] or "미정(사람이 읽는다)" for r in 표)),
        "다_사유": {
            "근거_전부_doc_articles_에_없음": sum(1 for r in 표
                                                if r["근거없음"] == r["근거수"]),
            "vlm_근거_포함": sum(1 for r in 표 if r["vlm근거"]),
        },
        "사전승인_true": sum(1 for r in 표 if r["사전승인"]),
        "한도있음": sum(1 for r in 표 if r["한도"]["값"] is not None),
        "명문표지만_있고_조건표지_없음": sum(
            1 for r in 표 if r["갈래"] is None
            and any(a.get("명문표지") for a in r["조문"])
            and not any(a.get("조건표지") for a in r["조문"])),
    }
    (ROOT / "scratchpad" / "P2_허용_감사.json").write_text(
        json.dumps({"요약": 요약, "행": 표}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(요약, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
