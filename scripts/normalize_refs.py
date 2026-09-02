# -*- coding: utf-8 -*-
"""`corpus.refs.dst_doc_id` 를 `corpus.documents.doc_id` 로 정규화한다.

## 왜 필요한가 — 참조 확장이 지금 작동하지 않는다

`RAG.md` §4-2 가 199문서를 `폐포전용`(검색 후보에서 빼고 참조로만 도달)으로 돌렸다.
그 안전 근거는 "검색에 안 걸려도 refs 참조 확장으로 도달한다" 인데, 실측하면 **도달하지 않는다**:

    진입점 문서발 resolved 참조        3,907건
      그중 dst 가 폐포전용 문서            0건   <- 하나도 없다

원인은 `dst_doc_id` 가 `doc_id` 로 정규화돼 있지 않은 것이다. 값이 세 갈래로 섞여 있다:

    법령 PDF/L1_법령/법인세법시행령      <- 파일 경로 (확장자·날짜 없음)
    L1_통합관리지침_제14차               <- 약칭
    L1_소득세법_20260701                 <- 정상 doc_id

resolved 17,386건 중 **8,668건(50%)이 documents 에 없는 dst** 를 가리킨다.
그래서 폐포전용 18,594청크가 검색으로도 참조로도 도달 불가다.

## 무엇을 하나

`dst_doc_id` 만 고친다. `해소상태`·`참조문자열`·`src_*` 는 건드리지 않는다 —
원문 표기(`참조문자열`)는 화면 7 이 "귀 기관 규정은 제33조라 하지만…" 을 그리는 재료다.

🔴 **매칭 실패는 조용히 넘기지 않는다.** 못 고친 것은 `해소상태` 를 그대로 두고
목록으로 보고한다. 억지로 비슷한 문서에 붙이면 **엉뚱한 조문이 근거로 인용된다** —
"인용은 S번호 추출" 방어선이 여기서 뚫린다.

## 판본 선택

같은 법령의 현행·구판이 함께 있으면 **`status='active'` 를 고른다.**
active 가 여럿이면 매칭하지 않고 보고한다 (사람이 정할 문제다).

실행:
    PYTHONIOENCODING=utf-8 python scripts/normalize_refs.py            # dry-run
    PYTHONIOENCODING=utf-8 python scripts/normalize_refs.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")


def 접기(s: str) -> str:
    """비교용 정규화. 공백·중점·물결·괄호 차이를 흡수한다.

    실제 값에 '대ㆍ중소기업'(U+3187 계열 중점)과 '대·중소기업'(U+00B7) 이 섞여 있고,
    '산업교육진흥및산학연협력촉진에관한법률' 처럼 공백이 통째로 빠진 표기도 있다.
    """
    s = unicodedata.normalize("NFKC", s or "")
    for ch in " \t·ㆍ・‧∙,()（）[]{}「」『』ㆍ~〜-–—_":
        s = s.replace(ch, "")
    return s.lower()


# ── 약칭 별칭 — 규칙으로 못 푸는 것만. 각 줄에 근거를 남긴다 ──────────────
#    기계 규칙이 아니라 판단이다. 확신이 없으면 여기 넣지 말고 미매칭으로 남겨라 —
#    엉뚱한 문서에 붙으면 그 조문이 근거로 인용된다.
별칭 = {
    # 본문이 "통합관리지침 제33조" 처럼 줄여 부른다. "제14차" 가 명시돼 있어 판본이 특정된다.
    # 제13차는 「창업사업화 지원사업」이라 다른 규범이다 — 혼동 금지.
    "L1_통합관리지침_제14차": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
}


def 후보들(name: str, docs: list[tuple[str, str]]) -> list[str]:
    """`L1_<name>_<8자리날짜>` 또는 접은 문자열이 같은 doc_id. active 우선."""
    타깃 = 접기(name)
    hits = []
    for doc_id, _ in docs:
        if not doc_id.startswith("L1_"):
            continue
        몸통 = doc_id[3:]
        # 뒤의 _YYYYMMDD 를 떼어낸다 (없을 수도 있다)
        if len(몸통) > 9 and 몸통[-8:].isdigit() and 몸통[-9] == "_":
            몸통 = 몸통[:-9]
        if 접기(몸통) == 타깃:
            hits.append(doc_id)
    active = {d for d, s in docs if s == "active"}
    act = [d for d in hits if d in active]
    return act or hits


def 이름뽑기(dst: str) -> str | None:
    """dst 값에서 문서 이름을 뽑는다. 경로형이면 마지막 조각, 확장자는 뗀다."""
    t = (dst or "").replace("\\", "/")
    if "/" in t:
        t = t.split("/")[-1]
    for ext in (".pdf", ".xml", ".hwp", ".hwpx"):
        if t.lower().endswith(ext):
            t = t[: -len(ext)]
    return t or None


def 직접매칭(name: str, 실재: set[str]) -> str | None:
    """doc_id 와 접은 문자열이 같은 것. L1_ 접두사가 없는 문서(세부관리기준 등)용."""
    타깃 = 접기(name)
    hits = [d for d in 실재 if 접기(d) == 타깃]
    return hits[0] if len(hits) == 1 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 UPDATE 한다 (기본은 dry-run)")
    a = ap.parse_args()

    with psycopg.connect(DSN, autocommit=False) as conn:
        docs = conn.execute("SELECT doc_id, status FROM corpus.documents").fetchall()
        실재 = {d for d, _ in docs}
        미해소 = conn.execute("""
            SELECT dst_doc_id, count(*) FROM corpus.refs r
             WHERE dst_doc_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM corpus.documents d WHERE d.doc_id = r.dst_doc_id)
             GROUP BY 1 ORDER BY 2 DESC""").fetchall()

        매핑: dict[str, str] = {}
        모호, 실패 = [], []
        for dst, n in 미해소:
            if dst in 별칭 and 별칭[dst] in 실재:      # ① 손으로 정한 별칭이 최우선
                매핑[dst] = 별칭[dst]
                continue
            nm = 이름뽑기(dst)
            if not nm:
                실패.append((dst, n)); continue
            직 = 직접매칭(nm, 실재)                     # ② doc_id 와 그대로 같은 것
            if 직:
                매핑[dst] = 직; continue
            h = 후보들(nm, docs)                        # ③ L1_<이름>_<날짜> 규칙
            if len(h) == 1:
                매핑[dst] = h[0]
            elif len(h) > 1:
                모호.append((dst, n, h))
            else:
                실패.append((dst, n))

        총 = sum(n for _, n in 미해소)
        해소건 = sum(n for dst, n in 미해소 if dst in 매핑)
        print(f"미해소 dst {len(미해소)}종 / {총:,}건")
        if not 총:
            # 고칠 게 없는 것은 정상이다 — build_refs 가 처음부터 doc_id 로 넣으면 여기가 빈다.
            print("  고칠 것이 없다. (build_refs.py 가 dst 를 doc_id 로 넣었다)")
            return
        print(f"  매핑 성공   {len(매핑):3}종 / {해소건:,}건 ({해소건/총*100:.1f}%)")
        print(f"  모호(후보>1) {len(모호):3}종 / {sum(n for _,n,_ in 모호):,}건 — 손대지 않는다")
        print(f"  실패        {len(실패):3}종 / {sum(n for _,n in 실패):,}건 — 손대지 않는다")

        if 모호:
            print("\n⚠️ 모호 — 사람이 판본을 정해야 한다:")
            for dst, n, h in 모호[:10]:
                print(f"  {dst[:50]:52} {n:5}건 -> {h}")
        if 실패:
            print("\n미매칭 상위 (코퍼스 밖이면 정상이다):")
            for dst, n in 실패[:12]:
                print(f"  {dst[:58]:60} {n:5}건")

        if not a.apply:
            print("\n(dry-run. --apply 를 주면 UPDATE 한다)")
            return

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE corpus.refs SET dst_doc_id = %s WHERE dst_doc_id = %s",
                [(v, k) for k, v in 매핑.items()])
            갱신 = cur.rowcount
        conn.commit()
        print(f"\nUPDATE {갱신:,}행")

        # 사후 검증 — 고친 뒤에 참조 확장이 실제로 켜졌는지 본다
        남은 = conn.execute("""
            SELECT count(*) FROM corpus.refs r
             WHERE dst_doc_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM corpus.documents d WHERE d.doc_id = r.dst_doc_id)
            """).fetchone()[0]
        도달 = conn.execute("""
            SELECT count(*) FROM corpus.refs r
              JOIN corpus.documents s ON s.doc_id = r.src_doc_id AND s.retrieval_scope = '진입점'
              JOIN corpus.documents d ON d.doc_id = r.dst_doc_id AND d.retrieval_scope = '폐포전용'
             WHERE r.해소상태 = 'resolved'""").fetchone()[0]
        print(f"남은 미해소 dst   {남은:,}건")
        print(f"진입점 -> 폐포전용 resolved 참조  {도달:,}건  (이 값이 0 이면 폐포는 여전히 꺼져 있다)")


if __name__ == "__main__":
    main()
