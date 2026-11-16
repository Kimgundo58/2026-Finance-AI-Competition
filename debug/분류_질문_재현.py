# -*- coding: utf-8 -*-
"""③ 분류 · ④-2 판정용 조사 · ⑤ 질문을 운영 DB(읽기 전용)로 같은 입력에 여러 번 다시 돌린다. 화면 흐름 대신 함수를 바로 부른다.

    PYTHONIOENCODING=utf-8 python debug/분류_질문_재현.py [반복=3] [판정조사=1]

운영 배포 코드(main efe9653 (PR #106 · #108 · #109 · #110 · #113 · #107 · #112 · #111 · #115 · #116)) 기준. 화면 경로(routes_questions)와 같은 함수를 같은 인자로 부른다:
  ③ item_classify.분류(cur, 사업명, {품목, 용도, 금액, 거래처}, 레이블, 확인정보)       분류 LLM 프롬프트 변형 A · B · C 3번
                                                                                     (+ 불일치면 조정 1번, 한 문장이면 설명 정리 LLM)
  ④-2 · ⑤ question_generate.질문생성({…, _노드결정, _판정조사설정})                   판정용 조사(웹) · 질문 LLM
입력은 debug/설정.py 의 칸(품목 · 금액 · 용도 · 거래처)과 품목설명이다. 칸이 비면 회의실 대여 건(2026-11-12)을 쓴다.
② 조사 확인 문장은 넣지 않는다(확인정보 = 품목설명 하나). 기관 L3 는 읽지 않는다(org_id 없음).
디버그 실행과 동시에 돌 수 있게 터널 포트를 다르게 쓴다(DB 15436 · SOCKS 11082).
"""
import json
import os
import sys
import types

_루트 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_루트)
sys.path[:0] = [os.path.join(_루트, "debug"), os.path.join(_루트, "scripts"), _루트]
import 설정 as _설정  # noqa: E402
import 운영연결  # noqa: E402

_따로 = types.SimpleNamespace(**{k: getattr(_설정, k) for k in dir(_설정) if not k.startswith("_")})
_따로.DB포트, _따로.SOCKS포트 = 15436, 11082
print("운영 연결:", {k: v for k, v in 운영연결.연결(_따로).items() if k != "코드고침"})

import item_classify  # noqa: E402
import ontology_lookup  # noqa: E402
import question_generate  # noqa: E402
import research_runtime  # noqa: E402
from _lib import db  # noqa: E402
from server._common import 비목_ENUM  # noqa: E402

반복 = int(sys.argv[1]) if len(sys.argv) > 1 else 3
판정조사 = (sys.argv[2] if len(sys.argv) > 2 else "1") == "1"
사업명 = _설정.사업명
칸 = {k: v for k, v in (_설정.칸 or {}).items() if v is not None}
입력 = ({"품목": 칸.get("품목"), "용도": 칸.get("용도"), "금액": float(칸["금액"]) if 칸.get("금액") else None, "거래처": 칸.get("거래처")}
      if 칸.get("품목") else {"품목": "회의실 1일 대여", "용도": "QA진행", "금액": 80000.0, "거래처": None})
확인정보 = item_classify.확인정보_글(None, _설정.품목설명 or None)
print("입력:", 입력, "· 확인정보:", 확인정보)
기록 = {"입력": 입력, "분류": [], "질문": []}

with db.connect(connect_timeout=10, autocommit=True) as c:
    cur = c.cursor()
    레이블 = [x for x in ontology_lookup.레이블_목록(cur, 사업명) if x["비목"] in 비목_ENUM]   # routes_questions._분류 와 같게
    print("\n③ 분류")
    결과들 = []
    for i in range(반복):
        설명캐시: dict = {}
        with research_runtime.activate(research_runtime.Budget()):
            r = item_classify.분류(cur, 사업명, 입력, 레이블, 확인정보, 설명캐시=설명캐시)
        d = r["디버그"]
        줄 = {"분류": (r["분류"] or {}).get("표시문"), "품목": (r["분류"] or {}).get("품목"), "방법": r["방법"],
             "확신": (r["노드결정"] or {}).get("확신"),
             # v056: 프롬프트 변형 A · B · C 결과와 이유 · 불일치(거래유형 | 경로) · 조정 근거 · 거래 유형 확인 질문
             "변형": [{k: x.get(k) for k in ("변형", "품목", "표시문", "거래유형", "확신", "이유", "ms", "실패")} for x in d.get("변형") or []],
             "불일치": d.get("불일치"), "조정": d.get("조정"), "거래유형질문": r.get("거래유형질문"),
             "확인거래유형": d.get("확인거래유형"), "발췌후보수": d.get("발췌후보수"), "필요정보": r.get("필요정보"),
             "설명정리": 설명캐시 or None, "LLM실패": d.get("LLM실패")}
        print(f"  #{i + 1}", json.dumps(줄, ensure_ascii=False))
        기록["분류"].append(줄)
        결과들.append(r)

print("\n④-2 · ⑤ 질문 (첫 분류 결과로" + (", 판정용 조사 켬)" if 판정조사 else ", 판정용 조사 끔)"))
r0 = next((r for r in 결과들 if r["분류"]), None)
if r0:
    q입력 = {"사업명": 사업명, "비목": r0["분류"]["비목"], "세부유형": r0["분류"]["세목"], "품목": 입력["품목"], "용도": 입력["용도"],
           "금액": 입력["금액"], "질문": f"{사업명}에서 {입력['용도']} {입력['품목']} {입력['금액']:,.0f}원을 사도 되나요?",
           "확정세목": r0["분류"]["세목"], "부가세구분": _설정.부가세구분, "_노드결정": r0["노드결정"],
           **({"_판정조사설정": {"판매URL": _설정.판매URL, "거래처": 입력.get("거래처")}} if 판정조사 else {})}
    for i in range(max(1, 반복 - 1)):
        with research_runtime.activate(research_runtime.Budget()):
            q = question_generate.질문생성(q입력)
        메타 = q.get("메타") or {}
        pj = q.get("판정조사") or {}                      # 질문생성 반환 키(라우트가 세션.판정조사 로 옮긴다)
        줄 = {"상태": q["상태"], "질문": [x.get("질문문") for x in q["질문"]], "추가입력": [x.get("라벨") for x in q.get("추가입력") or []],
             "조건목록수": 메타.get("조건목록"), "조건질문": 메타.get("조건질문"), "버린질문": list((메타.get("버린질문") or {}).keys()),
             "판정조사": {"상태": pj.get("상태"),
                       "요건결과": {v.get("번호"): [v.get("결과"), v.get("값"), v.get("URL")] for v in (pj.get("요건결과") or {}).values()
                                if isinstance(v, dict)}} if pj else None}
        print(f"  #{i + 1}", json.dumps(줄, ensure_ascii=False))
        기록["질문"].append(줄)
else:
    print("  분류가 없어 질문을 만들지 않습니다(화면은 ③-R 재조사 · 설명입력 · 분류 없이 판정으로 갑니다).")

json.dump(기록, open(os.path.join("debug", "실행기록", "분류_질문_재현.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("저장: debug/실행기록/분류_질문_재현.json")
