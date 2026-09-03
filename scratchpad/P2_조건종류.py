# -*- coding: utf-8 -*-
"""P2 — C1 조건서술형 62핵이 요구하는 «조건의 종류» 가 몇 가지인가. 읽기 전용.

ai-e8 의 판정선: 종류가 **5~6개면 필드로 풀린다. 20개가 넘으면 필드로는 안 풀린다.**
이 수가 설계 제안의 핵심 근거다.

## 🔴 세는 규칙

각 핵을 아래 종류에 **닿는 대로 전부** 배정한다(다중 배정 허용). 한 종류만 고르면
「협약종료일 1개월 이내에 채용한 신규 인력」처럼 기간 ∧ 자격을 둘 다 요구하는 핵이
한쪽으로만 세어져 «필드 몇 개가 필요한가» 를 과소평가한다.

    T1 4대보험     4대사회보험 가입 여부
    T2 기간        협약기간·시작일·종료일·이전·이후·전까지·1개월
    T3 완료여부    납품·사용·홍보·구동·소진이 완료됐나
    T4 절차        사전승인·사전검토·사전심의·보고·승인 이전
    T5 자격신분    대표자·직계존비속·배우자·소속 임직원·자격기준 충족
    T6 업체요건    사업자등록·업태·업종·제작경험·재직이력·재하청·연관성
    T7 한도초과    초과·이하·미만·50%·등급
    T8 명의귀속    본인 명의가 아닌 출원·등록·발명권자
    T9 부정행위    허위·임의 처분·부적정·부풀리
    T0 미분류      위 어디에도 안 닿음  ← 🔴 이 수가 크면 분류 자체가 틀린 것이다

**보고에 규칙을 그대로 옮긴다.** 규칙이 바뀌면 수가 변한다 — 그래서 T0 도 같이 낸다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P2_조건종류.py
산출: scratchpad/P2_조건종류.json
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
import rule_lookup                                                   # noqa: E402

_C2 = re.compile(r"제\s*\d+\s*조|「[^」]+」|법\s*제|민법|고용노동부|전문기관의\s*장|"
                 r"사업운영위원회|중소벤처기업부|창업진흥원")
# 🔴 `이전` 교정 (2026-09-03) — 「기술**이전**」·「권리**이전**」은 移轉이지 「以前」이 아니다.
#    교정 전 C1 이 62, 교정 후 **57**. 5핵(기술이전 평가비 2 · 권리이전 발명보상금 3)이
#    조건서술형으로 잘못 세어져 있었다. 앞서 낸 「C1 40%」는 **36.8%(57/155)** 가 맞다.
#    부분문자열이 경계 없이 걸린 사례 — 오늘 이 레인에서 세 번째다.
_C1 = re.compile(r"않은|않는|아닌|없는|없이|되지|하지|못한|초과|미만|이상|이하|"
                 r"미가입|미소진|미구입|미제출|벗어난|남는|지난|이내|"
                 r"(?<!기술)(?<!권리)이전|이후|전까지|"
                 r"경우|받지|허위|임의\s*처분|부적정|연관성이|부족")

종류 = {
    "T1 4대보험": r"4대\s*사회보험|4대\s*보험|고용보험|국민연금",
    "T2 기간": r"협약\s*기간|협약\s*시작일|협약\s*종료|협약일|협약시작일|"
              r"(?<!기술)(?<!권리)이전|이후|전까지|개월|지나지|당해|연도",
    "T3 완료여부": r"완료되지|완료된|납품되지|미소진|실소요되지|구동되지|남는|잔여|이월",
    "T4 절차": r"사전\s*승인|사전\s*검토|사전\s*심의|심의\s*대상|승인\s*이전|보고하지|제출하지|"
              r"거치지",
    "T5 자격신분": r"대표자|직계존비속|형제|자매|배우자|임직원|소속|자격\s*기준|멘토단|"
                r"근로자|채용|퇴사|재직",
    "T6 업체요건": r"사업자등록|업태|업종|종목|제작\s*경험|재하청|연관성|법인등기부|업체|"
                r"프리랜서|임대인",
    "T7 한도초과": r"초과|이하|미만|이상|50%|등급|기준\s*초과|부풀|다량",
    "T8 명의귀속": r"명의|발명권자|출원인|권리이전",
    "T9 부정행위": r"허위|임의\s*처분|부적정|부풀리",
}
_컴 = {k: re.compile(v) for k, v in 종류.items()}


def main() -> int:
    with psycopg.connect(db.DSN) as conn:
        conn.read_only = True
        cur = conn.cursor()
        cur.execute("SELECT 금지예시 FROM corpus.rules WHERE cardinality(금지예시)>0")
        고유: dict[str, str] = {}
        for (ex,) in cur.fetchall():
            for e in ex:
                h = rule_lookup.금지예시_해부(e)
                if not h["무조건"] or len(h["핵_정규형"]) < rule_lookup._최소핵길이:
                    continue
                if _C2.search(h["핵"]) or not _C1.search(h["핵"]):
                    continue                      # C1 만
                고유.setdefault(h["핵_정규형"], h["핵"])

    표 = []
    for 핵 in 고유.values():
        t = [k for k, r in _컴.items() if r.search(핵)]
        표.append({"핵": 핵, "종류": t or ["T0 미분류"], "종류수": len(t)})

    빈도 = Counter(k for r in 표 for k in r["종류"])
    조합 = Counter(" + ".join(sorted(r["종류"])) for r in 표)
    out = {
        "C1_고유핵": len(표),
        "종류별_핵수": dict(빈도.most_common()),
        "쓰인_종류수": len([k for k in 빈도 if k != "T0 미분류"]),
        "T0_미분류": [r["핵"] for r in 표 if r["종류"] == ["T0 미분류"]],
        "핵당_종류수_분포": dict(Counter(max(r["종류수"], 1) for r in 표)),
        "상위_조합": dict(조합.most_common(12)),
        "🔴 커버율": {
            f"상위 {n} 종류로 덮이는 핵": sum(
                1 for r in 표 if set(r["종류"]) & {k for k, _ in 빈도.most_common(n)
                                                  if k != "T0 미분류"})
            for n in (3, 4, 5, 6, 7)
        },
        "세는규칙": {**종류, "다중배정": "닿는 종류 전부에 배정한다(한 핵이 여러 필드를 요구한다)"},
    }
    (ROOT / "scratchpad" / "P2_조건종류.json").write_text(
        json.dumps({"요약": out, "핵": 표}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
