# -*- coding: utf-8 -*-
"""P2 — 금지예시 294개 중 «사용자 문장에 걸릴 법한» 것이 몇 개인가. 읽기 전용.

이건 「불가 경로를 살릴 수 있나」의 **상한**이다. 게이트 A 는 `핵n in (품목n+용도n)` 이라
사용자가 그 말을 안 쓰면 별칭을 아무리 채워도 안 걸린다.

## 🔴 세는 규칙 — 먼저 적고 시작한다 (ai-e8 지시)

두 규칙으로 따로 센다. **닻이 다르다** — A 는 문자열 모양만 보고, B 는 실제 사용자 문장을 본다.

### 규칙 A — 구조적 상한 (정답셋을 안 본다)

    ① 무조건            예외단서 붙은 건 게이트 A 대상이 아니다 (`금지예시_해부().무조건`)
    ② 핵_정규형 ≥ 4자    `rule_lookup._최소핵길이`
    ③ 갈래 ∉ {C1, C2}   조건서술형·규범참조형은 사용자가 쓰는 말이 아니다
                        (갈래 규칙은 `P2_핵_분류.py` 와 **같은 정규식**을 쓴다)
    ④ 어절 수 ≤ N        사용자가 품목란에 그대로 칠 수 있는 길이
    🔴 N 을 하나로 안 박는다. N=2 와 N=3 을 **둘 다** 낸다 — 규칙이 바뀌면 수가 변하고,
       「규칙이 안 적힌 수는 수가 아니다」의 반대편은 「규칙 하나에 수를 걸지 않는다」다.

### 규칙 B — 실측 상한 (사용자가 실제로 쓴 한국어에 있나)

    핵의 **모든 어절**이 정답셋 93문항의 질문 원문 어느 하나에 (정규화 후) 등장하는가.

    🔴 이건 «관측» 이지 «재료» 가 아니다. 정답셋을 인덱스에 넣지 않고, 여기서 나온
       어휘로 별칭을 만들지도 않는다(정답 유출). 어휘가 겹치는지만 센다.
    🔴 어절 단위다. 부분문자열로 세면 0 이 5 로 보인다(2026-09-03 실측).

분모는 둘 다 낸다 — 금지예시 294(원소) · 고유핵 155(중복 제거).

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_사용자표기_상한.py
산출: scratchpad/P2_사용자표기_상한.json
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

# `P2_핵_분류.py` 와 동일 — 갈리면 두 문서의 수가 안 맞는다
_C2 = re.compile(r"제\s*\d+\s*조|「[^」]+」|법\s*제|민법|고용노동부|전문기관의\s*장|"
                 r"사업운영위원회|중소벤처기업부|창업진흥원")
_C1 = re.compile(r"않은|않는|아닌|없는|없이|되지|하지|못한|초과|미만|이상|이하|"
                 r"미가입|미소진|미구입|미제출|벗어난|남는|지난|이내|이전|이후|전까지|"
                 r"경우|받지|허위|임의\s*처분|부적정|연관성이|부족")


def 갈래(핵: str) -> str:
    if _C2.search(핵):
        return "C2 규범참조형"
    if _C1.search(핵):
        return "C1 조건서술형"
    return "C3 복합명사구" if len(핵.split()) >= 3 else "C4 단순지출명"


def main() -> int:
    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT rule_id, 사업명, 비목, 금지예시 FROM corpus.rules "
                    "WHERE cardinality(금지예시) > 0 ORDER BY rule_id")
        예시 = []
        for rid, 사업, 비목, ex in cur.fetchall():
            for e in ex:
                h = rule_lookup.금지예시_해부(e)
                예시.append({"rule_id": rid, "사업명": 사업, "비목": 비목, "원문": e,
                             "핵": h["핵"], "핵n": h["핵_정규형"], "무조건": h["무조건"],
                             "갈래": 갈래(h["핵"]), "어절": len(h["핵"].split())})
        문항 = eval_store.평가대상(cur)

    # 규칙 B 준비 — 질문 원문의 어절 집합 (정규화 후). 관측 전용
    질문어절: set[str] = set()
    for m in 문항:
        for t in m["질문"].split():
            n = rule_lookup._norm(t)
            if n:
                질문어절.add(n)

    def B적중(핵: str) -> bool:
        토큰 = [rule_lookup._norm(t) for t in 핵.split()]
        토큰 = [t for t in 토큰 if t]
        return bool(토큰) and all(t in 질문어절 for t in 토큰)

    for x in 예시:
        x["기본"] = x["무조건"] and len(x["핵n"]) >= rule_lookup._최소핵길이
        x["갈래통과"] = x["갈래"] not in ("C1 조건서술형", "C2 규범참조형")
        x["A_N2"] = x["기본"] and x["갈래통과"] and x["어절"] <= 2
        x["A_N3"] = x["기본"] and x["갈래통과"] and x["어절"] <= 3
        x["B"] = x["기본"] and B적중(x["핵"])

    # 🔴 고유핵 분모가 둘이다 — 섞으면 다음 사람이 또 센다.
    #    167 = 294 전체의 고유핵 · 155 = 무조건만의 고유핵(`P2_핵_분류` 가 쓴 수).
    #    같은 핵이 무조건과 예외단서로 둘 다 나오면 **무조건 쪽을 남긴다**
    #    (`setdefault` 만 쓰면 먼저 온 쪽이 이겨 A 가 과소계수된다).
    고유: dict[str, dict] = {}
    for x in 예시:
        앞 = 고유.get(x["핵n"])
        if 앞 is None or (x["무조건"] and not 앞["무조건"]):
            고유[x["핵n"]] = x
    고유값 = list(고유.values())
    고유_무조건 = [r for r in 고유값 if r["무조건"]]

    def 세기(rows, key):
        return sum(1 for r in rows if r[key])

    요약 = {
        "분모": {"금지예시_원소": len(예시),
               "고유핵_전체": len(고유값),
               "고유핵_무조건": len(고유_무조건),
               "무조건_원소": 세기(예시, "무조건"),
               "기본통과(무조건∧4자이상)": 세기(예시, "기본"),
               "🔴": "고유핵 167 은 294 전체 기준, 155 는 무조건 기준이다. "
                     "아래 비율의 분모는 155(=고유핵_무조건)"},
        "규칙A_구조적상한": {
            "어절<=2 (N2)": {"예시": 세기(예시, "A_N2"), "고유핵": 세기(고유값, "A_N2")},
            "어절<=3 (N3)": {"예시": 세기(예시, "A_N3"), "고유핵": 세기(고유값, "A_N3")},
        },
        "규칙B_질문어휘_전어절적중": {"예시": 세기(예시, "B"), "고유핵": 세기(고유값, "B")},
        "A∩B (N3)": {"고유핵": sum(1 for r in 고유값 if r["A_N3"] and r["B"])},
        "갈래_고유핵": dict(Counter(r["갈래"] for r in 고유값 if r["기본"])),
        "세는규칙": {
            "A": "무조건 ∧ 핵정규형>=4자 ∧ 갈래∉{C1,C2} ∧ 어절<=N (N=2,3 둘 다 보고)",
            "B": "무조건 ∧ 핵정규형>=4자 ∧ 핵의 모든 어절이 93문항 질문 어절집합에 있음",
            "갈래": "C2→C1→C3→C4 순, P2_핵_분류.py 와 같은 정규식",
            "🔴": "B 는 관측이다. 여기서 나온 어휘로 별칭을 만들지 않는다(정답 유출)",
        },
    }
    출력 = {
        "요약": 요약,
        "A_N3_고유핵": sorted(r["핵"] for r in 고유값 if r["A_N3"]),
        "B_고유핵": sorted(r["핵"] for r in 고유값 if r["B"]),
        "A_N3이지만_B아님": sorted(r["핵"] for r in 고유값 if r["A_N3"] and not r["B"]),
        "B이지만_A_N3아님": sorted(r["핵"] for r in 고유값 if r["B"] and not r["A_N3"]),
    }
    (ROOT / "scratchpad" / "P2_사용자표기_상한.json").write_text(
        json.dumps({**출력, "예시": 예시}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(출력, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
