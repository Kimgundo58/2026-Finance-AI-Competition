# -*- coding: utf-8 -*-
"""corpus.chunks 부분 재청킹 동기화 — 8개 문서를 운영본으로 교체 + golden_chunks 재매칭.

레인: ai-33 QA ④ (2026-09-08). **실행은 중앙만.** 이 스크립트는 기본이 `--dry` 다
(`--commit` 을 명시해야 실제로 쓴다).

━━ 왜 단순 delete+insert 가 위험한가 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
8개 문서의 로컬 chunk_id 중 88개(참조 gold행 308건)를 `eval.golden_chunks` 가 참조한다.
`golden_chunks_chunk_id_fkey` 는 `ON DELETE SET NULL` 인데 `golden_chunks_실패_check`
(매칭방법≠'실패' 면 chunk_id NOT NULL 강제) 와 충돌해서, 참조된 chunk 하나만 지워도
그 순간 CHECK 위반으로 **트랜잭션 전체가 롤백**된다(부분 실패가 아니라 전체 실패).

━━ 절차 (문서별 SAVEPOINT + 재매칭 게이트별 SAVEPOINT) ━━━━━━━━━━━━━━━━━━━━━━
1. 스냅샷 — 8문서 chunk_id 를 참조하는 golden_chunks 전 행 + golden_set.정답근거 중
   8문서를 가리키는 항목을 «지우기 전» 파일로 남긴다(되돌릴 수 없는 삭제 전 기록).
2. 문서별로(SAVEPOINT):
   a) 그 문서 chunk_id 를 참조하는 golden_chunks 행을 **먼저 지운다** — 이러면 다음 단계
      corpus.chunks 삭제 때 아무도 그 chunk_id 를 안 봐서 SET NULL/CHECK 충돌이 안 난다.
   b) corpus.chunks 에서 그 문서 행을 지운다(chunk_terms·chunk_len 은 CASCADE).
   c) 입력 JSON 의 그 문서분을 삽입한다 — chunk_id 는 시퀀스가 새로 준다(운영 chunk_id
      재사용 안 함 — 로컬 다른 문서와 번호가 겹칠 위험을 원천적으로 없앤다). article_id 는
      `corpus.doc_articles`(doc_id+조번호)로 되찾고, embedding 은 로컬에서 다시 계산한다
      (KURE-v1, CPU, GPU 비용 없음 — 운영 임베딩 벡터를 받아올 필요가 없다).
3. 재매칭 — 1에서 지운 (gold_id, 근거순번) 쌍만, `golden_set.정답근거` 를 다시 읽어
   `scripts/archive/eval/pin_golden_chunks.매칭()` **그대로 재사용**(재구현 안 함)해서
   새 chunk_id 로 다시 꽂는다. 실패하면 매칭방법='실패'+실패사유로 명시(조용히 안 넘김).
4. 읽기검산 — 새 커넥션으로 다시 열어, 영향받은 모든 (gold_id,근거순번) 이 chunk_id 를
   갖거나 명시적 '실패' 인지 확인. **개수만이 아니라 내용도** 본다 — 새 chunk.text 가
   golden_set.정답근거[].원문 을 정규화 후에도 포함하는지 재확인한다.
5. `--dry`(기본) 는 1~4 를 전부 실행하되 **마지막에 항상 ROLLBACK** 한다 — DB 에 아무
   것도 안 남는다. 뭘 지우고 뭘 넣을지 요약을 콘솔에 찍는다. `--commit` 을 줘야 COMMIT.

━━ 입력 파일 모양 (운영 chunk 덤프 — ai-33 이 이 모양으로 준다) ━━━━━━━━━━━━━━
```json
{"청크": [
  {"doc_id": "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
   "조번호": "제38조", "조제목": "외주용역비", "항호": null, "페이지": 42,
   "사업명": null, "적용대상": "공통",
   "layer": "L1", "기관id": null, "parse_quality": "high", "version": null,
   "status": "active", "retrieval_scope": "진입점",
   "text": "제38조(외주용역비) ① ... (그 청크의 전체 본문, 그대로)"}
  , "..."
]}
```
`chunk_id`·`article_id`·`embedding` 은 안 보낸다 — 이 스크립트가 새로 만든다.
`corpus.chunks` 의 나머지 컬럼과 이름·타입을 맞춘다(NOT NULL: layer·parse_quality·
status·retrieval_scope·text). **`retrieval_scope` 는 `'진입점'`·`'폐포전용'` 둘 중
하나뿐이다**(CHECK 제약 실측 확인, 2026-09-07) — 그 칸을 대충 채우면 스모크테스트처럼
그 문서 전체가 SAVEPOINT 롤백된다(다른 문서는 안 건드리니 전체 실패는 아니다). 운영
값을 그대로 실어달라 — 이 스크립트가 추측하지 않는다. 8개 문서분을 한 파일에 다
담아도 되고 나눠도 된다(`--input` 을 여러 번 줄 수 있다 — 합쳐서 처리).

실행:
    PYTHONIOENCODING=utf-8 python scripts/sync_corpus_chunks.py --input 운영덤프.json
        (기본 dry — 아무것도 안 바뀐다)
    PYTHONIOENCODING=utf-8 python scripts/sync_corpus_chunks.py --input 운영덤프.json --commit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "archive" / "eval"))

import psycopg  # noqa: E402
from _lib import db  # noqa: E402
import retrieve  # noqa: E402 — 임베딩(text) 재사용, CPU
from pin_golden_chunks import 매칭 as _매칭  # noqa: E402 — 재구현하지 않는다

대상문서 = [
    "초격차 스타트업 프로젝트 세부관리기준(제10차)",
    "창업중심대학 세부관리기준2025년 개정",
    "붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문",
    "창업도약패키지 세부관리기준(2025년)",
    "모두의 창업 프로젝트 세부관리기준(개정본)",
    "초기창업패키지 세부관리기준(2025년)",
    "예비창업패키지 세부관리기준(2025년)",
    "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
]
필수컬럼 = ("doc_id", "layer", "parse_quality", "status", "retrieval_scope", "text")


def _스냅샷(cur) -> dict:
    cur.execute(
        """SELECT gc.gc_id, gc.gold_id, gc.\"근거순번\", gc.chunk_id, gc.article_id,
                  gc.doc_id, gc.\"조번호\", gc.\"매칭방법\", gc.\"실패사유\",
                  c.text AS 현재청크텍스트
             FROM eval.golden_chunks gc
             JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id
            WHERE c.doc_id = ANY(%s)
            ORDER BY gc.gold_id, gc.\"근거순번\"""", (대상문서,))
    cols = ("gc_id", "gold_id", "근거순번", "chunk_id", "article_id", "doc_id",
            "조번호", "매칭방법", "실패사유", "현재청크텍스트")
    골든청크행 = [dict(zip(cols, r)) for r in cur.fetchall()]

    cur.execute("SELECT gold_id, 정답근거 FROM eval.golden_set WHERE 정답근거 IS NOT NULL")
    영향받는_근거: list[dict] = []
    for gid, 근거 in cur.fetchall():
        for i, g in enumerate(근거 or []):
            if isinstance(g, dict) and g.get("doc") in 대상문서:
                영향받는_근거.append({"gold_id": gid, "근거순번": i, **g})

    return {"뜬시각": datetime.now(timezone.utc).isoformat(),
            "대상문서": 대상문서,
            "golden_chunks_스냅샷": 골든청크행,
            "golden_set_정답근거_스냅샷": 영향받는_근거}


def _article_id(cur, doc_id: str, 조번호: str | None) -> int | None:
    if not 조번호:
        return None
    cur.execute("SELECT article_id FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
                (doc_id, 조번호))
    r = cur.fetchone()
    return r[0] if r else None


def _문서교체(cur, doc_id: str, 신규행: list[dict]) -> dict:
    cur.execute("SAVEPOINT sp_doc")
    try:
        cur.execute(
            "DELETE FROM eval.golden_chunks WHERE chunk_id IN "
            "(SELECT chunk_id FROM corpus.chunks WHERE doc_id=%s)", (doc_id,))
        지운골든 = cur.rowcount
        cur.execute("DELETE FROM corpus.chunks WHERE doc_id=%s", (doc_id,))
        지운청크 = cur.rowcount

        새id들 = []
        for row in 신규행:
            빠짐 = [k for k in 필수컬럼 if not row.get(k)]
            if 빠짐:
                raise ValueError(f"필수 컬럼 누락 {빠짐}: {row.get('조번호')}")
            벡터 = retrieve.임베딩(row["text"])
            aid = _article_id(cur, doc_id, row.get("조번호"))
            cur.execute(
                """INSERT INTO corpus.chunks
                   (doc_id, article_id, layer, "기관id", parse_quality, version, status,
                    retrieval_scope, "조번호", "조제목", "항호", "페이지", "사업명",
                    "적용대상", text, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING chunk_id""",
                (doc_id, aid, row["layer"], row.get("기관id"), row["parse_quality"],
                 row.get("version"), row["status"], row["retrieval_scope"],
                 row.get("조번호"), row.get("조제목"), row.get("항호"), row.get("페이지"),
                 row.get("사업명"), row.get("적용대상"), row["text"], 벡터))
            새id들.append(cur.fetchone()[0])
        cur.execute("RELEASE SAVEPOINT sp_doc")
        return {"지운골든": 지운골든, "지운청크": 지운청크, "새청크수": len(새id들),
                "새chunk_id_범위": (min(새id들), max(새id들)) if 새id들 else None}
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_doc")
        return {"오류": f"{type(e).__name__}: {e}"}


def _재매칭(cur, 영향받는_근거: list[dict]) -> list[dict]:
    결과 = []
    for 항 in 영향받는_근거:
        gid, i, doc, 조, 원문 = 항["gold_id"], 항["근거순번"], 항.get("doc"), 항.get("조번호"), 항.get("원문")
        cur.execute("SAVEPOINT sp_pin")
        try:
            방법, ids, 사유 = _매칭(cur, doc, 조, 원문)
            if ids:
                for cid in ids:
                    cur.execute(
                        """INSERT INTO eval.golden_chunks
                           (gold_id, "근거순번", chunk_id, article_id, doc_id, "조번호", "매칭방법")
                           SELECT %s, %s, c.chunk_id, c.article_id, c.doc_id, %s, %s
                             FROM corpus.chunks c WHERE c.chunk_id = %s
                           ON CONFLICT DO NOTHING""",
                        (gid, i, 조, 방법, cid))
                결과.append({"gold_id": gid, "근거순번": i, "매칭방법": 방법, "chunk_id들": ids})
            else:
                cur.execute(
                    """INSERT INTO eval.golden_chunks
                       (gold_id, "근거순번", doc_id, "조번호", "매칭방법", "실패사유")
                       VALUES (%s,%s,%s,%s,'실패',%s) ON CONFLICT DO NOTHING""",
                    (gid, i, doc, 조, 사유))
                결과.append({"gold_id": gid, "근거순번": i, "매칭방법": "실패", "실패사유": 사유})
            cur.execute("RELEASE SAVEPOINT sp_pin")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_pin")
            결과.append({"gold_id": gid, "근거순번": i, "매칭방법": "실패",
                       "실패사유": f"재매칭 중 예외 {type(e).__name__}: {e}"})
    return 결과


def _읽기검산(dsn: str, 영향받는_근거: list[dict]) -> dict:
    """🔴 새 커넥션 — 같은 트랜잭션 안에서 보면 '보였는데 사실 커밋 전'을 놓친다."""
    import re
    _잡문자 = re.compile(r"[\s　·,.\"'()（）「」『』]")

    def norm(s):
        return _잡문자.sub("", s or "")

    문제 = []
    with psycopg.connect(dsn) as conn2, conn2.cursor() as cur2:
        for 항 in 영향받는_근거:
            gid, i = 항["gold_id"], 항["근거순번"]
            cur2.execute(
                """SELECT gc.chunk_id, gc."매칭방법", gc."실패사유", c.text
                     FROM eval.golden_chunks gc
                     LEFT JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id
                    WHERE gc.gold_id=%s AND gc."근거순번"=%s""", (gid, i))
            rows = cur2.fetchall()
            if not rows:
                문제.append({"gold_id": gid, "근거순번": i, "문제": "재매칭 후 golden_chunks 행이 0건"})
                continue
            for chunk_id, 방법, 사유, 텍스트 in rows:
                if 방법 == "실패":
                    if not 사유:
                        문제.append({"gold_id": gid, "근거순번": i, "문제": "매칭방법=실패인데 실패사유가 비었다"})
                    continue
                if chunk_id is None:
                    문제.append({"gold_id": gid, "근거순번": i, "문제": "매칭방법이 실패가 아닌데 chunk_id가 NULL"})
                    continue
                # 내용 검산 — 원래 앵커(정답근거.원문)가 새 청크 안에 정규화 후 있는지
                원문 = 항.get("원문")
                if 원문 and norm(원문)[:40] not in norm(텍스트):
                    문제.append({"gold_id": gid, "근거순번": i, "chunk_id": chunk_id,
                               "문제": "내용검산 실패 — 원문 앵커가 새 청크 텍스트에 없다"})
    return {"검사한_근거쌍수": len(영향받는_근거), "문제": 문제}


def 실행(입력파일들: list[Path], *, commit: bool) -> None:
    청크: list[dict] = []
    for p in 입력파일들:
        d = json.loads(p.read_text(encoding="utf-8"))
        청크.extend(d["청크"])
    문서별 = {}
    for row in 청크:
        문서별.setdefault(row["doc_id"], []).append(row)

    없는문서 = set(대상문서) - set(문서별)
    if 없는문서:
        print(f"🔴 입력에 없는 대상문서 {len(없는문서)}개 — 그 문서는 건드리지 않는다: {없는문서}")

    with psycopg.connect(db.DSN) as conn:
        cur = conn.cursor()

        스냅 = _스냅샷(cur)
        스냅경로 = ROOT / "scratchpad" / f"_sync_스냅샷_{datetime.now():%Y%m%d_%H%M%S}.json"
        스냅경로.write_text(json.dumps(스냅, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"① 스냅샷 {len(스냅['golden_chunks_스냅샷'])}행(golden_chunks) + "
              f"{len(스냅['golden_set_정답근거_스냅샷'])}행(정답근거) -> {스냅경로}")

        print("\n② 문서 교체")
        문서결과 = {}
        for doc in 대상문서:
            if doc not in 문서별:
                continue
            r = _문서교체(cur, doc, 문서별[doc])
            문서결과[doc] = r
            상태 = "🔴 실패" if "오류" in r else "OK"
            print(f"  {상태} {doc[:40]:40} {r}")

        print("\n③ 재매칭")
        재매칭결과 = _재매칭(cur, 스냅["golden_set_정답근거_스냅샷"])
        매칭집계 = {}
        for r in 재매칭결과:
            매칭집계[r["매칭방법"]] = 매칭집계.get(r["매칭방법"], 0) + 1
        print(f"  {매칭집계}")
        실패목록 = [r for r in 재매칭결과 if r["매칭방법"] == "실패"]
        if 실패목록:
            print(f"  🔴 재매칭 실패 {len(실패목록)}건(사유와 함께 DB에 남음):")
            for r in 실패목록[:20]:
                print(f"    gold_id={r['gold_id']} 근거순번={r['근거순번']} — {r.get('실패사유')}")

        if commit:
            conn.commit()
            print("\n✅ COMMIT 했다.")
        else:
            conn.rollback()
            print("\n(--dry) 전부 ROLLBACK 했다 — DB 에 아무것도 안 남았다.")
            print("   커밋 후 상태를 미리 보려면 아래 재매칭 집계·실패목록으로 판단해라.")
            return

    print("\n④ 읽기검산 (새 커넥션)")
    검산 = _읽기검산(db.DSN, 스냅["golden_set_정답근거_스냅샷"])
    print(f"  검사한 근거쌍 {검산['검사한_근거쌍수']} · 문제 {len(검산['문제'])}건")
    for p in 검산["문제"][:20]:
        print(f"   🔴 {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", action="append", required=True, help="운영 chunk 덤프 JSON (여러 번 가능)")
    ap.add_argument("--commit", action="store_true", help="실제로 커밋한다. 기본은 dry(전부 롤백)")
    a = ap.parse_args()
    실행([Path(p) for p in a.input], commit=a.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
