# -*- coding: utf-8 -*-
"""P2 — 금지예시 매칭을 «단어 대조 → 의미 대조» 로 바꾸면 수가 어떻게 되나. 측정 전용.

배선하지 않는다. `rule_lookup.py`·`orchestrate.py`·`retrieve.py` 를 한 글자도 안 고친다.
여기서 하는 건 **지금 코드가 게이트 A 에서 보는 것과 똑같은 후보 집합**에 대해
문자열 매칭 대신 코사인을 재 보는 것뿐이다.

## 후보 집합 — 스코프를 지키는 게 이 측정의 절반이다

게이트 A 는 `금지적중(cur, 품목, 용도, 사업명, 비목)` → `base_룰(cur, 사업명, 비목)` 이다.
그러니 문항마다 **그 사업에 실제로 붙는 룰의 금지예시만** 후보다. 294개 전부와 대조하면
적중도 오탐도 같이 부풀어 숫자가 무의미해진다. TIPS 처럼 룰 0행인 사업은 후보가 0이라
임베딩을 아무리 잘 해도 안 열린다 — 그 자체가 결과다.

비목은 `None` 으로 둔다. 기준선에서 비목확정이 43/93 실패했고, 실패하면 게이트 A 는
비목 필터 없이 사업 전체를 본다. 후보가 가장 넓은 = 적중에 가장 유리한 조건이다.

후보에서 빼는 것 (지금 코드와 동일):
  · 예외단서가 붙은 금지예시 — 즉답 불가의 재료가 아니다 (`금지예시_해부().무조건`)
  · 정규형 4자 미만 핵 (`_최소핵길이`)

## 🔴 입력(품목·용도)이 없다 — 그래서 두 축으로 잰다

ai-e8 지시는 「93문항의 품목·용도를 벡터화」다. 그런데 그 품목·용도가 어디에도 없다:

  · run 191(실제 LLM 정규화) — `run_items.원출력` 에 `정규화` 키가 없고 `eval_e2e` 는
    `기록=False` 라 `tenant.decisions` 에도 안 남는다 → 복원 불가 (레인기록 §3)
  · `eval.golden_set.입력필드` — 93/93 전부 `null` (이 스크립트가 확인한다)
  · dry 정규화 — 있긴 한데 **용도가 93/93 빈 문자열**이고 품목은 「출장 갈」·「사업비 산」
    같은 조각이다 (레인기록 관측 B)

없는 입력을 있는 척 만들 수 없으니 «둘 다» 재고 각각의 편향을 적는다:

    축1  dry 품목+용도   오늘 배관이 실제로 쥔 본문. 조각이라 **과소**로 나온다. 하한.
    축2  질문 원문        품목·용도 정보를 다 담고 있지만 군더더기도 같이 담는다.
                         🔴 배포 조건이 아니다 — 게이트 A 는 질문 원문을 안 본다.
                         「어휘가 병목이면 임베딩이 그걸 넘나」를 보는 용도다.

참값은 둘 사이에 있다. 한 축만 보고 결론 내지 않는다.

## 오탐의 정의 — 이게 진짜 비용이다

게이트 A 는 **LLM 0회로 「불가」를 근거까지 달아 즉답**한다. 그래서
    참불가 = 임계 넘김 ∧ 정답판정 == '불가'
    오탐   = 임계 넘김 ∧ 정답판정 != '불가'   (조건부·가능·판단불가 전부)
순증 = 참불가 − 오탐. 채택기준 ①은 「치명 오답 0」이라 **오탐 1건이면 그 임계는 탈락**이다.

## 🔴 정답 유출 방지
정답셋 문항은 어떤 인덱스에도 안 들어간다. 임베딩은 메모리에서만 만들고 파일에 안 쓴다.
산출 JSON 에는 gold_id · 정답판정 · 코사인 · 걸린 금지예시(코퍼스 텍스트)만 남긴다 —
질문 원문도 벡터도 안 남긴다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_의미매칭_측정.py
산출: scratchpad/P2_의미매칭_임계값.json + 표준출력 표
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np                                                   # noqa: E402
import psycopg                                                       # noqa: E402
from _lib import db                                                  # noqa: E402
import eval_store                                                    # noqa: E402
import rule_lookup                                                   # noqa: E402

임계목록 = [round(0.50 + 0.025 * i, 3) for i in range(19)]            # 0.500 ~ 0.950


def main() -> int:
    base = json.loads((ROOT / "scratchpad" / "P2_적중_baseline.json")
                      .read_text(encoding="utf-8"))["행"]
    dry = {r["gold_id"]: r for r in base}

    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        문항 = eval_store.평가대상(cur)

        # 입력필드가 정말 비었나 — 「없다」는 주장에는 방금 돌린 출력을 붙인다
        cur.execute("SELECT count(*) FILTER (WHERE 입력필드 IS NULL OR 입력필드='{}'::jsonb),"
                    " count(*) FROM eval.golden_set WHERE gold_id = ANY(%s)",
                    ([m["gold_id"] for m in 문항],))
        입력필드_빈, 입력필드_분모 = cur.fetchone()

        # ── 문항별 후보 핵 (스코프 준수) ────────────────────────────────────
        캐시: dict[str | None, list[dict]] = {}
        행 = []
        for m in 문항:
            사업 = eval_store.사업키(m["사업명"])
            if 사업 not in 캐시:
                핵들: dict[str, dict] = {}
                for r in rule_lookup.base_룰(cur, 사업, None):
                    for e in r["금지예시"]:
                        h = rule_lookup.금지예시_해부(e)
                        if not h["무조건"]:
                            continue
                        if len(h["핵_정규형"]) < rule_lookup._최소핵길이:
                            continue
                        핵들.setdefault(h["핵_정규형"],
                                       {"핵": h["핵"], "rule_id": r["rule_id"],
                                        "비목": r["비목"], "layer": r["layer"]})
                캐시[사업] = list(핵들.values())
            d = dry.get(m["gold_id"], {})
            행.append({
                "gold_id": m["gold_id"], "사업키": 사업, "정답판정": m["정답판정"],
                "후보수": len(캐시[사업]),
                "_축1": f"{d.get('품목','')} {d.get('용도','')}".strip(),
                "_축2": m["질문"],
            })

    # ── 임베딩 (메모리 전용) ────────────────────────────────────────────────
    전체핵 = {}
    for cands in 캐시.values():
        for c in cands:
            전체핵.setdefault(c["핵"], c)
    핵텍스트 = list(전체핵)
    print(f"금지예시 핵 {len(핵텍스트)}종 임베딩 (KURE-v1, CPU) ...", flush=True)
    H = rule_lookup.임베딩(핵텍스트)
    핵idx = {t: i for i, t in enumerate(핵텍스트)}

    # ── 🔴 음성 대조 — 「작은 분리」가 우연보다 큰가 ──────────────────────────
    # 문항을 «다른 사업» 의 후보 풀과 붙인다. 정답과 무관한 짝이니 여기서 나오는
    # 적중·오탐이 곧 우연 수준이다. 본 측정이 이걸 못 넘으면 신호가 없는 것이다.
    풀키 = sorted(캐시, key=lambda k: (k is None, k or ""))
    엇갈림 = {k: 풀키[(i + 1) % len(풀키)] for i, k in enumerate(풀키)}

    결과 = {}
    for 축 in ("_축1", "_축2"):
        텍스트 = [r[축] for r in 행]
        print(f"{축} 본문 {len(텍스트)}건 임베딩 ...", flush=True)
        Q = rule_lookup.임베딩([t or " " for t in 텍스트])

        def 재기(풀선택):
            out = []
            for i, r in enumerate(행):
                풀 = 캐시[풀선택(r["사업키"])]
                cand = [핵idx[c["핵"]] for c in 풀]
                if not cand or not r[축]:
                    out.append({"gold_id": r["gold_id"], "정답판정": r["정답판정"],
                                "후보수": r["후보수"], "cos": None, "핵": None})
                    continue
                s = H[cand] @ Q[i]
                j = int(np.argmax(s))
                out.append({"gold_id": r["gold_id"], "정답판정": r["정답판정"],
                            "후보수": r["후보수"], "cos": round(float(s[j]), 4),
                            "핵": 풀[j]["핵"]})
            return out

        def 곡선내기(최대):
            c = []
            for t in 임계목록:
                적중 = [x for x in 최대 if x["cos"] is not None and x["cos"] >= t]
                참 = sum(1 for x in 적중 if x["정답판정"] == "불가")
                오 = len(적중) - 참
                c.append({"임계": t, "적중": len(적중), "참불가": 참, "오탐": 오,
                          "순증": 참 - 오,
                          "오탐_판정": dict(Counter(x["정답판정"] for x in 적중
                                                  if x["정답판정"] != "불가"))})
            return c

        최대 = 재기(lambda k: k)
        대조 = 재기(lambda k: 엇갈림[k])
        곡선 = 곡선내기(최대)
        불가cos = sorted(x["cos"] for x in 최대
                        if x["정답판정"] == "불가" and x["cos"] is not None)
        비불가cos = sorted(x["cos"] for x in 최대
                         if x["정답판정"] != "불가" and x["cos"] is not None)

        def 분위(v, p):
            return round(v[int(p * (len(v) - 1))], 4) if v else None

        결과[축] = {
            "최대코사인_분포": {
                "불가": {"n": len(불가cos), "중앙": 분위(불가cos, .5),
                       "p90": 분위(불가cos, .9), "최대": 분위(불가cos, 1.0)},
                "비불가": {"n": len(비불가cos), "중앙": 분위(비불가cos, .5),
                        "p90": 분위(비불가cos, .9), "최대": 분위(비불가cos, 1.0)},
            },
            "곡선": 곡선,
            "음성대조_곡선": 곡선내기(대조),
            "행": 최대,
        }

    요약 = {
        "문항": len(행), "정답분포": dict(Counter(r["정답판정"] for r in 행)),
        "후보수_분포": dict(Counter(r["후보수"] for r in 행)),
        "후보0_문항": sum(1 for r in 행 if r["후보수"] == 0),
        "고유핵_전체": len(핵텍스트),
        "golden_set_입력필드_빈": f"{입력필드_빈}/{입력필드_분모}",
        "임베더": "nlpai-lab/KURE-v1 / cpu / max_seq=128 / normalize=True (rule_lookup.임베딩)",
        "후보규칙": "base_룰(사업키, 비목=None) → 무조건 금지예시 → 핵 정규형 4자 이상, 문항내 중복 제거",
    }
    (ROOT / "scratchpad" / "P2_의미매칭_임계값.json").write_text(
        json.dumps({"요약": 요약, "축1_dry품목용도": 결과["_축1"],
                    "축2_질문원문": 결과["_축2"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    print(json.dumps(요약, ensure_ascii=False, indent=2))
    for 축, 이름 in (("_축1", "축1 dry 품목+용도"), ("_축2", "축2 질문 원문")):
        print(f"\n── {이름} ──  최대코사인 {결과[축]['최대코사인_분포']}")
        print("임계  적중  참불가  오탐  순증  │ 음성대조(적중/참/오)")
        for c, n in zip(결과[축]["곡선"], 결과[축]["음성대조_곡선"]):
            print(f"{c['임계']:.3f}  {c['적중']:4d}  {c['참불가']:5d}  "
                  f"{c['오탐']:4d}  {c['순증']:+4d}  │ "
                  f"{n['적중']:3d}/{n['참불가']:2d}/{n['오탐']:2d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
