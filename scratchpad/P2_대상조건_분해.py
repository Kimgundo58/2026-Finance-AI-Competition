# -*- coding: utf-8 -*-
"""P2 — C1 조건서술형 62핵을 「대상 / 조건」으로 쪼개고 **대상만** 의미 대조한다. 측정 전용.

앞 측정(`docs/기록/_레인_P2_의미매칭`)의 결론은 「임베딩이 부정을 못 읽는다」였다.
「4대사회보험 **미가입** 임직원의 교육훈련비」가 가입한 문항(정답 가능)에 걸린다.
그래서 ai-e8 승인 범위: **대상(교육훈련비)만 의미 대조 · 조건(미가입)은 코드 검사.**
범위는 고유핵 155 전체가 아니라 **C1 조건서술형 62개**에 한정한다. 배선하지 않는다.

## 🔴 분해 규칙 (보고에 그대로 옮긴다)

핵을 공백 어절로 끊고 **오른쪽 끝부터** 훑어 «지출명으로 끝나는 첫 어절» 을 찾는다.
한국어는 머리명사가 뒤에 온다 — 「4대사회보험 미가입 임직원의 **교육훈련비**」.

    지출명접미 = 비|료|금|액|운임|비용|수수료  (어절의 **끝**에 붙어야 한다)
    대상   = 그 어절 (찾은 것)
    조건   = 그 어절을 뺀 나머지 전부 (코드가 검사해야 할 몫. 여기서는 안 쓴다)
    대상없음 = 끝까지 못 찾음 → 🔴 **이 핵은 지출명을 아예 안 담고 있다**

`대상없음` 은 실패가 아니라 결과다. 「시제품과 유사한 제품에 대한 제작 경험이 없는 업체」는
무엇을 샀는지가 문자열에 없다 — 대상만 대조하는 안이 **원리적으로 못 닿는 자리**다.
수를 맞추려고 규칙을 늘리지 않는다. 규칙과 못 잡은 것을 둘 다 적는다.

## 재는 것 — 앞 측정과 같은 배관, 후보만 갈아끼운다

    (a) C1 핵 통째   앞 측정을 C1 62개로 좁힌 것. 비교 기준선
    (b) C1 대상만    같은 문항·같은 임베더·같은 스코프. 후보 문자열만 대상으로 바꾼다

문항·스코프·오탐 정의·음성대조는 `P2_의미매칭_측정.py` 와 동일하다
(후보는 `base_룰(사업키, 비목=None)` 의 무조건 금지예시 중 C1 인 것, 문항 내 중복 제거).
축도 그대로 둘이다 — 축1 dry 품목+용도(실제 배관·하한) · 축2 질문 원문(배포 조건 아님).

## 🔴 판정 기준 — 오탐 0 에서 몇 건인가

ai-e8 지시로 **오탐을 허용하는 임계는 보고에서 뺀다.** 다만 (b)가 (a)보다 오탐이 «늘» 수
있다는 게 이 안의 핵심 위험이다 — 조건을 떼면 「가입한 사람의 교육훈련비」에도 더 잘 걸린다.
그래서 오탐 수 자체를 같이 남긴다: **그 수가 곧 「조건 검사가 걸러내야 할 건수」** 다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_대상조건_분해.py
산출: scratchpad/P2_대상조건_분해.json + 표준출력
"""
from __future__ import annotations

import json
import re
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

# `P2_핵_분류.py` 와 같은 규칙 — 갈래가 갈리면 두 문서의 수가 안 맞는다
_C2 = re.compile(r"제\s*\d+\s*조|「[^」]+」|법\s*제|민법|고용노동부|전문기관의\s*장|"
                 r"사업운영위원회|중소벤처기업부|창업진흥원")
_C1 = re.compile(r"않은|않는|아닌|없는|없이|되지|하지|못한|초과|미만|이상|이하|"
                 r"미가입|미소진|미구입|미제출|벗어난|남는|지난|이내|이전|이후|전까지|"
                 r"경우|받지|허위|임의\s*처분|부적정|연관성이|부족")
_지출명 = re.compile(r"(비|료|금|액|운임|비용|수수료)$")
임계목록 = [round(0.50 + 0.025 * i, 3) for i in range(19)]


def 갈래(핵: str) -> str:
    if _C2.search(핵):
        return "C2"
    if _C1.search(핵):
        return "C1"
    return "C3" if len(핵.split()) >= 3 else "C4"


def 분해(핵: str) -> dict:
    """→ {대상, 조건, 대상있음}. 오른쪽 끝부터 지출명으로 끝나는 첫 어절을 찾는다."""
    어절 = 핵.split()
    for i in range(len(어절) - 1, -1, -1):
        t = 어절[i].strip("·,")
        if _지출명.search(t):
            return {"대상": t, "조건": " ".join(어절[:i] + 어절[i + 1:]), "대상있음": True}
    return {"대상": None, "조건": 핵, "대상있음": False}


def 곡선(최대: list[dict]) -> list[dict]:
    out = []
    for t in 임계목록:
        적중 = [x for x in 최대 if x["cos"] is not None and x["cos"] >= t]
        참 = sum(1 for x in 적중 if x["정답판정"] == "불가")
        out.append({"임계": t, "적중": len(적중), "참불가": 참, "오탐": len(적중) - 참})
    return out


def 세트별(최대: list[dict], 임계: float | None) -> dict:
    """오탐 0 임계에서 세트별 적중. ai-a3 가 쏠림이 «세트» 축으로 갈린다고 잡았다
    (보강 58~60% · 본세트 0% · 공식 0~50%) — 이득이 한 세트에 몰리면 그것도 결과다."""
    if 임계 is None:
        return {}
    out = {}
    for s in sorted({x["세트"] for x in 최대}):
        g = [x for x in 최대 if x["세트"] == s]
        h = [x for x in g if x["cos"] is not None and x["cos"] >= 임계]
        out[s] = {"문항": len(g), "정답불가": sum(1 for x in g if x["정답판정"] == "불가"),
                  "적중": len(h), "참불가": sum(1 for x in h if x["정답판정"] == "불가")}
    return out


def 오탐0(c: list[dict]) -> dict:
    """오탐 0 을 만족하는 가장 낮은 임계와 그때의 참불가."""
    for x in c:
        if x["오탐"] == 0:
            return {"임계": x["임계"], "참불가": x["참불가"]}
    return {"임계": None, "참불가": 0}


def main() -> int:
    dry = {r["gold_id"]: r for r in json.loads(
        (ROOT / "scratchpad" / "P2_적중_baseline.json").read_text(encoding="utf-8"))["행"]}

    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        문항 = eval_store.평가대상(cur)

        캐시: dict[str | None, list[dict]] = {}
        행 = []
        for m in 문항:
            사업 = eval_store.사업키(m["사업명"])
            if 사업 not in 캐시:
                핵들: dict[str, dict] = {}
                for r in rule_lookup.base_룰(cur, 사업, None):
                    for e in r["금지예시"]:
                        h = rule_lookup.금지예시_해부(e)
                        if not h["무조건"] or len(h["핵_정규형"]) < rule_lookup._최소핵길이:
                            continue
                        if 갈래(h["핵"]) != "C1":
                            continue
                        핵들.setdefault(h["핵_정규형"], {"핵": h["핵"], **분해(h["핵"]),
                                                      "rule_id": r["rule_id"]})
                캐시[사업] = list(핵들.values())
            d = dry.get(m["gold_id"], {})
            행.append({"gold_id": m["gold_id"], "사업키": 사업, "세트": m["세트"],
                       "정답판정": m["정답판정"], "후보수": len(캐시[사업]),
                       "_축1": f"{d.get('품목','')} {d.get('용도','')}".strip(),
                       "_축2": m["질문"]})

    전체 = {}
    for cands in 캐시.values():
        for c in cands:
            전체.setdefault(c["핵"], c)
    분해표 = sorted(전체.values(), key=lambda c: (not c["대상있음"], c["핵"]))
    대상있음 = [c for c in 분해표 if c["대상있음"]]

    # ── 임베딩 (메모리 전용) ────────────────────────────────────────────────
    핵텍스트 = [c["핵"] for c in 분해표]
    대상텍스트 = sorted({c["대상"] for c in 대상있음})
    print(f"C1 고유핵 {len(핵텍스트)} · 대상 {len(대상텍스트)}종 임베딩 (KURE-v1, CPU) ...",
          flush=True)
    H = rule_lookup.임베딩(핵텍스트)
    T = rule_lookup.임베딩(대상텍스트)
    Hi = {t: i for i, t in enumerate(핵텍스트)}
    Ti = {t: i for i, t in enumerate(대상텍스트)}

    풀키 = sorted(캐시, key=lambda k: (k is None, k or ""))
    엇갈림 = {k: 풀키[(i + 1) % len(풀키)] for i, k in enumerate(풀키)}

    결과 = {}
    for 축 in ("_축1", "_축2"):
        Q = rule_lookup.임베딩([r[축] or " " for r in 행])

        def 재기(mat, idx, 뽑기, 풀선택):
            out = []
            for i, r in enumerate(행):
                키 = sorted({뽑기(c) for c in 캐시[풀선택(r["사업키"])] if 뽑기(c)})
                기본 = {"gold_id": r["gold_id"], "정답판정": r["정답판정"],
                       "세트": r["세트"]}
                if not 키 or not r[축]:
                    out.append({**기본, "cos": None, "걸린것": None})
                    continue
                s = mat[[idx[k] for k in 키]] @ Q[i]
                j = int(np.argmax(s))
                out.append({**기본, "cos": round(float(s[j]), 4), "걸린것": 키[j]})
            return out

        a = 재기(H, Hi, lambda c: c["핵"], lambda k: k)
        b = 재기(T, Ti, lambda c: c["대상"], lambda k: k)
        b대조 = 재기(T, Ti, lambda c: c["대상"], lambda k: 엇갈림[k])
        결과[축] = {
            "a_C1핵통째": {"곡선": 곡선(a), "오탐0": 오탐0(곡선(a)), "행": a,
                          "세트별": 세트별(a, 오탐0(곡선(a))["임계"])},
            "b_C1대상만": {"곡선": 곡선(b), "오탐0": 오탐0(곡선(b)), "행": b,
                          "세트별": 세트별(b, 오탐0(곡선(b))["임계"])},
            "b_음성대조": {"곡선": 곡선(b대조), "오탐0": 오탐0(곡선(b대조))},
        }

    out = {
        "C1_고유핵": len(분해표),
        "대상있음": len(대상있음), "대상없음": len(분해표) - len(대상있음),
        "대상_종류": len(대상텍스트), "대상_목록": 대상텍스트,
        "대상없음_핵": [c["핵"] for c in 분해표 if not c["대상있음"]],
        "분해규칙": {"지출명접미": _지출명.pattern, "방향": "어절 오른쪽 끝부터 첫 일치",
                  "대상없음": "끝까지 못 찾으면 지출명을 안 담은 핵"},
        "임베더": "nlpai-lab/KURE-v1 / cpu / max_seq=128 / normalize (rule_lookup.임베딩)",
    }
    (ROOT / "scratchpad" / "P2_대상조건_분해.json").write_text(
        json.dumps({"요약": out, "분해": 분해표, "측정": 결과},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nC1 고유핵 {out['C1_고유핵']} = 대상있음 {out['대상있음']} + "
          f"대상없음 {out['대상없음']} · 대상 {out['대상_종류']}종")
    print("대상 목록:", ", ".join(대상텍스트))
    for 축, 이름 in (("_축1", "축1 dry 품목+용도"), ("_축2", "축2 질문 원문")):
        print(f"\n── {이름} ── (오탐 0 기준)")
        for k in ("a_C1핵통째", "b_C1대상만", "b_음성대조"):
            v = 결과[축][k]["오탐0"]
            print(f"  {k:<12} 임계 {str(v['임계']):>5}  참불가 {v['참불가']}"
                  f"   {결과[축][k].get('세트별', '')}")
        print("  임계  a적중/참/오      b적중/참/오")
        for x, y in zip(결과[축]["a_C1핵통째"]["곡선"], 결과[축]["b_C1대상만"]["곡선"]):
            if x["적중"] or y["적중"]:
                print(f"  {x['임계']:.3f}  {x['적중']:3d}/{x['참불가']:3d}/{x['오탐']:3d}"
                      f"      {y['적중']:3d}/{y['참불가']:3d}/{y['오탐']:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
