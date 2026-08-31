# -*- coding: utf-8 -*-
"""판정 채점기 — `LLM.md` §5 의 4지표.

## 지표와 우선순위

    치명 오답률   정답이 불가/조건부인데 "가능"        <- 🔴 1순위
    판정 일치율   4-way 완전 일치
    인용 정확도   인용 조항이 실제 근거인가
    판단불가율    최소화가 아니라 **균형** 대상

🔴 **선택 기준: 치명 오답 0 제약 하에서 판단불가율 최소.**
   `LLM.md` §5 — "기준은 결과를 보기 전에 정한다." 숫자를 보고 기준을 맞추면 측정이 아니다.

## 왜 치명 오답이 1순위인가

이 서비스에서 유일하게 돌이킬 수 없는 오답은 **"안 되는 걸 된다고 하는 것"** 이다.
사용자가 그 말을 믿고 집행하면 환수당한다. 반대로 "판단불가" 는 틀려도 안전하다 —
사용자가 담당자에게 물어보면 된다. 이 비대칭이 지표 순서를 정한다.

`판단불가율` 은 그 균형추다. 전부 판단불가로 답하면 치명 오답은 0 이지만 제품이 아니다.
§5 가 "0%면 원칙 위반, 50%면 제품 가치 상실" 이라고 적은 이유다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/score_judgment.py --in 결과.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

판정들 = ("가능", "조건부", "불가", "판단불가")


def 치명(정답: str, 예측: str) -> bool:
    """정답이 불가/조건부인데 '가능' 이라고 한 것. 이것만이 치명이다."""
    return 정답 in ("불가", "조건부") and 예측 == "가능"


def 인용뽑기(행: dict) -> list[str]:
    """결과 한 줄에서 S번호 목록을 꺼낸다.

    🔴 검증기가 [1겹] `인용`(S번호 문자열 배열)을 [2겹] `인용목록`
       (doc_id·조번호·원문까지 채운 객체 배열)로 바꾼다. 키 이름이 다르다 —
       `인용` 만 보면 항상 비어 있고 "인용 0건" 으로 잘못 집계된다 (2026-08-31 실제로 겪음).
       둘 다 본다.
    """
    목록 = 행.get("인용목록")
    if 목록:
        return [c.get("s번호") for c in 목록 if isinstance(c, dict) and c.get("s번호")]
    return [x for x in (행.get("인용") or []) if isinstance(x, str)]


def 인용정확(예측인용: list, 정답청크: set, s맵: dict) -> float | None:
    """인용한 S번호가 정답 근거를 가리키는가.

    S번호 -> (종류, id, 항호) 로 풀어 정답 집합과 대조한다.
    인용이 없으면 None (판단불가일 때가 대부분이라 0 으로 세면 왜곡된다).
    """
    if not 예측인용:
        return None
    맞음 = 0
    for s in 예측인용:
        v = s맵.get(s)
        if v and (v[0], v[1]) in 정답청크:
            맞음 += 1
    return 맞음 / len(예측인용)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="한 줄에 {gold_id, 판정, 인용, s맵, ...} 인 jsonl")
    a = ap.parse_args()

    rows = [json.loads(l) for l in Path(a.inp).open(encoding="utf-8") if l.strip()]
    if not rows:
        sys.exit("입력이 비었다")

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("SELECT gold_id, 세트, 정답판정, 정답근거 FROM eval.golden_set")
        정답표 = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

        # 정답근거 -> (종류, id) 집합. article_id 로 맞춘다 (조립기가 article 로 넣는다)
        정답청크: dict[int, set] = {}
        for gid, (_, _, 근거) in 정답표.items():
            s = set()
            for g in (근거 or []):
                import re
                m = re.match(r"(제\d+조(?:의\d+)?)", g.get("조번호") or "")
                cur.execute("""SELECT article_id FROM corpus.doc_articles
                                WHERE doc_id=%s AND 조번호=%s""",
                            (g.get("doc"), m.group(1) if m else g.get("조번호")))
                for (aid,) in cur.fetchall():
                    s.add(("article", aid))
                    cur.execute("SELECT chunk_id FROM corpus.chunks WHERE article_id=%s", (aid,))
                    for (cid,) in cur.fetchall():
                        s.add(("chunk", cid))
            정답청크[gid] = s

    n = len(rows)
    치명수 = 일치 = 판단불가 = 0
    # 🔴 판단불가를 한 숫자로 세면 `LLM.md` §5 · 계약 §7 이 오독된다 (2026-09-01 D 발견).
    #    "판단불가율 0% 면 근거 없이 답을 만들고 있다는 뜻" 인데, 그 경고가 풀렸는지는
    #    **모델이 스스로 판단불가를 골랐는가**로만 판정된다.
    #    §8 실패 경로(타임아웃·스키마 위반·출력 잘림)가 사고를 안전하게 닫은 건수는
    #    "근거가 없으면 답하지 않는다" 의 증거가 아니다 — 오히려 그 반대다.
    #    실측: 실전 E2E 77문항의 판단불가 5건이 **전부** max_tokens 잘림이었고
    #    모델이 스스로 고른 건 0건이었다. 한 숫자로 6.5% 만 보면 "경고 해소" 로 읽힌다.
    모델선택 = 실패경로 = 0
    인용점수: list[float] = []
    혼동 = collections.Counter()
    세트별 = collections.defaultdict(lambda: dict(n=0, 치명=0, 일치=0, 판단불가=0))
    치명목록 = []

    for r in rows:
        gid = r["gold_id"]
        if gid not in 정답표:
            continue
        세트, 정답, _ = 정답표[gid]
        예측 = r.get("판정")
        b = 세트별[세트]; b["n"] += 1
        혼동[(정답, 예측)] += 1
        if 예측 == 정답:
            일치 += 1; b["일치"] += 1
        if 예측 == "판단불가":
            판단불가 += 1; b["판단불가"] += 1
            # 실패단계가 있거나 경로에 실패·예외·dry 가 박혔으면 모델의 선택이 아니다.
            # 두 키가 아예 없는 산출물(구 격리 D6 jsonl)은 실패 경로가 없었으므로 모델선택.
            경로 = str(r.get("경로") or "")
            if r.get("실패단계") or any(k in 경로 for k in ("실패", "예외", "dry")):
                실패경로 += 1
            else:
                모델선택 += 1
        if 치명(정답, 예측):
            치명수 += 1; b["치명"] += 1
            치명목록.append((gid, 세트, 정답, 예측))
        p = 인용정확(인용뽑기(r), 정답청크.get(gid, set()),
                    {k: tuple(v) for k, v in (r.get("s맵") or {}).items()})
        if p is not None:
            인용점수.append(p)

    print(f"문항 {n}건\n")
    print(f"  🔴 치명 오답률   {치명수/n*100:5.1f}%  ({치명수}건)   <- 1순위. 0 이어야 한다")
    print(f"     판정 일치율   {일치/n*100:5.1f}%  ({일치}건)")
    print(f"     인용 정확도   " + (f"{sum(인용점수)/len(인용점수)*100:5.1f}%  (인용한 {len(인용점수)}건 기준)"
                                    if 인용점수 else "  — (인용 0건)"))
    print(f"     판단불가율    {판단불가/n*100:5.1f}%  ({판단불가}건 = "
          f"모델선택 {모델선택} + 실패경로 {실패경로})   <- 0%도 50%도 안 된다")
    # 🔴 §7 경고의 해소 여부는 **모델선택**만 본다. 총합이 아니다.
    if 모델선택 == 0:
        print(f"     🔴 모델이 스스로 판단불가를 고른 적이 0 이다 — "
              f"판단불가율이 {판단불가/n*100:.1f}% 여도 "
              f"`LLM.md` §5 경고(근거 없이 답을 만든다)는 **미해소**다")
        if 실패경로:
            print(f"        {실패경로}건은 §8 실패 경로가 사고를 안전하게 닫은 것이다 "
                  f"— 옳게 동작한 것이지 판정력의 증거가 아니다")

    if 치명목록:
        print(f"\n🔴 치명 오답 {len(치명목록)}건 — 전수:")
        for gid, 세트, 정답, 예측 in 치명목록:
            print(f"     gold_id={gid} [{세트}] 정답={정답} -> 예측={예측}")

    print("\n혼동행렬 (행=정답, 열=예측)")
    print(f"    {'':8}" + "".join(f"{p:>10}" for p in 판정들))
    for 정 in 판정들:
        줄 = "".join(f"{혼동.get((정, 예), 0):>10}" for 예 in 판정들)
        print(f"    {정:8}{줄}")

    print("\n세트별")
    for 세트, b in sorted(세트별.items()):
        if not b["n"]:
            continue
        print(f"    {세트:8} {b['n']:3}건  치명 {b['치명']/b['n']*100:5.1f}% · "
              f"일치 {b['일치']/b['n']*100:5.1f}% · 판단불가 {b['판단불가']/b['n']*100:5.1f}%")

    print("\n" + "=" * 66)
    if 치명수:
        print(f"🔴 치명 오답 {치명수}건 — 배포 불가. 원인부터 본다")
        sys.exit(1)
    print(f"✅ 치명 오답 0 · 판단불가율 {판단불가/n*100:.1f}%")


if __name__ == "__main__":
    main()
