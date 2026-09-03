# -*- coding: utf-8 -*-
"""P1 과제 A — S번호가 폭증할 때 스키마·검증 경로가 견디는가 (읽기 전용 · GPU 불필요).

P3 의 B3 행분해가 켜지면 S번호 개수가 크게 는다. 그 전에 **우리 쪽 상한**을 잰다.

🔴 **여기서 재는 것과 못 재는 것**
    잰다   — 스키마 문자수 · enum 몫 · JSON Schema 컴파일/검사 소요 · `검증()` 소요
             · S번호 문자열 자릿수가 `S번호_PATTERN` 을 언제 벗어나는가
    못 잰다 — vLLM `guided_json` 이 실제로 견디는가. 로컬에 xgrammar/outlines 가 없다.
             그건 GPU 박스에서만 닫힌다. **여기 수치로 그걸 추정해 확정하지 마라.**

DB 는 한 행도 쓰지 않는다. `--실맵` 을 줄 때만 run 191 의 s맵 크기를 읽는다.

    PYTHONIOENCODING=utf-8 python scratchpad/P1_S번호_내성.py
    PYTHONIOENCODING=utf-8 python scratchpad/P1_S번호_내성.py --실맵
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from llm_schema import S번호_PATTERN, 판정_스키마  # noqa: E402
from llm_validate import 검증  # noqa: E402

_RE_S = re.compile(S번호_PATTERN)

# 현행 실측 닻: run 191 의 `f_경로집합()` 은 21개다 (P1 1일차 실측).
F경로 = [f"신청기업.필드{i:02d}" for i in range(21)]

크기들 = (21, 50, 100, 200, 500, 1000, 2000, 3000)

부분집합 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "P4_부분집합_0903.json")


def _s(i: int) -> str:
    """`assemble_context.py:114` 와 **같은 식**이다. 다르면 이 측정이 무의미하다."""
    return f"S{i:02d}"


def 합성(n: int) -> tuple[list[str], dict, dict]:
    """S번호 n개짜리 s맵 + 메타 스텁. 메타를 주면 `검증()` 이 DB 를 안 본다."""
    번호 = [_s(i) for i in range(1, n + 1)]
    s맵 = {sid: ("chunk", 10000 + i, None) for i, sid in enumerate(번호)}
    메타 = {sid: dict(doc_id="L2_모두의창업_세부관리기준", 조번호="제33조",
                     조제목="사업비의 사용용도", 원문="① 사업비는 다음 각 호의 비목에 사용한다.",
                     원문범위="청크", version="제3차", extraction="native",
                     항호_DB="①", 기관id=None, domain="창업지원사업", layer="L2")
          for sid in 번호}
    return 번호, s맵, 메타


def 출력(번호: list[str]) -> dict:
    """스키마 상한을 꽉 채운 1겹 출력 — 인용 20(maxItems) · 전제 10(maxItems)."""
    return {
        "판정": "조건부",
        "요약": "해당 비목은 사용 가능하나 사전 승인이 필요하다.",
        "해야할일": [{"항목": f"항목{i}", "설명": "증빙을 갖춘다"} for i in range(10)],
        "인용": 번호[:20],
        "전제": [{"사실": "창업기업이 중소기업에 해당한다",
                 "근거조항": 번호[i % len(번호)],
                 "매핑": [F경로[0]],
                 "미충족시": "불가"} for i in range(10)],
    }


def 재기(fn, 회=5) -> float:
    """가장 빠른 회차의 ms. 평균은 GC·OS 잡음을 그대로 먹는다."""
    최소 = float("inf")
    for _ in range(회):
        t = time.perf_counter()
        fn()
        최소 = min(최소, (time.perf_counter() - t) * 1000)
    return 최소


def 표() -> None:
    try:
        import jsonschema
    except ImportError:
        jsonschema = None

    print("과제 A — S번호 N개일 때 (관측)\n")
    print(f"{'N':>6} {'스키마자':>9} {'enum몫':>7} {'S번호자릿':>8} {'패턴':>5} "
          f"{'스키마생성ms':>11} {'검사기ms':>9} {'1건검사ms':>10} {'검증ms':>8}")
    print("-" * 84)

    for n in 크기들:
        번호, s맵, 메타 = 합성(n)
        스키마 = 판정_스키마(번호, None)
        s = json.dumps(스키마, ensure_ascii=False)
        # enum 이 두 벌 실린다 — `인용.items` 와 `전제.items.근거조항` 이 같은 dict 를
        # 참조하지만 직렬화는 두 번 된다. 이게 문자수의 지배항이다.
        enum몫 = 2 * (len(json.dumps(번호, ensure_ascii=False)) + len('"enum":'))
        o = 출력(번호)

        생성ms = 재기(lambda: 판정_스키마(번호, None))
        검사기ms = 검사ms = float("nan")
        if jsonschema is not None:
            def _컴파일():
                jsonschema.Draft202012Validator.check_schema(스키마)
                return jsonschema.Draft202012Validator(스키마)
            검사기ms = 재기(_컴파일, 3)
            v = _컴파일()
            검사ms = 재기(lambda: v.is_valid(o), 3)
        검증ms = 재기(lambda: 검증(o, s맵, 메타=메타, f경로=F경로, 프롬프트=""), 3)

        마지막 = 번호[-1]
        print(f"{n:>6} {len(s):>9,} {enum몫*100//len(s):>6}% {len(마지막)-1:>8} "
              f"{'OK' if _RE_S.match(마지막) else '🔴위반':>5} "
              f"{생성ms:>11.2f} {검사기ms:>9.2f} {검사ms:>10.3f} {검증ms:>8.2f}")

    print(f"\nS번호_PATTERN = {S번호_PATTERN!r}  ·  생성식 = assemble_context.py:114 `f\"S{{i:02d}}\"`")
    if jsonschema is None:
        print("⚠️ jsonschema 미설치 — 검사기 열은 못 쟀다")


def 실맵() -> None:
    """run 191 의 실제 s맵 크기 분포. 합성 N 을 어디에 놓고 읽어야 하는지의 닻."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    import psycopg
    from _lib import db
    with psycopg.connect(db.DSN) as conn:
        rows = conn.execute(
            "SELECT jsonb_array_length(COALESCE(원출력->'인용목록','[]'::jsonb)), "
            "       (SELECT count(*) FROM jsonb_object_keys(COALESCE(원출력->'s맵','{}'::jsonb))) "
            "  FROM eval.run_items WHERE run_id = 191").fetchall()
    s = sorted(r[1] for r in rows)
    if not s:
        print("run 191 원출력에 s맵 키가 없다")
        return
    print(f"\nrun 191 실제 s맵 크기 — n={len(s)} 최소 {s[0]} · 중앙 {s[len(s)//2]} · "
          f"최대 {s[-1]} · 평균 {sum(s)/len(s):.1f}")
    print(f"인용목록 크기 최대 {max(r[0] for r in rows)}")


def 프롬프트():
    """run 191 의 s맵으로 B1~B3 원문 길이를 **복원**한다.

    run 191 원출력엔 `프롬프트길이` 가 없다(dry 경로 전용 · P1 1일차 실측). 그래서
    s맵의 (종류, id) 로 원문을 되짚어 «규정 원문이 차지한 자리» 만 다시 센다.
    B0·B4·B5·B6 는 안 들어간다 — 이건 **하한**이다.

    행분해가 늘리는 것은 원문이 아니라 «표시머리 반복»이다
    (`assemble_context.py:205` — 항 조각마다 머리를 다시 쓴다).
    """
    import psycopg
    from _lib import db

    with psycopg.connect(db.DSN) as conn:
        rows = conn.execute(
            "SELECT gold_id, 원출력->'s맵' FROM eval.run_items "
            " WHERE run_id = 191 AND 원출력 ? 's맵'").fetchall()
        본문 = {}
        for 종류, q in (
            ("chunk", "SELECT chunk_id, length(text), length(doc_id||' '||COALESCE(조번호,'')"
                      "||'('||COALESCE(조제목,'')||')') FROM corpus.chunks"),
            ("article", "SELECT article_id, length(본문), length(doc_id||' '||COALESCE(조번호,'')"
                        "||'('||COALESCE(조제목,'')||')') FROM corpus.doc_articles"),
            ("l3", "SELECT article_id, length(본문), length(COALESCE(조번호,'')"
                   "||'('||COALESCE(조제목,'')||')') FROM tenant.l3_articles"),
        ):
            본문[종류] = {r[0]: (r[1] or 0, r[2] or 0) for r in conn.execute(q).fetchall()}

    잰것 = []
    for gid, s맵 in rows:
        if not s맵:
            continue
        원문자 = 0
        본 = set()
        머리합 = 0
        for _sid, v in s맵.items():
            종류, _id = v[0], v[1]
            길이, 머리 = 본문.get(종류, {}).get(_id, (0, 0))
            머리합 += 머리 + 9          # "[Sxx] " + "\n" + 블록 구분 "\n\n"
            if (종류, _id) not in 본:
                본.add((종류, _id))
                원문자 += 길이
        잰것.append((gid, len(s맵), 원문자, 머리합))

    if not 잰것:
        print("\nrun 191 에 s맵이 실린 행이 없다")
        return
    합 = sorted(x[2] + x[3] for x in 잰것)
    n = sorted(x[1] for x in 잰것)
    print(f"\nrun 191 B1~B3 원문 자리 (복원 · 하한) — {len(잰것)}건")
    print(f"  S번호      중앙 {n[len(n)//2]:>6,} · 최대 {n[-1]:>6,}")
    print(f"  원문+머리  중앙 {합[len(합)//2]:>6,}자 · 최대 {합[-1]:>6,}자")
    # 🔴 argmax 의 gold_id 는 미사용 41 일 수 있다. 크기만 쓰고 **번호는 가린다** —
    #    held-out 문항을 지목하는 순간 「어느 문항이 특이한가」를 알게 된다.
    튜닝 = set(json.load(open(부분집합, encoding="utf-8"))["튜닝52"])
    최 = max(잰것, key=lambda x: x[2] + x[3])
    이름 = f"gold_id={최[0]}" if 최[0] in 튜닝 else "(미사용 41 문항 — 번호 가림)"
    print(f"  최대 문항 {이름}  S={최[1]}  원문 {최[2]:,}자 + 머리 {최[3]:,}자")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--실맵", action="store_true", help="run 191 의 실제 s맵 크기 (DB 읽기)")
    ap.add_argument("--프롬프트", action="store_true", help="run 191 프롬프트 원문 자리 복원")
    a = ap.parse_args()
    if not a.프롬프트:
        표()
    if a.실맵:
        실맵()
    if a.프롬프트:
        프롬프트()


if __name__ == "__main__":
    main()
