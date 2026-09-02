# -*- coding: utf-8 -*-
"""A2 엄격조항 검토 — L3 업로드 시 돌리는 비동기 배치 (`Agent.md` §9 우선순위 2).

무엇을 찾는가
  주관기관 규정(L3)에 **국가 규정(L1·L2)보다 엄격한 조항**이 있으면 사용자에게 알려야 한다.
  기관 규정은 사용자가 올린 것이라 우리가 검수하지 않았고, 판정에 바로 먹이면 위험하다.
  그래서 **후보만 뽑아 검수 큐에 넣는다.**

🔴 **엄격한 게 이기는 게 아니다.** CLAUDE.md 가 못박았다 —
   「충돌 해소는 "아래가 엄격하면 이긴다" 가 아니다 — **반대가 규칙**이다.」
   8개 사업 중 6개가 적용범위 조에서 **L2 > L3** 를 명시한다. L3 가 더 엄격해도 진다.
   그래서 이 파일은 엄격도만 보지 않는다. `corpus.precedence_rules` 를 같이 조회해
   **"더 엄격하지만 우선순위상 진다"** 를 함께 적는다. 그게 사람이 봐야 할 값이다.
   엄격도만 뽑아 큐에 넣으면 검수자가 "그러니 이걸 적용하자" 로 읽는다 — 그게 오답의 씨앗이다.

무엇을 엄격하다고 보는가 (넷)
  ① 국가가 `가능`·`조건부` 인데 L3 가 **금지 표현**을 쓴다
  ② 같은 단위의 **금액·개수 한도가 더 작다**
  ③ 국가에 없는 **사전승인**을 L3 가 요구한다
  ④ 국가에 없는 **기한**(며칠 이내)을 L3 가 건다

무엇을 안 보는가
  🔴 사업비와 무관한 조(복무시간·연차·출퇴근·실험실 안전)는 후보에서 뺀다.
     기관 규정에는 그런 조가 더 많고, 다 넣으면 큐가 노이즈로 덮인다.
  🔴 "~에 따른다" 만 있는 참조 조는 제약이 아니다 (게이팅 (2) 갈래).

실행:
    PYTHONIOENCODING=utf-8 python scripts/agent_a2.py --dry
    PYTHONIOENCODING=utf-8 python scripts/agent_a2.py --org <org_id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _lib import db  # noqa: E402
from agent_a4 import 적재, 접기                                   # noqa: E402

DSN = db.DSN

비목_ENUM = ("재료비", "외주용역비", "기계장치", "인건비", "지급수수료",
             "여비", "교육훈련비", "광고선전비", "특허권등무형자산취득비", "창업활동비")

# 사업비와 무관한 조를 거르는 말. 기관 규정의 대부분이 이쪽이다
_무관 = re.compile(r"복무|근무시간|연차|휴가|출퇴근|안전점검|실험실|보안|윤리|성희롱|"
                   r"인사|징계|채용|비밀유지")

_금지 = re.compile(r"할\s*수\s*없다|하지\s*못한다|불가|금지|제외한다|인정하지\s*않는다|"
                   r"집행할\s*수\s*없|구매할\s*수\s*없|사용할\s*수\s*없")
_사전승인 = re.compile(r"사전\s*승인|사전\s*심의|승인을\s*받아야|사전에\s*협의")
_참조만 = re.compile(r"에\s*따른다\.?\s*$|을\s*준용한다\.?\s*$")
_기한 = re.compile(r"(\d+)\s*(일|개월|월)\s*(?:이내|안에)")

_금액 = re.compile(r"([0-9][0-9,]*)\s*(억|천만|백만|만)?\s*원")
_개수 = re.compile(r"([0-9]+)\s*(대|명|인|건|회)\s*(?:이내|이하|까지|를\s*초과할\s*수\s*없)")

_배수 = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, None: 1}


def 금액들(t: str) -> list[int]:
    out = []
    for 수, 단위 in _금액.findall(t):
        try:
            out.append(int(수.replace(",", "")) * _배수.get(단위 or None, 1))
        except ValueError:
            pass
    return out


def 비목추정(조제목: str, 본문: str, 별칭: dict[str, str]) -> str | None:
    """조제목이 주다 — 기관 규정도 비목명을 조제목으로 쓴다 (픽스처 실측)."""
    for 텍스트, 가중 in ((조제목 or "", 2), (본문 or "", 1)):
        for 표기, 비목 in 별칭.items():
            if 표기 and 표기 in 텍스트:
                return 비목
        if 가중 == 2 and 조제목 and 조제목 in 비목_ENUM:
            return 조제목
    return None


def 별칭표(cur) -> dict[str, str]:
    표: dict[str, str] = {b: b for b in 비목_ENUM}
    try:
        for 비목, 별칭 in cur.execute(
                'SELECT "비목", "별칭" FROM corpus.item_vocab').fetchall():
            for a in (별칭 or []):
                표[a] = 비목
    except Exception:                                             # noqa: BLE001
        pass
    # 긴 표기가 먼저 걸리게 — '재료비' 가 '원재료비' 를 먹지 않도록
    return dict(sorted(표.items(), key=lambda kv: -len(kv[0])))


def 우선순위(cur, 사업명: str) -> dict:
    """이 사업에서 L3 가 이기는가. 🔴 대부분 진다."""
    행 = cur.execute("""
        SELECT "우선계층", "열위계층", "범위", "우선규범" FROM corpus.precedence_rules
        WHERE "사업명" = %s
    """, (사업명,)).fetchall()
    for 우선, 열위, 범위, 규범 in 행:
        if 열위 == "L3":
            이김 = 범위 != "all"          # 'all' 이면 L3 는 무조건 진다
            return {"L3승리": 이김, "우선계층": 우선, "범위": 범위, "우선규범": 규범,
                    "결론": ("L3 가 더 엄격해도 진다 — 적용범위 조가 "
                            f"{우선} > L3 를 범위 'all' 로 명시한다" if not 이김 else
                            f"{우선} > L3 이나 범위가 '{범위}' 라 미규정 영역에서는 L3 가 산다")}
    # 조항이 없는 사업(TIPS·초격차 등) — 없다고 L3 가 이기는 것도 아니다
    return {"L3승리": None, "우선계층": None, "범위": None, "우선규범": None,
            "결론": "이 사업에는 L2>L3 우선순위 조항이 등록돼 있지 않다 — 사람이 판단해야 한다"}


def 국가룰(cur, 사업명: str, 비목: str) -> dict | None:
    행 = cur.execute("""
        SELECT rule_id, "허용", "사전승인", "한도_유형", "한도_값", "한도_단위",
               "금지예시", "verified"
        FROM corpus.rules
        WHERE "사업명" = %s AND "비목" = %s AND "layer" IN ('L1','L2')
        ORDER BY ("layer"='L2') DESC, rule_id LIMIT 1
    """, (사업명, 비목)).fetchone()
    if not 행:
        return None
    k = ("rule_id", "허용", "사전승인", "한도_유형", "한도_값", "한도_단위", "금지예시", "verified")
    return dict(zip(k, 행))


def 엄격판정(본문: str, 룰: dict | None) -> list[dict]:
    """이 L3 조가 국가 규정보다 엄격한 지점들."""
    나온것: list[dict] = []
    if _참조만.search(본문.strip()):
        return 나온것                        # "~에 따른다" 는 제약이 아니다

    if _금지.search(본문):
        국가 = (룰 or {}).get("허용")
        if 국가 in (None, "가능", "조건부"):
            나온것.append({"유형": "금지추가", "L3": "금지 표현 있음",
                          "국가": 국가 or "룰 없음"})

    if _사전승인.search(본문) and not (룰 or {}).get("사전승인"):
        나온것.append({"유형": "사전승인추가", "L3": "사전승인 요구",
                      "국가": "사전승인 없음" if 룰 else "룰 없음"})

    한도값 = (룰 or {}).get("한도_값")
    if 한도값 is not None and (룰 or {}).get("한도_유형") in ("금액", "개수"):
        후보 = 금액들(본문) if 룰["한도_유형"] == "금액" else \
               [int(x) for x, _ in _개수.findall(본문)]
        더작은 = [v for v in 후보 if v < float(한도값)]
        if 더작은:
            나온것.append({"유형": "한도축소", "L3": max(더작은), "국가": float(한도값),
                          "단위": 룰.get("한도_단위"),
                          # ⚠️ 단위 문자열이 자유 텍스트라 자동 비교는 여기까지다.
                          #    같은 축인지 확인은 사람이 한다 — 그래서 큐로 간다
                          "주의": "단위 문자열이 자유 텍스트다. 같은 축인지 사람이 확인할 것"})

    m = _기한.search(본문)
    if m and 룰 is not None:
        나온것.append({"유형": "기한추가", "L3": f"{m.group(1)}{m.group(2)} 이내",
                      "국가": "해당 룰에 기한 없음"})
    return 나온것


def 실행(conn, org: str | None, 줄) -> list[dict]:
    cur = conn.cursor()
    별칭 = 별칭표(cur)
    orgs = cur.execute(
        'SELECT org_id, "기관명", "사업명" FROM tenant.orgs'
        + (' WHERE org_id = %s' if org else ''), (org,) if org else ()).fetchall()

    recs: list[dict] = []
    for org_id, 기관명, 사업들 in orgs:
        줄(f"\n■ {기관명}  사업 {사업들}")
        조들 = cur.execute("""
            SELECT "조번호", "조제목", "본문" FROM tenant.l3_articles
            WHERE org_id = %s ORDER BY article_id
        """, (org_id,)).fetchall()
        걸림 = 무관 = 비목없음 = 0
        for 조번호, 조제목, 본문 in 조들:
            if _무관.search((조제목 or "") + " " + (본문 or "")):
                무관 += 1
                continue
            비목 = 비목추정(조제목, 본문, 별칭)
            if not 비목:
                비목없음 += 1
                continue
            for 사업명 in (사업들 or [None]):
                룰 = 국가룰(cur, 사업명, 비목) if 사업명 else None
                지점 = 엄격판정(본문 or "", 룰)
                if not 지점:
                    continue
                걸림 += 1
                prec = 우선순위(cur, 사업명) if 사업명 else {"L3승리": None, "결론": "사업 미지정"}
                요약 = " · ".join(f"{d['유형']}(L3 {d['L3']} vs 국가 {d['국가']})" for d in 지점)
                recs.append({
                    "종류": "A2엄격조항", "사유코드": "STRICTER_L3",
                    "대상종류": "rule" if 룰 else "none",
                    "대상ID": str(룰["rule_id"]) if 룰 else None,
                    "사업명": 사업명, "비목": 비목,
                    "doc_id": str(org_id), "조번호": 조번호,
                    "구doc_id": None, "구조번호": None,
                    "변경유형": None, "유사도": None,
                    "요약": f"[{기관명}] {조번호}({조제목}) — {요약}. 🔴 {prec['결론']}",
                    "상세": {"기관명": 기관명, "조제목": 조제목,
                            "엄격지점": 지점, "우선순위": prec,
                            "국가룰": {k: str(v) for k, v in (룰 or {}).items()},
                            "L3본문": (본문 or "")[:500]},
                })
        줄(f"    조 {len(조들)}개  ·  사업비 무관 제외 {무관}  ·  비목 미확정 {비목없음}"
           f"  ·  엄격 후보 {걸림}")
    return recs


def main() -> None:
    ap = argparse.ArgumentParser(description="A2 엄격조항 검토 — L3 vs 국가 규정")
    ap.add_argument("--org", help="특정 org_id 만")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    줄들: list[str] = []

    def 줄(s=""):
        print(s, flush=True)
        줄들.append(s)

    줄("A2 엄격조항 검토 — 🔴 엄격한 게 이기는 게 아니다. 우선순위를 같이 판정한다")
    with db.connect() as conn:
        recs = 접기(실행(conn, a.org, 줄))
        줄("\n" + "=" * 74)
        줄(f"후보 {len(recs)}건")
        이김 = sum(1 for r in recs if r["상세"]["우선순위"].get("L3승리") is True)
        짐 = sum(1 for r in recs if r["상세"]["우선순위"].get("L3승리") is False)
        줄(f"  그중 L3 가 이기는 것 {이김} · 우선순위상 지는 것 {짐} · 판단 불가 {len(recs)-이김-짐}")
        for r in recs:
            줄(f"  · [{r['사업명']}] {r['요약'][:140]}")
        n, msg = 적재(conn, recs, a.dry)
        줄(f"\n적재: {msg}")
    Path(ROOT / "scripts/_work/_A2_엄격조항.md").write_text(
        "# A2 엄격조항 검토\n\n```\n" + "\n".join(줄들) + "\n```\n", encoding="utf-8")


if __name__ == "__main__":
    main()
