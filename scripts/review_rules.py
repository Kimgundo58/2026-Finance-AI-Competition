# -*- coding: utf-8 -*-
"""D단계 검수 도구 : 룰 19행을 원문과 나란히 보여주고 y/n 을 받는다.

  y  맞음   → verified=true 로 저장
  n  틀림   → 메모를 남기고 다음으로 (수정은 pgweb 또는 SQL 로)
  s  나중에 → 건너뛰기
  q  종료   → 진행 상황은 DB 에 저장되어 있으므로 언제든 이어서 가능

실행:  python scripts/review_rules.py
"""
from __future__ import annotations
import io, os, re, sys, textwrap

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
W = 74


def 원문찾기(conn, doc_id: str, 비목: str) -> str:
    """붙임2 본문에서 해당 비목 구간만 잘라낸다."""
    row = conn.execute("""
        SELECT 본문 FROM doc_articles
        WHERE doc_id = %s AND 조번호 = '붙임2' LIMIT 1
    """, (doc_id,)).fetchone()
    if not row:
        return "(붙임2 원문을 찾지 못했습니다 — doc_articles 확인 필요)"
    본문 = row[0]

    key = re.sub(r"\(.*?\)", "", 비목).strip()          # '기계장치(...)' → '기계장치'
    pat = r"\s*".join(map(re.escape, key))              # 셀 분리로 줄바꿈이 낀 경우 대응
    m = re.search(pat, 본문)
    if not m:
        return f"({비목} 구간을 자동으로 찾지 못했습니다. 붙임2 전문을 직접 확인하세요)"

    start = m.start()
    nxt = re.search(r"\n\s*(?:정의)\s*\n", 본문[start + 200:])
    end = start + 200 + (nxt.end() if nxt else 2500)
    seg = 본문[start:min(end, start + 2500)]
    return seg.strip()


def 표시(i, n, r, 원문):
    (rid, 사업, 비목, 허용, 승인, 조건, 유형, 값, 단위, 증빙, 금지, 허용예, 근거) = r
    print("\n" + "=" * W)
    print(f"[{i}/{n}]  {사업} · {비목}")
    print("=" * W)
    print("추출값:")
    print(f"  허용      : {허용}")
    한도 = f"{유형} {값} {단위}" if 유형 else "(없음)"
    print(f"  한도      : {한도}")
    print(f"  사전승인  : {승인}" + (f"\n              └ {조건}" if 조건 else ""))
    print(f"  증빙({len(증빙 or [])}건) :")
    for s in (증빙 or []):
        print(f"      - {s}")
    if 금지:
        print(f"  금지예시  : {', '.join(금지)}")
    print("-" * W)
    print("원문 [붙임2]:")
    for line in textwrap.wrap(re.sub(r"\n+", " ", 원문)[:1400], width=W - 4):
        print("  " + line)
    print("=" * W)


def main():
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("""
            SELECT rule_id, 사업명, 비목, 허용, 사전승인, 사전승인_조건,
                   한도_유형, 한도_값, 한도_단위, 증빙, 금지예시, 허용예시, 근거
            FROM rules WHERE verified = FALSE ORDER BY 사업명 DESC, rule_id
        """).fetchall()

        총 = conn.execute("SELECT count(*) FROM rules").fetchone()[0]
        done = 총 - len(rows)
        if not rows:
            print(f"검수 완료 — {총}/{총}행 verified=true. D단계 끝!")
            return
        print(f"검수 대상 {len(rows)}행 (전체 {총}행 중 {done}행 완료)")
        print("y=맞음 / n=틀림(메모) / s=나중에 / q=종료\n")

        검수자 = input("검수자 이름: ").strip() or "미기재"

        for i, r in enumerate(rows, 1):
            doc = (r[12] or [{}])[0].get("doc_id", "") if isinstance(r[12], list) else ""
            표시(done + i, 총, r, 원문찾기(conn, doc, r[2]))

            while True:
                ans = input("맞습니까? [y/n/s/q] > ").strip().lower()
                if ans in ("y", "n", "s", "q"):
                    break
            if ans == "q":
                print("\n중단. 진행 상황은 저장되어 있습니다.")
                break
            if ans == "s":
                continue
            if ans == "y":
                conn.execute("""UPDATE rules SET verified=TRUE, 검수자=%s,
                                검수일=CURRENT_DATE WHERE rule_id=%s""", (검수자, r[0]))
                conn.commit()
                print("  ✓ verified")
            else:
                memo = input("  무엇이 틀렸나요? > ").strip()
                print(f"  ✗ 메모 기록: {memo}")
                print(f"     수정 SQL 예시:")
                print(f"     UPDATE rules SET 한도_값=..., verified=TRUE,")
                print(f"       검수자='{검수자}', 검수일=CURRENT_DATE WHERE rule_id={r[0]};")
                with open("scripts/_review_memo.txt", "a", encoding="utf-8") as f:
                    f.write(f"rule_id={r[0]}\t{r[1]}\t{r[2]}\t{memo}\n")

        남음 = conn.execute("SELECT count(*) FROM rules WHERE verified=FALSE").fetchone()[0]
        print(f"\n남은 검수: {남음}행")


if __name__ == "__main__":
    main()
