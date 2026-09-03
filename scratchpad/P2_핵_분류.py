# -*- coding: utf-8 -*-
"""P2 — 금지예시 「핵」이 무슨 모양인지 규칙으로 센다. 읽기 전용.

`docs/9-4` ②는 「예외단서 없는 금지예시 158종 중 약 87%가 조문 인용체」라고 적으면서
그 수가 **길이 12자 대리지표**임을 스스로 밝혀 뒀다. 여기서는 길이를 안 쓰고 규칙으로 센다.

## 🔴 분류 규칙 — 보고에 그대로 옮긴다 (규칙이 안 적힌 수는 수가 아니다)

`금지예시_해부()` 가 돌려준 **핵**(괄호를 뺀 본문)에 대해, 아래 순서로 **먼저 걸리는 하나**에
배정한다. 상호배타이고 순서가 결과를 바꾼다 — 그래서 순서를 여기 박아 둔다.

    C2 규범참조형   `제\\d+조` · `「…」` · `법 제` · 기관/기구명(전문기관의 장·사업운영위원회·
                    고용노동부·민법)이 핵 안에 있다 → 사용자가 쓸 수 있는 말이 아니다
    C1 조건서술형   부정·조건·기간·한도 표지가 있다 (닫힌 목록, 아래 `_C1` 그대로)
                    → 「무엇을 샀나」가 아니라 「어떤 경우인가」를 적은 것이다
    C3 복합명사구   어절 3개 이상 (C1·C2 아님)
    C4 단순지출명   어절 2개 이하 → 사용자가 실제로 타이핑할 수 있는 모양

어절은 공백 기준으로 센다(NFKC 후). `·` 로 이은 나열은 한 어절로 본다 — 원문이 그렇게 붙여 쓴다.

## 🔴 두 번째 축 — 매칭 «방향»

`금지후보()` 는 `핵n in 본문` 을 본다. 본문은 정규화된 «품목 + 용도» 다.
핵이 본문보다 길면 이 조건은 **길이만으로 성립이 불가능**하다. 그래서 같이 센다:

    포함가능      len(핵n) <= len(본문n)  인 (문항, 핵) 쌍이 하나라도 있나
    역방향적중    품목n 이 핵n 안에 들어 있나 — 즉 «사용자 표현 ⊂ 금지예시» 인가
                  🔴 이건 진단용이지 채택 후보가 아니다. 짧은 품목이 긴 서술에 우연히
                  박히면 오탐이 쏟아진다(「거래처」·「임차료」). 그래서 아래 둘로 갈라 센다:
                    역방향_전체   제약 없이 부분문자열
                    역방향_경계   품목n 길이 >= 4 이고, 핵의 **어절 하나와 정확히 같을 때만**
                  ai-e8 경고(2026-09-03): 부분문자열로만 세면 0 이 5 로 보인다

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_핵_분류.py
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

_C2 = re.compile(r"제\s*\d+\s*조|「[^」]+」|법\s*제|민법|고용노동부|전문기관의\s*장|"
                 r"사업운영위원회|중소벤처기업부|창업진흥원")
_C1 = re.compile(r"않은|않는|아닌|없는|없이|되지|하지|못한|초과|미만|이상|이하|"
                 r"미가입|미소진|미구입|미제출|벗어난|남는|지난|이내|이전|이후|전까지|"
                 r"경우|받지|허위|임의\s*처분|부적정|연관성이|부족")


def 분류(핵: str) -> str:
    if _C2.search(핵):
        return "C2 규범참조형"
    if _C1.search(핵):
        return "C1 조건서술형"
    return "C3 복합명사구" if len(핵.split()) >= 3 else "C4 단순지출명"


def main() -> int:
    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT rule_id, layer, 사업명, 비목, 금지예시 FROM corpus.rules "
                    "WHERE cardinality(금지예시) > 0 ORDER BY rule_id")
        예시들 = []
        for rid, layer, 사업, 비목, ex in cur.fetchall():
            for e in ex:
                h = rule_lookup.금지예시_해부(e)
                예시들.append({"rule_id": rid, "layer": layer, "사업명": 사업, "비목": 비목,
                              "원문": e, "핵": h["핵"], "핵n": h["핵_정규형"],
                              "무조건": h["무조건"], "갈래": 분류(h["핵"])})
        문항 = eval_store.평가대상(cur)

    무조건 = [x for x in 예시들 if x["무조건"]]
    고유핵 = {}
    for x in 무조건:
        고유핵.setdefault(x["핵n"], x)

    # ── 방향 축: 하네스가 실제로 쓴 품목·용도를 재사용한다 ─────────────────────
    base = ROOT / "scratchpad" / "P2_적중_baseline.json"
    방향 = None
    if base.exists():
        행 = json.loads(base.read_text(encoding="utf-8"))["행"]
        핵목록 = [(k, v) for k, v in 고유핵.items() if len(k) >= rule_lookup._최소핵길이]
        포함가능 = 역_전체 = 역_경계 = 0
        역예 = []
        for r in 행:
            본문n = rule_lookup._norm(f"{r['품목']} {r['용도']}")
            품목n = rule_lookup._norm(r["품목"])
            if any(len(k) <= len(본문n) for k, _ in 핵목록):
                포함가능 += 1
            hit_all = [v for k, v in 핵목록 if 품목n and 품목n in k]
            hit_tok = [v for k, v in 핵목록
                       if len(품목n) >= 4 and 품목n in {rule_lookup._norm(t)
                                                      for t in v["핵"].split()}]
            역_전체 += bool(hit_all)
            역_경계 += bool(hit_tok)
            if hit_tok:
                역예.append({"gold_id": r["gold_id"], "품목": r["품목"],
                            "정답판정": r["정답판정"],
                            "핵": [h["핵"] for h in hit_tok][:4]})
        방향 = {"문항": len(행), "포함가능": 포함가능,
               "역방향_전체": 역_전체, "역방향_경계": 역_경계, "역방향_경계_예": 역예,
               "품목n_길이_중앙": sorted(len(rule_lookup._norm(r["품목"]))
                                     for r in 행)[len(행) // 2]}

    out = {
        "예시_총": len(예시들), "무조건": len(무조건),
        "예외단서": len(예시들) - len(무조건),
        "고유핵_무조건": len(고유핵),
        "갈래_전체": dict(Counter(x["갈래"] for x in 예시들)),
        "갈래_무조건": dict(Counter(x["갈래"] for x in 무조건)),
        "갈래_고유핵": dict(Counter(v["갈래"] for v in 고유핵.values())),
        "C4_고유핵": sorted(v["핵"] for v in 고유핵.values() if v["갈래"] == "C4 단순지출명"),
        "짧은핵_제외": sum(1 for k in 고유핵 if len(k) < rule_lookup._최소핵길이),
        "방향": 방향,
        "분류규칙": {"C2": _C2.pattern, "C1": _C1.pattern,
                   "C3": "어절>=3", "C4": "어절<=2", "순서": "C2 → C1 → C3 → C4"},
    }
    (ROOT / "scratchpad" / "P2_핵_분류.json").write_text(
        json.dumps({"요약": out, "예시": 예시들}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
