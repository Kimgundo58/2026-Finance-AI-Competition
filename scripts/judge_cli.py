# -*- coding: utf-8 -*-
"""`judge_cli` — 한 줄로 (1)~(7) 을 돌리는 사람용 입구.

오늘의 목표가 이 파일이다: **judge_cli 한 줄로 끝까지 돌고, 지표가 남는다.**
`orchestrate.판정()` 은 dict 를 돌려주는 라이브러리이고, 여기는 그걸 사람이 읽는
화면으로 바꾼다. UI 보다 CLI 판정기가 먼저다 (`CLAUDE.md` — 판정 품질이 정답셋을
못 넘으면 UI 는 의미가 없다).

    PYTHONIOENCODING=utf-8 python scripts/judge_cli.py "맥북 250만원 사도 되나요"
    PYTHONIOENCODING=utf-8 python scripts/judge_cli.py "..." --사업명 예비창업패키지
    PYTHONIOENCODING=utf-8 python scripts/judge_cli.py "..." --org 1d6b...-c0838b431a7f
    PYTHONIOENCODING=utf-8 python scripts/judge_cli.py "..." --dry     # LLM 없이 배관만
    PYTHONIOENCODING=utf-8 python scripts/judge_cli.py "..." --json    # 기계용

## 화면에 무엇을 내보이는가
판정·요약·인용(원문 치환된 것)·해야할일·전제·신뢰등급·버전스탬프까지다.
**강등사유와 경로도 같이 낸다** — 왜 조건부로 내려갔는지가 안 보이면 사람이 결과를
못 믿고, 못 믿으면 이 서비스는 쓰이지 않는다. 판단불가일 때는 유사사례와 문의 유도를 낸다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrate import 강등코드_전체, 모듈상태, 판정  # noqa: E402

_색 = {"가능": "\033[32m", "조건부": "\033[33m", "불가": "\033[31m",
       "판단불가": "\033[36m", "선택필요": "\033[35m"}
_끝 = "\033[0m"


def _헤드(r: dict) -> str:
    p = r.get("판정") or "?"
    return f"{_색.get(p, '')}■ {p}{_끝}  {r.get('요약') or ''}"


def 출력(r: dict, *, 상세: bool = True) -> None:
    print()
    print(_헤드(r))

    if r.get("갈래"):
        # 게이트 C — "비목을 고르세요" 로 문제를 되돌려주지 않는다.
        # 각 선택의 판정 결과를 나란히 보여주고 고르게 한다 (`Agent.md` §3 C).
        print("\n  비목이 갈립니다. 각각의 판정은 이렇습니다 —")
        for g in r["갈래"]:
            print(f"    · {g.get('비목')}: {g.get('판정')} — {g.get('요약')}")
        print("\n  고르신 비목은 다음 판정부터 자동으로 적용됩니다.")

    등급 = r.get("신뢰등급")
    if 등급 or r.get("버전스탬프"):
        print(f"\n  신뢰등급 {등급 or '—'}"
              + (f" · 기준 {r['버전스탬프']}" if r.get("버전스탬프") else ""))

    인용 = r.get("인용목록") or []
    if 인용:
        print(f"\n  근거 {len(인용)}건")
        for c in 인용:
            머리 = " ".join(x for x in (c.get("doc_id"), c.get("조번호"),
                                      f"({c.get('조제목')})" if c.get("조제목") else "",
                                      c.get("항호") or "") if x)
            print(f"    [{c.get('s번호')}] {머리}")
            본 = (c.get("원문") or "").strip().replace("\n", " ")
            print(f"          {본[:180]}{'…' if len(본) > 180 else ''}")
            if c.get("extraction") == "vlm":
                print("          ⚠️ 스캔 판독본 기반입니다 — 원문을 함께 확인하세요")

    할일 = r.get("해야할일") or []
    if 할일:
        print(f"\n  해야 할 일 {len(할일)}건")
        for h in 할일:
            print(f"    □ {h.get('항목')}"
                  + (f"  [{h['code']}]" if h.get("code") else ""))
            if h.get("설명"):
                print(f"        {h['설명']}")

    해소 = r.get("전제해소") or {}
    if 해소.get("인라인요청"):
        print("\n  아래를 알려주시면 바로 다시 계산합니다 (모델 재호출 없음)")
        for p in 해소["인라인요청"]:
            print(f"    ? {p.get('사실')}  ← {', '.join(p.get('필요입력') or [])}")
    if 해소.get("즉시검증"):
        print(f"\n  확인된 전제 {len(해소['즉시검증'])}건")
        for p in 해소["즉시검증"]:
            print(f"    ✓ {p.get('사실')}")
    if 해소.get("미매핑"):
        print(f"\n  ⚠️ 확인할 수 없는 전제 {len(해소['미매핑'])}건 — 담당자 확인이 필요합니다")
        for p in 해소["미매핑"]:
            print(f"    ? {p.get('사실')}")

    사례 = r.get("유사사례") or []
    if 사례:
        print(f"\n  참고 사례 {len(사례)}건 — 🔴 귀하의 사업에 적용되지 않습니다")
        for s in 사례:
            print(f"    · {(s.get('질문') or s.get('text') or '')[:110]}")

    사슬 = r.get("참조사슬") or []
    if 상세 and 사슬:
        print(f"\n  이게 왜 적용되나 (참조 {len(사슬)}건)")
        for e in 사슬[:6]:
            f_, t_ = e.get("from") or {}, e.get("to") or {}
            보정 = f"  ⟳{e['보정']}" if e.get("보정") else ""
            print(f"    {f_.get('조번호')} —[{e.get('표기')}]→ "
                  f"{t_.get('doc_id')} {t_.get('조번호')}{보정}")
        if len(사슬) > 6:
            print(f"    … 외 {len(사슬)-6}건")

    if 상세:
        코드 = r.get("강등코드") or []
        사유 = r.get("강등사유") or []
        if 사유:
            print(f"\n  검증 로그 {len(사유)}건" + (f"  {코드}" if 코드 else ""))
            for s in 사유:
                print(f"    · {s}")
        지연 = r.get("지연ms") or {}
        print(f"\n  게이트 {r.get('게이트') or '—'} · 경로 {r.get('경로') or '—'} · "
              f"{지연.get('총', 0):,}ms"
              + (f" · decision_id={r['decision_id']}" if r.get("decision_id") else ""))
        if 지연:
            print("    " + "  ".join(f"{k} {v:,}ms" for k, v in 지연.items() if k != "총"))
        if r.get("실패단계"):
            print(f"  🔴 실패단계 {r['실패단계']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="써도돼요 판정 CLI")
    ap.add_argument("질문", nargs="?", help="예: 맥북 250만원 디자이너 작업용")
    ap.add_argument("--사업명")
    ap.add_argument("--org", dest="org_id", help="주관기관 org_id (L3 경로)")
    ap.add_argument("--기관", dest="기관ID", help="기관ID — 인용 누수 검사 기준")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 배관만")
    ap.add_argument("--json", action="store_true", help="기계용 원본 dict")
    ap.add_argument("--no-log", action="store_true", help="decisions 기록 생략")
    ap.add_argument("--quiet", action="store_true", help="검증 로그·지연 숨김")
    ap.add_argument("--codes", action="store_true", help="강등코드 18종을 찍고 끝낸다")
    a = ap.parse_args()

    if a.codes:
        for c in 강등코드_전체:
            print(c)
        return
    if not a.질문:
        ap.error("질문을 한 줄 주거나 --codes 를 쓴다")

    # 스텁으로 도는 축이 있으면 화면에 말한다. 조용히 스텁 결과를 내놓지 않는다.
    스텁 = [k for k, v in 모듈상태.items() if not v]
    if 스텁:
        print(f"⚠️ 스텁으로 동작 중인 축: {', '.join(스텁)} — 결과를 지표로 쓰지 마라",
              file=sys.stderr)

    r = 판정(a.질문, 사업명=a.사업명, org_id=a.org_id, 기관ID=a.기관ID,
            dry=a.dry, 기록=not a.no_log)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        출력(r, 상세=not a.quiet)
    # 판단불가는 정상 종료다 — 제품 기능이지 실패가 아니다 (`Agent.md` §7).
    # 배관이 깨진 것(실패단계)만 비0 으로 낸다.
    sys.exit(2 if r.get("실패단계") else 0)


if __name__ == "__main__":
    main()
