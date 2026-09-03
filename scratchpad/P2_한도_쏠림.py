# -*- coding: utf-8 -*-
"""P2 — 「금액비교 미발화」가 조건부 쏠림의 원인인가. run 191 산출물만으로. 읽기 전용.

ai-a3 가 `_effective()` 에 `수치=` 가 안 넘어가 (2)-e 금액비교가 실판정 경로에서 한 번도
발화 안 했다는 걸 잡았다. 고치면 「고치기 전」을 영영 못 만든다 — 그 전에 재는 것이 이 파일이다.

## 재는 것

한도가 붙은 비목의 문항과 아닌 문항으로 **분모를 갈라** 조건부 쏠림률을 따로 낸다.

    쏠림   = 정답 ≠ '조건부'  ∧  예측 == '조건부'
    쏠림률 = 쏠림 / (정답 ≠ '조건부' 인 문항 수)      ← 분모가 다르니 비율로만 비교한다

  갈리면   → 금액비교 미발화가 원인이다
  안 갈리면 → 원인은 다른 데 있다. 이걸 고쳐도 25건은 안 준다

## 🔴 닻을 둘로 둔다 — 「그 문항의 비목」이 한 값이 아니다

`run_items.원출력` 에 **비목 키가 없다**(키 전수: s맵·top5·강등사유·강등코드·게이트값·경로·
근거적중·사업명·세트·신뢰등급·실패경로·실패단계·요약·인용목록·인용적중·적용범위·전제목록·
지연ms·치명·판정·프롬프트길이). 그러니 run 191 이 실제로 어느 비목으로 룰을 조회했는지는
복원 불가다. 대신 둘로 잰다:

    닻α  `eval.golden_set.비목`    정답 비목. 「원래 걸렸어야 할 룰」. 59/93 에만 있다
    닻β  dry 비목확정 결과          `P2_적중_baseline.json` 의 `비목`. 93 전수지만 43 실패

둘이 같은 방향이면 결론이 서고, 갈리면 그 사실 자체를 적는다. 한 닻으로 확정하지 않는다.

## 한도 판정

`effective_rule(cur, 사업키, 비목, None)` 의 **`한도목록` 이 비지 않았나**.
기관ID 는 None — run 191 설정에 org 가 없다(`{"dry": false, "top_k": 5, ...}`).
🔴 `base_룰` 이 아니라 `effective_rule` 을 쓴다. 프롬프트에 나가는 한도는 병합 후 값이다.

🔴 처음엔 `r.get("한도_값") is not None` 으로 짜서 「한도 있는 비목 0문항」이 나왔다.
   `effective_rule` 의 반환에는 **`한도_값` 키가 아예 없다** — 병합 결과는 `한도목록`
   (리스트)에 담긴다. `dict.get` 이 조용히 None 을 돌려줘서 **조건이 한 번도 참이 될 수
   없었다.** corpus.rules 에 한도_값 있는 행이 20개고 골든 비목과 5종이 겹치는데 0 이 나온
   게 걸려서 잡았다. 「0 이 너무 깨끗하면 규칙이 실제로 도는지부터 본다」— 같은 실수 두 번째다.

## 곁가지 — 질문에 금액이 있나

한도 룰이라도 질문에 수치가 없으면 금액비교는 어차피 「비교 불가」다. 그래서 같이 센다.
🔴 이건 **질문 원문 정규식**이라 대리지표다(`\\d+ *(원|만원|천원|억)` 또는 `\\d{4,}`).
①정규화가 실제로 뽑은 금액이 아니다 — 그건 run 191 에 안 남아 있다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_한도_쏠림.py
산출: scratchpad/P2_한도_쏠림.json + 표준출력 표
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
import eval_store                                                    # noqa: E402
import rule_lookup                                                   # noqa: E402

_금액 = re.compile(r"\d+\s*(원|만원|천원|억|백만)|\d{4,}")
RUN = 191


def 지표(행: list[dict]) -> dict:
    비조건부 = [r for r in 행 if r["정답"] != "조건부"]
    쏠림 = [r for r in 비조건부 if r["예측"] == "조건부"]
    return {
        "문항": len(행),
        "일치": sum(1 for r in 행 if r["예측"] == r["정답"]),
        "일치율": round(sum(1 for r in 행 if r["예측"] == r["정답"]) / len(행), 3)
        if 행 else None,
        "쏠림분모(정답≠조건부)": len(비조건부),
        "쏠림": len(쏠림),
        "쏠림률": round(len(쏠림) / len(비조건부), 3) if 비조건부 else None,
        "쏠림_내역": dict(Counter(r["정답"] for r in 쏠림)),
    }


def main() -> int:
    dryrows = {r["gold_id"]: r for r in json.loads(
        (ROOT / "scratchpad" / "P2_적중_baseline.json").read_text(encoding="utf-8"))["행"]}

    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        문항 = {m["gold_id"]: m for m in eval_store.평가대상(cur)}

        cur.execute("SELECT gold_id, 예측, 정답 FROM eval.run_items WHERE run_id=%s", (RUN,))
        예측 = {g: (p, a) for g, p, a in cur.fetchall()}

        cur.execute("SELECT gold_id, 비목 FROM eval.golden_set WHERE gold_id = ANY(%s)",
                    (list(문항),))
        골든비목 = dict(cur.fetchall())

        # 룰 한도 여부 — (사업키, 비목) 캐시
        캐시: dict[tuple, dict | None] = {}

        def 한도(사업, 비목):
            if 비목 is None:
                return None
            k = (사업, 비목)
            if k not in 캐시:
                r = rule_lookup.effective_rule(cur, 사업, 비목, None)
                캐시[k] = None if r is None else {
                    "한도목록": r.get("한도목록") or [],
                    "금액비교": r.get("금액비교"), "허용": r.get("허용")}
            return 캐시[k]

        행 = []
        for g, m in 문항.items():
            if g not in 예측:
                continue
            p, a = 예측[g]
            사업 = eval_store.사업키(m["사업명"])
            bα, bβ = 골든비목.get(g), (dryrows.get(g) or {}).get("비목")
            rα, rβ = 한도(사업, bα), 한도(사업, bβ)
            행.append({
                "gold_id": g, "사업키": 사업, "예측": p, "정답": a,
                "비목α": bα, "비목β": bβ,
                "한도α": None if rα is None else bool(rα["한도목록"]),
                "한도β": None if rβ is None else bool(rβ["한도목록"]),
                "금액비교사유α": (rα or {}).get("금액비교", {}) and
                (rα["금액비교"] or {}).get("사유"),
                "질문에금액": bool(_금액.search(m["질문"])),
            })

    out = {
        "run": RUN, "문항": len(행),
        "닻α_골든비목_있음": sum(1 for r in 행 if r["비목α"]),
        "닻β_dry비목_있음": sum(1 for r in 행 if r["비목β"]),
        "질문에금액": sum(1 for r in 행 if r["질문에금액"]),
        "전체": 지표(행),
    }
    for 닻, k in (("α 골든비목", "한도α"), ("β dry비목", "한도β")):
        out[f"닻{닻}"] = {
            "한도있는 비목": 지표([r for r in 행 if r[k] is True]),
            "한도없는 비목": 지표([r for r in 행 if r[k] is False]),
            "룰/비목 없음": 지표([r for r in 행 if r[k] is None]),
        }
    out["금액비교_사유(닻α·한도있음)"] = dict(Counter(
        r["금액비교사유α"] for r in 행 if r["한도α"] is True))
    out["곁가지_한도O_금액O(닻α)"] = 지표(
        [r for r in 행 if r["한도α"] is True and r["질문에금액"]])
    out["곁가지_한도O_금액X(닻α)"] = 지표(
        [r for r in 행 if r["한도α"] is True and not r["질문에금액"]])

    (ROOT / "scratchpad" / "P2_한도_쏠림.json").write_text(
        json.dumps({"요약": out, "행": 행}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"run {RUN} · 문항 {len(행)} · 골든비목 {out['닻α_골든비목_있음']} · "
          f"dry비목 {out['닻β_dry비목_있음']} · 질문에금액 {out['질문에금액']}")
    for key in ("전체",):
        print(f"\n[{key}] {json.dumps(out[key], ensure_ascii=False)}")
    for 닻 in ("닻α 골든비목", "닻β dry비목"):
        print(f"\n── {닻} ──")
        print(f"{'갈래':<14}{'문항':>5}{'일치율':>8}{'쏠림분모':>9}{'쏠림':>6}{'쏠림률':>8}   내역")
        for 갈래, v in out[닻].items():
            print(f"{갈래:<14}{v['문항']:>5}{str(v['일치율']):>8}"
                  f"{v['쏠림분모(정답≠조건부)']:>9}{v['쏠림']:>6}"
                  f"{str(v['쏠림률']):>8}   {v['쏠림_내역']}")
    print("\n[곁가지 · 닻α 한도있는 비목만]")
    for k in ("곁가지_한도O_금액O(닻α)", "곁가지_한도O_금액X(닻α)"):
        v = out[k]
        print(f"  {k:<24} 문항 {v['문항']:>3} 쏠림 {v['쏠림']}/{v['쏠림분모(정답≠조건부)']}"
              f" = {v['쏠림률']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
