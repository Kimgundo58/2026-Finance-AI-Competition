# -*- coding: utf-8 -*-
"""P3 — 「질문과 무관한 행을 뺀다」가 얼마를 줄이고 무엇을 잃는지 «구현 전에» 잰다.

읽기 전용이다. 코드도 안 고친다 — `SUDDOE_ROWSPLIT=1` 로 쪼갠 행에 라벨을 붙여
**세기만** 한다. 제거 기능은 아직 없다(ai-e8 승인 전).

## 무엇을 근거로 뺀다고 말할 수 있나 — 이 스크립트가 답할 물음
  ① 행에 «비목» 라벨이 실제로 붙는가. 안 붙는 행이 몇 자인가 (라벨 없으면 못 뺀다)
  ② 질문의 정규화 비목이 실제로 채워지는가. 비면 뺄 근거가 없다
  ③ 라벨이 질문 비목과 «다른» 행은 몇 자인가 (= 뺄 수 있는 최대치)
  ④ 🔴 그렇게 빼면 **정답 근거가 든 행**을 잃는가
     `eval.golden_set.정답근거[].원문`(중앙 60자대)을 공백 제거 후 부분문자열로 찾아
     그 문장이 든 행이 「빼는 쪽」인지 「남는 쪽」인지 센다. 이것이 유일한 **행 단위**
     정답이다 — `_인용좌표`·`golden_chunks` 는 (doc_id, 조번호) 라 행을 못 가른다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P3_행제거_예상.py --out scratchpad/P3_행제거_예상.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

_여기 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_여기), "scripts"))

from _lib import db          # noqa: E402
import eval_store            # noqa: E402
import assemble_context as A  # noqa: E402
import orchestrate           # noqa: E402


def 행라벨(행: str) -> str | None:
    """행 머리에 붙은 비목. 없으면 None — 라벨 없는 행은 «뺄 근거가 없는» 행이다."""
    m = A._RE_비목행.match(행)
    return re.sub(r"\s+", "", m.group(0)) if m else None


def 납작(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_여기, "P3_행제거_예상.json"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=5)
    a = ap.parse_args()

    포획: dict = {}
    원조립 = A.조립

    def spy(cur, 질문, 정규화, **kw):
        포획.clear()
        포획.update(정규화=dict(정규화 or {}), 폐포=list(kw.get("폐포") or []))
        return 원조립(cur, 질문, 정규화, **kw)

    orchestrate.조립 = spy
    os.environ["SUDDOE_ROWSPLIT"] = "1"

    결과, t0 = [], time.time()
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        코퍼스 = eval_store.코퍼스버전(cur)
        문항 = eval_store.평가대상(cur)
        if a.limit:
            문항 = 문항[:a.limit]
        print(f"코퍼스버전 {코퍼스} · 채점대상 {len(문항)}문항 · top_k={a.top_k}", flush=True)
        본문캐시: dict[int, tuple] = {}

        for i, m in enumerate(문항, 1):
            gid = m["gold_id"]
            포획.clear()
            try:
                orchestrate.판정(m["질문"], 사업명=eval_store.사업키(m["사업명"]),
                                dry=True, top_k=a.top_k, conn=conn, 기록=False)
            except Exception as e:
                결과.append({"gold_id": gid, "오류": f"{type(e).__name__}: {e}"})
                continue
            if not 포획:
                결과.append({"gold_id": gid, "오류": "조립 미호출"})
                continue

            정규화 = 포획["정규화"]
            # 🔴 `정규화` 에 `비목` 키는 없다. `비목후보[{비목,신뢰도}]` 다
            #    (`normalize_run.호출자리1_스키마`). 첫 후보를 쓴다.
            후보 = 정규화.get("비목후보") or []
            # 후보 원소가 dict 인 run 과 str 인 run 이 둘 다 있다 — 실측으로 확인했다.
            첫 = 후보[0] if 후보 else None
            비목 = 납작(str((첫.get("비목") if isinstance(첫, dict) else 첫) or ""))
            폐포 = 포획["폐포"]
            새 = [x for x in 폐포 if x not in 본문캐시]
            if 새:
                cur.execute("""SELECT article_id, doc_id, 조번호, 본문
                                 FROM corpus.doc_articles WHERE article_id = ANY(%s)""", (새,))
                for r in cur.fetchall():
                    본문캐시[r[0]] = (r[1], r[2], r[3])
            cur.execute("SELECT 정답근거 FROM eval.golden_set WHERE gold_id=%s", (gid,))
            근거 = (cur.fetchone() or [None])[0] or []
            정답문 = [납작(g.get("원문") or "") for g in 근거 if (g.get("원문") or "").strip()]

            표자수 = 라벨자수 = 무라벨자수 = 제거후보 = 0
            표행수 = 라벨행수 = 0
            정답행_있음 = 정답행_제거됨 = 0
            for aid in 폐포:
                if aid not in 본문캐시:
                    continue
                doc, 조, 본문 = 본문캐시[aid]
                조각들 = A.분해(본문 or "")
                if len(조각들) < 2 or not A._표덩이인가(본문 or ""):
                    continue
                표자수 += len(본문 or "")
                표행수 += len(조각들)
                for _, 행 in 조각들:
                    lab = 행라벨(행)
                    뺄까 = bool(lab and 비목 and lab != 비목)
                    라벨자수 += len(행) if lab else 0
                    무라벨자수 += 0 if lab else len(행)
                    라벨행수 += 1 if lab else 0
                    제거후보 += len(행) if 뺄까 else 0
                    납작행 = 납작(행)
                    for 문 in 정답문:
                        if 문 and 문 in 납작행:
                            정답행_있음 += 1
                            정답행_제거됨 += 1 if 뺄까 else 0
                            break

            결과.append({
                "gold_id": gid, "세트": m["세트"], "적용범위": m["적용범위"],
                "비목": 비목 or None, "비목후보수": len(후보),
                "정규화출처": 정규화.get("_출처"), "품목": 정규화.get("품목"),
                "표자수": 표자수, "표행수": 표행수, "라벨행수": 라벨행수,
                "라벨자수": 라벨자수, "무라벨자수": 무라벨자수,
                "제거후보자수": 제거후보,
                "정답문수": len(정답문), "정답행_찾음": 정답행_있음,
                "정답행_제거됨": 정답행_제거됨,
            })
            if i % 20 == 0 or i == len(문항):
                print(f"  {i}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)

    os.environ.pop("SUDDOE_ROWSPLIT", None)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"코퍼스버전": 코퍼스, "문항수": len(결과), "항목": 결과},
                  f, ensure_ascii=False, indent=1)
    print(f"\n저장 {a.out}")

    산 = [x for x in 결과 if "오류" not in x]
    표있 = [x for x in 산 if x["표자수"] > 0]
    n = len(표있) or 1
    비목있 = [x for x in 산 if x["비목"]]
    print(f"\n① 표가 뜬 문항            {len(표있)}/{len(산)}")
    print(f"   표 행 총수             {sum(x['표행수'] for x in 표있):,} "
          f"· 그중 비목 라벨이 붙은 행 {sum(x['라벨행수'] for x in 표있):,}")
    print(f"   라벨 붙은 자수         {sum(x['라벨자수'] for x in 표있):,} "
          f"· 라벨 없는 자수 {sum(x['무라벨자수'] for x in 표있):,}")
    print(f"② 정규화 비목이 채워진 문항 {len(비목있)}/{len(산)}"
          f"   (예: {[x['비목'] for x in 비목있][:6]})")
    print(f"③ 제거 후보 자수          평균 {sum(x['제거후보자수'] for x in 표있)//n:,}"
          f" · 최대 {max([x['제거후보자수'] for x in 표있], default=0):,}"
          f" · 표자수 대비 {sum(x['제거후보자수'] for x in 표있)/max(1,sum(x['표자수'] for x in 표있))*100:.0f}%")
    찾 = sum(x["정답행_찾음"] for x in 산)
    잃 = sum(x["정답행_제거됨"] for x in 산)
    print(f"④ 🔴 정답 근거 문장이 표 행에서 발견된 문항 {찾}"
          f" · 그중 «빼는 쪽»에 걸린 것 {잃}")


if __name__ == "__main__":
    main()
