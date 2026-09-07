# -*- coding: utf-8 -*-
"""corpus.rules 의 문장(금지예시·허용예시·사전승인_조건)이 근거 조문 본문에 있는지 문자 4-gram
커버리지로 대조한다. 출력은 사람이 볼 우선순위 목록이지 합격/불합격 판정이 아니다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/verify_rules.py            # 요약
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/verify_rules.py --show 0.5 # 임계 미만 전부 출력
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/verify_rules.py --json scripts/_work/_rules_검증.json
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
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
N = 4                      # n-gram 길이. 3 은 우연 일치가 많고 5 는 조사 변형에 약하다
THRESHOLD = 0.60           # 이 밑이면 사람이 본다


def 정규화(s: str) -> str:
    """공백·구두점·쪽번호를 없앤다."""
    s = re.sub(r"-\s*\d+\s*-", "", s or "")          # 쪽번호 "- 15 -"
    s = re.sub(r"[\s ]+", "", s)
    s = re.sub(r"[·․‧、,\.\(\)（）\[\]「」『』\"'“”‘’:;/*※☞•\-–—→]", "", s)
    return s


def grams(s: str, n: int = N) -> set[str]:
    s = 정규화(s)
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def 커버리지(문장: str, 풀: set[str]) -> float:
    g = grams(문장)
    return len(g & 풀) / len(g) if g else 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=float, default=THRESHOLD, help="이 값 미만인 문장을 출력")
    ap.add_argument("--json", help="전체 결과를 JSON 으로 저장")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        본문 = {(d, j): t for d, j, t in conn.execute(
            "SELECT doc_id, 조번호, 본문 FROM corpus.doc_articles").fetchall()}
        rows = conn.execute("""
            SELECT rule_id, 사업명, 비목, 허용, 사전승인_조건, 금지예시, 허용예시, 근거, verified
              FROM corpus.rules ORDER BY 사업명, 비목
        """).fetchall()

    결과, 낮은것 = [], []
    for rid, 사업, 비목, 허용, 조건, 금지, 허용예, 근거, verified in rows:
        키 = [(g["doc_id"], g["조번호"]) for g in 근거]
        없는키 = [k for k in 키 if k not in 본문]
        풀 = set()
        for k in 키:
            풀 |= grams(본문.get(k, ""))

        문장들 = ([("사전승인_조건", 조건)] if 조건 else [])
        문장들 += [("금지예시", x) for x in (금지 or [])]
        문장들 += [("허용예시", x) for x in (허용예 or [])]

        점수 = []
        for 축, s in 문장들:
            c = 커버리지(s, 풀)
            점수.append(c)
            if c < a.show:
                낮은것.append((c, 사업, 비목, 축, s))
        결과.append(dict(rule_id=rid, 사업명=사업, 비목=비목, 허용=허용, verified=verified,
                        근거키=[f"{d}|{j}" for d, j in 키], 없는근거=[f"{d}|{j}" for d, j in 없는키],
                        문장수=len(문장들),
                        평균커버리지=round(sum(점수) / len(점수), 3) if 점수 else None,
                        최저커버리지=round(min(점수), 3) if 점수 else None))

    총문장 = sum(r["문장수"] for r in 결과)
    유효 = [r for r in 결과 if r["평균커버리지"] is not None]
    print(f"룰 {len(결과)}행 · 검사 문장 {총문장}개 · n-gram {N} · 임계 {a.show}")
    print(f"행 평균 커버리지 {sum(r['평균커버리지'] for r in 유효)/len(유효):.3f}")
    print(f"임계 미만 문장 {len(낮은것)}개 ({len(낮은것)/총문장*100:.0f}%)\n")

    dang = [r for r in 결과 if r["없는근거"]]
    print(f"근거 dangling 행: {len(dang)}" + ("  🔴" if dang else ""))
    for r in dang:
        print("   🔴", r["사업명"], r["비목"], r["없는근거"])

    print("\n== 사업별 평균 커버리지")
    by = {}
    for r in 유효:
        by.setdefault(r["사업명"], []).append(r["평균커버리지"])
    # 사업명 NULL 은 L1 공통행이다 — None 을 그대로 포맷하면 TypeError 로 죽는다
    for k, v in sorted(by.items(), key=lambda x: sum(x[1]) / len(x[1])):
        print(f"   {(k or 'L1공통(사업명 NULL)'):<22} {sum(v)/len(v):.3f}  ({len(v)}행)")

    if 낮은것:
        print(f"\n== 임계 미만 문장 — 사람이 원문과 대조할 순서 (낮은 것부터)")
        for c, 사업, 비목, 축, s in sorted(낮은것, key=lambda x: (x[0], x[1] or "", x[2] or "")):
            print(f"   {c:.2f}  {(사업 or 'L1공통')[:10]:<10} {(비목 or '-')[:12]:<12} "
                  f"{축:<12} {s[:72]}")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(dict(임계=a.show, n=N, 룰행=len(결과), 검사문장=총문장,
                           임계미만=len(낮은것), 행별=결과,
                           낮은문장=[dict(커버리지=round(c, 3), 사업명=b, 비목=i, 축=x, 문장=s)
                                   for c, b, i, x, s in sorted(낮은것)]),
                      f, ensure_ascii=False, indent=2)
        print(f"\n산출: {a.json}")


if __name__ == "__main__":
    main()
