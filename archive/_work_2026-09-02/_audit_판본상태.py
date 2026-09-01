# -*- coding: utf-8 -*-
"""판본·품질 라벨이 **현실과 어긋난 곳**을 전수조사한다.

`scripts/audit_db.py`(중앙세션)는 참조 무결성을 본다 — 고아 행·dangling·널.
이 파일은 다른 축이다: **라벨은 멀쩡한데 내용이 틀린 상태**를 찾는다.

    status='active'   인데 실제로는 작년 판
    parse_quality='low' 인데 검색 진입점
    extraction='vlm'  인데 판정 근거로 인용됨
    superseded        인데 그게 진짜 현행 (파싱 실패로 밀려난 것)

이 부류는 무결성 검사를 **전부 통과한다.** FK 도 안 깨지고 널도 아니다.
그래서 조용하고, 그래서 위험하다 — 검색은 정상 동작하고 답도 나오는데 근거가 작년 것이다.

🔴 이 스크립트는 읽기만 한다. 고치는 건 `corpus.documents` 소유자(중앙세션)다.

실행:  PYTHONIOENCODING=utf-8 python scripts/_work/_audit_판본상태.py
"""
from __future__ import annotations

import io
import os
import re
import sys

if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

발견: list[tuple[str, str]] = []       # (심각도, 한 줄)


def 알림(심각: str, s: str) -> None:
    발견.append((심각, s))


def 규범군(doc_id: str) -> str:
    """같은 규범의 판본끼리 묶기 위한 키. 연도·차수 표기를 지운다."""
    s = doc_id
    s = re.sub(r"\d{4}년?", "", s)
    s = re.sub(r"제?\d+차\s*개?정?(안)?", "", s)
    s = re.sub(r"_?\d{8}$", "", s)
    s = re.sub(r"[()（）\[\]\s_.\-]+", "", s)
    return s


def main() -> None:
    with psycopg.connect(DSN) as conn:
        docs = conn.execute("""
            SELECT d.doc_id, d.layer, d.status, d.parse_quality, d.extraction,
                   d.version, d.시행일, d.index_target, d.retrieval_scope,
                   (SELECT count(*) FROM corpus.doc_articles a WHERE a.doc_id=d.doc_id),
                   (SELECT count(*) FROM corpus.chunks c WHERE c.doc_id=d.doc_id)
              FROM corpus.documents d ORDER BY d.doc_id""").fetchall()

        print(f"documents {len(docs)}행 전수조사\n")

        # ── 1. 판본 역전 — active 가 최신이 아니다 ────────────────────────
        군: dict[str, list] = {}
        for r in docs:
            군.setdefault(규범군(r[0]), []).append(r)
        def 시점(r):
            """시행일이 있으면 그걸, 없으면 version/doc_id 의 연도를 쓴다.

            🔴 시행일 NULL 이 283행 중 53행(index_target 9행)이라, 시행일만 보면
               **재도전 판본 역전을 통째로 놓친다** (세 판본 모두 NULL). 실제로 놓쳤다.
            """
            if r[6]:
                return (r[6].year, r[6].month, r[6].day)
            y = re.findall(r"(20\d{2})", (r[5] or "") + " " + r[0])
            return (int(max(y)), 0, 0) if y else None

        for k, v in sorted(군.items()):
            if len(v) < 2:
                continue
            시 = [(x, 시점(x)) for x in v]
            시 = [(x, t) for x, t in 시 if t]
            if len(시) < 2:
                continue
            최신, t최신 = max(시, key=lambda z: z[1])
            for a, ta in 시:
                if a[2] == "active" and t최신 > ta:
                    알림("🔴", f"판본 역전: active '{a[0][:40]}'({a[5]}) 보다 "
                              f"'{최신[0][:40]}'({최신[5]}) 가 최신인데 "
                              f"status={최신[2]} · 조={최신[9]} · {최신[3]}/{최신[4]}")

        # ── 2. 저품질인데 인덱스에 들어가 있다 ────────────────────────────
        for r in docs:
            doc, _, st, pq, ex, _, _, it, scope, 조, 청크 = r
            if it and pq == "low":
                알림("🔴", f"저품질 인덱싱: parse_quality=low 인데 index_target · "
                          f"scope={scope} · 청크={청크} · {doc[:50]}")
            if it and ex == "vlm":
                알림("🔴", f"판독본 인덱싱: extraction=vlm 인데 index_target · "
                          f"scope={scope} · 청크={청크} · {doc[:50]}")

        # ── 3. 인덱스 대상인데 알맹이가 없다 ──────────────────────────────
        for r in docs:
            doc, _, st, pq, ex, _, _, it, scope, 조, 청크 = r
            if it and 조 == 0:
                알림("🔴", f"빈 인덱스 대상: index_target 인데 조 0건 · {doc[:56]}")
            elif it and 청크 == 0:
                알림("⚠️", f"청크 0: index_target·조 {조} 인데 chunks 0 · {doc[:52]}")

        # ── 4. 파싱 실패가 superseded 로 은폐됐나 ─────────────────────────
        for r in docs:
            doc, _, st, pq, ex, ver, 시행, it, scope, 조, 청크 = r
            if st == "superseded" and 조 == 0 and (pq == "low" or ex == "vlm"):
                알림("🔴", f"파싱 실패 은폐: superseded·조 0건·{pq}/{ex} — 밀려난 게 "
                          f"아니라 **못 읽은 것**일 수 있다 · {doc[:44]}")

        # ── 5. 사업 커버리지 ──────────────────────────────────────────────
        스코프 = ["예비창업패키지", "초기창업패키지", "재도전성공패키지", "창업도약패키지",
                "창업중심대학", "초격차 스타트업 프로젝트", "모두의 창업 프로젝트", "TIPS"]
        rules = {x[0]: x[1] for x in conn.execute(
            "SELECT 사업명, count(*) FROM corpus.rules GROUP BY 1").fetchall()}
        prec = {x[0]: x[1] for x in conn.execute(
            "SELECT 사업명, count(*) FROM corpus.precedence_rules GROUP BY 1").fetchall()}
        gold = {x[0]: x[1] for x in conn.execute(
            "SELECT 사업명, count(*) FROM eval.golden_set GROUP BY 1").fetchall()}
        print("== 사업 커버리지 (스코프 8개)")
        print(f"   {'사업':<24}{'rules':>6}{'prec':>6}{'golden':>8}")
        for s in 스코프:
            r_, p_, g_ = rules.get(s, 0), prec.get(s, 0), gold.get(s, 0)
            print(f"   {s:<24}{r_:>6}{p_:>6}{g_:>8}"
                  + ("   <- 룰 없음" if not r_ else ""))
            if not r_:
                알림("🔴", f"룰 0행: {s}")
            elif not g_:
                알림("⚠️", f"골든셋 0문항 — 룰 {r_}행을 검증할 수단이 없다: {s}")
        print()

        # ── 6. 검색 진입점의 판본 건강도 ──────────────────────────────────
        진입 = conn.execute("""
            SELECT d.doc_id, d.status, d.parse_quality, d.extraction, count(*)
              FROM corpus.chunks c JOIN corpus.documents d ON d.doc_id=c.doc_id
             WHERE c.retrieval_scope='진입점'
             GROUP BY 1,2,3,4 ORDER BY 5 DESC""").fetchall()
        나쁨 = [x for x in 진입 if x[1] != "active" or x[2] == "low" or x[3] == "vlm"]
        print(f"== 검색 진입점 문서 {len(진입)}종 · 상태 이상 {len(나쁨)}종")
        for x in 나쁨:
            알림("🔴", f"진입점 이상: {x[0][:44]} status={x[1]} {x[2]}/{x[3]} 청크={x[4]}")
        print()

    # ── 결과 ──────────────────────────────────────────────────────────────
    심각 = [x for x in 발견 if x[0] == "🔴"]
    경고 = [x for x in 발견 if x[0] == "⚠️"]
    print(f"== 발견 {len(발견)}건 (심각 {len(심각)} · 경고 {len(경고)})\n")
    for m, s in 심각:
        print(f"  {m} {s}")
    if 경고:
        print()
    for m, s in 경고:
        print(f"  {m} {s}")


if __name__ == "__main__":
    main()
