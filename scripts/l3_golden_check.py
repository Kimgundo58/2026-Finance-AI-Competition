# -*- coding: utf-8 -*-
"""L3 골든셋 검산 — L2 산출(`scratchpad/L3골든셋_L2.json`, 30~40건)이 뜨는 «동안» 같이 돈다.

검사 4가지 (전부 DB 읽기전용):
  ① article_id 실재 — `tenant.l3_articles` 에 그 id 가 있는가
  ② 근거원문 포함 — `정답근거[].원문` 이 «그 article 의 본문 안에» 있는가
     (긴 인용문이라 `tips_rule_check._norm_발췌`(조사떼기 없는 정규형)를 재사용한다 —
     짧은 품목명용 `rule_lookup._norm()` 을 그대로 쓰면 PDF/HWP 줄바꿈이 단어를 끊는
     자리에서 조사떼기가 오히려 정규형을 갈라놓는다는 게 T4 실측으로 확인됐다)
  ③ 🔴 org_id 격리 — article 의 org_id 가 파일이 선언한 기관(경상국립대 창업중심대학사업단)
     것인가. **타 기관 혼입은 여기서 하드 실패** — 남의 기관 규정이 이 골든셋을 통해
     판정에 섞이면 그 자체가 TENANT_LEAK 이다
  ④ 판정 4종 enum — `정답판정` ∈ {가능,조건부,불가,판단불가} (`eval.golden_set` 실측 확인)

🔴 먼저 «일부러 틀린 항목» 을 넣어(존재 안 하는 article_id·타 기관 article_id·
   원문에 없는 근거·잘못된 enum) 이 검사기가 잡는지 자체증명한다(`--self-test`).
   자체증명 재료는 **실제 DB 값**(대상 기관 article 1건 + 실재하는 타 기관 article 1건)을
   쓴다 — 지어낸 값이 아니라 진짜 대조군으로 검출력을 증명한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/l3_golden_check.py --self-test
    PYTHONIOENCODING=utf-8 python scripts/l3_golden_check.py "scratchpad/L3골든셋_L2.json"

## 입력 스키마 (실 파일 확인, 2026-09-06)

파일 최상단에 `org_id`(문항 전체가 같은 기관) · `문항`(리스트). 문항 하나:
    {"no": "L3-01", "정답판정": "조건부",
     "정답근거": [{"article_id": 369, "doc": "...", "원문": "본문에서 그대로 뗀 인용문"}], ...}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rule_lookup                                                    # noqa: E402
import tips_rule_check as _t                                          # noqa: E402  (_norm_발췌 재사용)
from _lib import db                                                   # noqa: E402

_norm_발췌 = _t._norm_발췌

# 데모 기관 확정 — docs/9_미결 #1·HANDOFF. DB 실측(2026-09-06): 207개 article 보유.
대상_ORG_ID = "cfeba091-251a-5ae4-8cc9-88c6e6679440"          # 경상국립대학교 창업중심대학사업단

정답판정_ENUM = {"가능", "조건부", "불가", "판단불가"}         # eval.golden_set 실측(2026-09-06)


# ══════════════════════════════════════════════════════════════════════════════
# article 조회 (캐시)
# ══════════════════════════════════════════════════════════════════════════════

def _article(cur, article_id, _캐시: dict = {}) -> dict | None:
    if article_id in _캐시:
        return _캐시[article_id]
    cur.execute("SELECT article_id, org_id, 조번호, 본문 FROM tenant.l3_articles "
                "WHERE article_id = %s", [article_id])
    r = cur.fetchone()
    out = None if not r else {"article_id": r[0], "org_id": str(r[1]), "조번호": r[2], "본문": r[3]}
    _캐시[article_id] = out
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 근거 한 건 검산 — ①②③
# ══════════════════════════════════════════════════════════════════════════════

def 근거검산(cur, g: dict, *, 대상_org: str = 대상_ORG_ID) -> dict:
    art = _article(cur, g.get("article_id"))
    if art is None:
        return {"article_id": g.get("article_id"), "①article_id실재": False,
                "결과": "FAIL — article_id 가 tenant.l3_articles 에 없다"}

    org_ok = art["org_id"] == 대상_org
    if not org_ok:
        return {"article_id": g.get("article_id"), "①article_id실재": True,
                "②org_id격리": {"결과": False, "article의_org_id": art["org_id"],
                              "대상_org_id": 대상_org},
                "결과": "FAIL — 타 기관 혼입"}

    원문 = g.get("원문")
    if not 원문:
        포함 = None
    else:
        포함 = _norm_발췌(원문) in _norm_발췌(art["본문"])

    out = {"article_id": g.get("article_id"), "조번호": art["조번호"],
           "①article_id실재": True, "②org_id격리": {"결과": True},
           "③근거원문포함": {"결과": 포함} if 포함 is not None
                          else {"결과": None, "이유": "원문 필드가 없다"}}
    out["결과"] = "OK" if 포함 is not False else "FAIL — 근거원문이 article 본문에 없다"
    return out


def 문항검산(cur, q: dict, *, 대상_org: str = 대상_ORG_ID) -> dict:
    근거들 = q.get("정답근거") or []
    근거결과 = [근거검산(cur, g, 대상_org=대상_org) for g in 근거들]
    정답판정 = q.get("정답판정")
    enum결과 = {"결과": 정답판정 in 정답판정_ENUM, "값": 정답판정}

    문제 = [r for r in 근거결과 if r["결과"] != "OK"]
    if not 근거들:
        문제상태 = "FAIL — 정답근거가 없다"
    elif 문제:
        문제상태 = f"FAIL — 근거 {len(문제)}/{len(근거결과)}건 문제"
    elif not enum결과["결과"]:
        문제상태 = f"FAIL — 정답판정 {정답판정!r} 이 enum 밖"
    else:
        문제상태 = "OK"

    return {"no": q.get("no"), "④판정enum": enum결과, "근거별": 근거결과, "결과": 문제상태}


def 검산(cur, 파일들: list[dict]) -> dict:
    문항별 = []
    for 파일 in 파일들:
        org = 파일.get("org_id") or 대상_ORG_ID
        for q in (파일.get("문항") or []):
            r = 문항검산(cur, q, 대상_org=org)
            r["파일_org_id"] = org
            r["파일_org_격리"] = (org == 대상_ORG_ID)
            문항별.append(r)
    return {"총문항수": len(문항별), "실패문항수": sum(1 for r in 문항별 if r["결과"] != "OK"),
            "문항별": 문항별}


# ══════════════════════════════════════════════════════════════════════════════
# 자체증명 — 실제 DB 값으로 만든 대조군
# ══════════════════════════════════════════════════════════════════════════════

def _self_test(cur) -> bool:
    기준 = _article(cur, 309)
    assert 기준 and 기준["org_id"] == 대상_ORG_ID, "픽스처 전제(article 309)가 깨졌다"
    실재발췌 = 기준["본문"][10:30]

    양호 = {"no": "S1", "정답판정": "조건부",
           "정답근거": [{"article_id": 309, "원문": 실재발췌}]}
    불량_없는id = {"no": "S2", "정답판정": "가능",
                "정답근거": [{"article_id": -999999, "원문": "아무거나"}]}
    불량_타기관 = {"no": "S3", "정답판정": "불가",
                "정답근거": [{"article_id": 31, "원문": "아무거나"}]}
    불량_원문없음 = {"no": "S4", "정답판정": "가능",
                  "정답근거": [{"article_id": 309, "원문": "이 문장은 본문 어디에도 없다 완전 창작"}]}
    불량_enum밖 = {"no": "S5", "정답판정": "재검토필요",
                "정답근거": [{"article_id": 309, "원문": 실재발췌}]}

    파일 = {"org_id": 대상_ORG_ID, "문항": [양호, 불량_없는id, 불량_타기관, 불량_원문없음, 불량_enum밖]}
    결과 = 검산(cur, [파일])
    문항별 = {r["no"]: r for r in 결과["문항별"]}

    실패: list[str] = []
    if 문항별["S1"]["결과"] != "OK":
        실패.append(f"양호를 통과시키지 못함: {문항별['S1']}")
    if 문항별["S2"]["결과"] == "OK":
        실패.append("①-존재 안 하는 article_id 를 못 잡음")
    if 문항별["S3"]["결과"] == "OK":
        실패.append("③-타 기관 article_id(31, 다른 org)를 못 잡음 — org_id 격리 실패")
    if 문항별["S4"]["결과"] == "OK":
        실패.append("②-원문에 없는 근거를 못 잡음")
    if 문항별["S5"]["결과"] == "OK":
        실패.append("④-enum 밖 정답판정을 못 잡음")

    print(f"[self-test] 검사 5건 중 실패 {len(실패)}건")
    for f in 실패:
        print("  ✗", f)
    if not 실패:
        print("  ✓ 전부 통과 — 존재하는 타 기관 article(31·org f03ce8d6…)로 "
              "격리 검출력까지 증명함")
    return not 실패


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    cur = db.connect().cursor()

    if args.self_test:
        sys.exit(0 if _self_test(cur) else 1)

    if not args.files:
        print("파일을 주거나 --self-test 를 써라"); sys.exit(2)

    파일들 = [json.loads(Path(fp).read_text(encoding="utf-8")) for fp in args.files]
    print(json.dumps(검산(cur, 파일들), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
