# -*- coding: utf-8 -*-
"""① 지출 항목 입력 검증(v057) 측정. 개발용 129건(cls_exp/items.jsonl) × 반복 N회를 운영 코드 경로(expense_input_check.검증)로 돌린다.
LLM 은 운영 서버 IP 경유(운영 api env 의 모델), DB 는 쓰지 않는다. 정답: 라벨 A · B 합의(cls_exp/eval.py 의 gold).

  PYTHONIOENCODING=utf-8 python debug/입력검증_측정.py [반복=3] [출력.jsonl]
"""
import collections
import concurrent.futures as cf
import json
import sys
from pathlib import Path

DEBUG = Path(__file__).resolve().parent
작업트리 = DEBUG.parent
메인 = 작업트리.parents[2]
CLS = 메인 / ".claude" / "작업" / "분류실험_20261115" / "cls_exp"
for p in (str(DEBUG), str(작업트리 / "scripts"), str(작업트리)):
    sys.path.insert(0, p)

import 설정  # noqa: E402
import 운영연결  # noqa: E402

설정.연결 = "운영"
설정.DB포트, 설정.SOCKS포트 = 15491, 11131
반복 = int(sys.argv[1]) if len(sys.argv) > 1 else 3
출력 = Path(sys.argv[2]) if len(sys.argv) > 2 else DEBUG / "실행기록" / "입력검증_측정.jsonl"
출력.parent.mkdir(parents=True, exist_ok=True)

정보 = 운영연결.연결(설정)
print("운영 SHA", 정보["운영SHA"], "· 코드 SHA", 정보["코드SHA"])
print({k: v for k, v in 운영연결.보이는_env().items() if "MODEL" in k})

import expense_input_check as eic  # noqa: E402  (연결 뒤 import: llm_qwen 이 import 때 env 를 읽는다)

ITEMS = [json.loads(x) for x in (CLS / "items.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
sys.path.insert(0, str(CLS))
_argv, sys.argv = sys.argv, [sys.argv[0]]
import eval as EV  # noqa: E402  (gold_paths.json 이 있으면 DB 없이 gold 만 만든다)
sys.argv = _argv


def 한번(job):
    it, n = job
    try:
        r = eic.검증(it.get("품목"), it.get("용도"), it.get("금액"))
        return {"id": it["id"], "run": n, **r}
    except Exception as e:  # noqa: BLE001
        return {"id": it["id"], "run": n, "오류": type(e).__name__}


jobs = [(it, n) for it in ITEMS for n in range(반복)]
with cf.ThreadPoolExecutor(8) as ex, 출력.open("w", encoding="utf-8") as f:
    rows = []
    for row in ex.map(한번, jobs):
        rows.append(row)
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()

A = {json.loads(l)["id"]: json.loads(l) for l in (CLS / "label/labels_A.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
B = {json.loads(l)["id"]: json.loads(l) for l in (CLS / "label/labels_B.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
by = collections.defaultdict(list)
for r in rows:
    by[r["id"]].append(r.get("판단") or "오류")
다수 = {i: ("지출아님" if v.count("지출아님") * 2 > len(v) else "지출") for i, v in by.items()}
오류 = sum(1 for r in rows if "오류" in r)
아님 = [i for i in by if A[i]["종류"] == B[i]["종류"] == "품목아님"]
품목합의 = [i for i in by if A[i]["종류"] == B[i]["종류"] == "품목"]
품목111 = [i for i, g in EV.gold.items() if g["종류"] == "품목"]
ITEM = {it["id"]: it for it in ITEMS}
print(f"\n실행 {len(rows)}회 (129건 × {반복}) · LLM 오류 {오류}")
print(f"지출 아님 재현율(다수결): {sum(다수[i] == '지출아님' for i in 아님)}/{len(아님)}"
      f" · 실행 단위 {sum(by[i].count('지출아님') for i in 아님)}/{len(아님) * 반복}")
for 이름, ids in (("gold 품목(eval.py, 경로 합의)", 품목111), ("품목 합의(노드없음 포함)", 품목합의)):
    오탐 = [i for i in ids if 다수[i] == "지출아님"]
    print(f"{이름} 오탐(다수결): {len(오탐)}/{len(ids)} · 실행 단위 {sum(by[i].count('지출아님') for i in ids)}/{len(ids) * 반복}")
    for i in 오탐:
        print("   ", i, ITEM[i]["품목"][:30], "|", (ITEM[i].get("용도") or "")[:40], by[i])
불일치 = [i for i, v in by.items() if len(set(v)) > 1]
print(f"반복 불일치 {len(불일치)}건:", [(ITEM[i]["품목"][:20], by[i]) for i in 불일치])
ms = sorted(r["ms"] for r in rows if "ms" in r)
if ms:
    print(f"지연 ms: 중앙 {ms[len(ms) // 2]:,} · p90 {ms[int(len(ms) * 0.9)]:,} · 최대 {ms[-1]:,}")
print("출력:", 출력)
운영연결.터널_닫기()
