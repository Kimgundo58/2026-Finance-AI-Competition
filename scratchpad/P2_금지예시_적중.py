# -*- coding: utf-8 -*-
"""P2 — 금지예시 매칭 적중 하네스 (읽기 전용).

`docs/9-4` ②: 「불가」의 유일한 룰 경로인 금지예시 문자열 매칭이 정답셋에서 0건 적중이다.
그 0 이 정말 0 인지, 0 이 아니게 만들면 무엇이 깨지는지를 재는 저울이다.

## 진입점 — P4 와 같은 것을 쓴다 (ai-e8 지시, 2026-09-03)

    orchestrate.판정(질문, 사업명=사업, dry=True, 기록=False)

`eval_e2e.실행()` 은 부르지 않는다 — 그건 `eval.runs` 에 행을 쓴다.
`dry=True` 는 orchestrate.py:619 에서 멈추고 :630 이 `기록=False` 로 마무리하므로
`tenant.decisions` 에 한 행도 안 쓴다. 커넥션도 `read_only` 로 연다 (이중 잠금).

금지예시가 적중하면 판정은 **게이트 A 에서 즉답 「불가」로 끝난다**(orchestrate.py:544~555).
그래서 적중 여부는 반환 dict 의 `게이트 == "A"` 로 읽는다. 갈래별 진단(예외단서가 달려
즉답 대상이 아닌 근접 항목 · 허용예시 충돌)은 판정이 실제로 쓴 품목·용도·비목을 그대로
`rule_lookup.금지후보()` 에 다시 먹여 얻는다 — 같은 입력이라 두 수가 갈리지 않는다.

🔴 dry 의 `규칙_정규화()` 는 실전과 반환 모양이 다르다(문자열 리스트) — orchestrate.py:471~475.
   품목·용도를 꺼낼 때 모양을 가정하지 않고 걸러 받는다. 여기서 조용히 빈 문자열이 되면
   「적중 0」이 하네스 탓인지 데이터 탓인지 못 가른다. `정규화_모양` 칸에 실제 모양을 남긴다.

## 재는 것 — 이득과 손실을 같이 센다 (ai-e8 지시)

    적중        게이트 A 로 즉답 「불가」가 나간 문항 수
    참불가      정답=불가 ∧ 적중                    (이득)
    🔴 틀린불가  정답≠불가 ∧ 적중                    (손해 — 0 이어야 한다)
    🔴 놓침      정답=불가 ∧ 미적중                  (손실 쪽 분모. 적중만 세면 결론이 뒤집힌다)
    허용충돌    적중했는데 같은 룰 행의 허용예시에도 걸린 문항
    조건부근접  예외단서가 달려 즉답 대상이 아닌 항목만 걸린 문항 (진단용)

판단 기준은 「적중이 늘었다」가 아니라 «참불가 − 틀린불가» 다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_금지예시_적중.py
    PYTHONIOENCODING=utf-8 python scratchpad/P2_금지예시_적중.py --out scratchpad/P2_적중_after.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg                                                       # noqa: E402
from _lib import db                                                  # noqa: E402
import eval_store                                                    # noqa: E402
import orchestrate                                                   # noqa: E402
import rule_lookup                                                   # noqa: E402


def _문자열(v) -> str:
    """dry 와 실전이 모양이 달라도 같은 문자열이 나오게. 모양을 가정하지 않는다."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(_문자열(x) for x in v)
    if isinstance(v, dict):
        return _문자열(v.get("품목") or v.get("용도") or v.get("값") or "")
    return str(v)


def 한문항(conn, m: dict) -> dict:
    사업 = eval_store.사업키(m["사업명"])
    # conn 을 넘기면 orchestrate 가 `닫기=False` 로 잡는다(:428) — 커넥션을 뺏기지 않는다
    r = orchestrate.판정(m["질문"], 사업명=사업, dry=True, 기록=False, conn=conn)

    정규 = r.get("정규화") or {}
    품목 = _문자열(정규.get("품목")) or m["질문"][:40]
    용도 = _문자열(정규.get("용도"))
    비목 = r.get("비목")
    적중 = r.get("게이트") == "A"

    # 판정이 실제로 쓴 입력 그대로 다시 먹여 갈래를 본다 — 같은 입력이라 수가 안 갈린다
    cur = conn.cursor()
    cand = rule_lookup.금지후보(cur, 품목, 용도, 사업, 비목)
    무조건, 조건부 = cand["무조건"], cand["조건부"]

    본문 = rule_lookup._norm(f"{품목} {용도}")
    충돌 = []
    for h in 무조건:
        for b in rule_lookup.base_룰(cur, 사업, 비목):
            if b["rule_id"] != h["rule_id"]:
                continue
            for a in b["허용예시"]:
                핵 = rule_lookup.금지예시_해부(a)["핵_정규형"]
                if len(핵) >= rule_lookup._최소핵길이 and 핵 in 본문:
                    충돌.append({"rule_id": b["rule_id"], "허용예시": a, "금지예시": h["예시"]})
    return {
        "gold_id": m["gold_id"], "세트": m["세트"], "사업명": m["사업명"], "사업키": 사업,
        "질문": m["질문"], "정답판정": m["정답판정"], "정답비목": m["비목"],
        "품목": 품목, "용도": 용도, "비목": 비목,
        "게이트": r.get("게이트"), "경로": r.get("경로"), "적중": 적중,
        "금지근거": r.get("금지근거"),
        "정규화_모양": {k: type(v).__name__ for k, v in 정규.items()},
        "무조건": 무조건, "조건부근접": 조건부, "허용충돌": 충돌,
    }


def 표(행들: list[dict]) -> dict:
    적중 = [r for r in 행들 if r["적중"]]
    불가 = [r for r in 행들 if r["정답판정"] == "불가"]
    참불가 = [r for r in 적중 if r["정답판정"] == "불가"]
    틀린불가 = [r for r in 적중 if r["정답판정"] != "불가"]
    놓침 = [r for r in 불가 if not r["적중"]]
    충돌 = [r for r in 적중 if r["허용충돌"]]
    근접 = [r for r in 행들 if not r["적중"] and r["조건부근접"]]
    return {
        "문항": len(행들),
        "정답분포": dict(Counter(r["정답판정"] for r in 행들)),
        "적중": len(적중), "참불가": len(참불가), "틀린불가": len(틀린불가),
        "순증(참불가-틀린불가)": len(참불가) - len(틀린불가),
        "놓침(정답=불가∧미적중)": len(놓침), "정답=불가": len(불가),
        "허용충돌": len(충돌), "조건부근접": len(근접),
        "비목확정_실패": sum(1 for r in 행들 if not r["비목"]),
        "게이트분포": dict(Counter(r["게이트"] for r in 행들)),
        "세트별_적중": dict(Counter(r["세트"] for r in 적중)),
        "적중_gold_ids": [r["gold_id"] for r in 적중],
        "틀린불가_gold_ids": [r["gold_id"] for r in 틀린불가],
        "조건부근접_gold_ids": [r["gold_id"] for r in 근접],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "scratchpad" / "P2_적중_baseline.json"))
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    t0 = time.time()
    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True                      # 🔴 DB 는 한 행도 쓰지 않는다 (이중 잠금)
        cur = conn.cursor()
        문항 = eval_store.평가대상(cur)
        if a.limit:
            문항 = 문항[: a.limit]
        행들 = [한문항(conn, m) for m in 문항]

    s = 표(행들)
    s["플래그"] = {"SUDDOE_금지예시_별칭": os.environ.get("SUDDOE_금지예시_별칭"),
                  "SUDDOE_RULE_OVERLAY": os.environ.get("SUDDOE_RULE_OVERLAY")}
    s["경과초"] = round(time.time() - t0, 1)

    Path(a.out).write_text(json.dumps({"요약": s, "행": 행들}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"\n→ {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
