# -*- coding: utf-8 -*-
"""판정 채점기 — 치명 오답률 · 판정 일치율 · 인용 정확도 · 판단불가율 4지표를 낸다.

치명 오답 = 정답이 불가/조건부인데 '가능' 이라 한 것. 1건이라도 있으면 exit 1.
실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/score_judgment.py --in 결과.jsonl
"""
from __future__ import annotations

# `scripts/_lib` 이 보일 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# archive 하위 폴더끼리 import 하므로 scripts/archive/ 의 모든 하위 폴더도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


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
    """정답이 불가/조건부인데 '가능' 이라고 한 것."""
    return 정답 in ("불가", "조건부") and 예측 == "가능"


def 인용뽑기(행: dict) -> list[str]:
    """결과 한 줄에서 S번호 목록을 꺼낸다 — 검증기를 거친 `인용목록` 과 원본 `인용` 둘 다 본다."""
    목록 = 행.get("인용목록")
    if 목록:
        return [c.get("s번호") for c in 목록 if isinstance(c, dict) and c.get("s번호")]
    return [x for x in (행.get("인용") or []) if isinstance(x, str)]


def 인용정확(예측인용: list, 정답청크: set, s맵: dict) -> float | None:
    """인용한 S번호 중 정답 근거를 가리키는 비율. 인용이 없으면 None."""
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
    # 판단불가는 모델이 스스로 고른 것과 실패 경로(타임아웃·스키마 위반·잘림)를 나눠 센다
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
            # 실패단계가 있거나 경로에 실패·예외·dry 가 박혔으면 모델의 선택이 아니다
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
