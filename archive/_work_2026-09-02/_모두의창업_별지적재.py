# -*- coding: utf-8 -*-
"""모두의 창업 세부관리기준 — 별지 제①②⑤호를 `doc_articles`+`chunks` 에 넣는다.

왜 필요한가
───────────
이 문서의 부속물은 **doc_articles 에 한 건도 없다.** 본칙 제52조에서 끊겨 있다.
원인은 결손이 아니라 **섹션 분리기가 동그라미 번호를 못 잡는 것**이다:

    RE_첨부.match('별지1')        -> True
    RE_첨부.match('[별지 제①호]')  -> False        ← 여기서 통째로 빠졌다

그 결과 「일반·기술트랙 진출자 사업비 집행기준」(비목별 정의·증빙·유의사항)이
인덱스 밖이다. 이건 정확히 우리가 판정하는 물건이다.

🔴 무엇을 넣고 무엇을 빼는가 — 이 문서는 트랙이 섞여 있다
──────────────────────────────────────────────────────────
    별지①  일반·기술트랙 **주관기관** 사업비 집행기준   → 넣되 적용대상='주관기관'
    별지②  일반·기술트랙 **진출자**  사업비 집행기준   → 넣는다. 판정 재료 본체
    별지③  **로컬트랙** 운영기관 집행기준              → 🔴 넣지 않는다
    별지④  **로컬트랙** 진출자 등 집행기준             → 🔴 넣지 않는다
    별지⑤  공급기업 위반사항 조치 기준                 → 넣는다 (제32조가 인용)

③④ 는 제3편 로컬트랙이고 CLAUDE.md 가 범위 밖으로 못박았다 —
「제3편을 남겨두면 일반·기술트랙 판정에 범위 밖 룰이 딸려온다」.
골든셋 A8·A9 의 근거가 별지④에만 있어 범위밖 처분된 것도 같은 이유다.

🔴 적용대상을 폴백에 맡기지 않는다
──────────────────────────────────
`tag_apply_target.적용대상_of()` 는 태그에 없는 부속물에 **'공통'** 을 준다
(`tag_apply_target.py:193`). 그런데 별지① 은 **주관기관의** 집행기준이다 —
'공통' 으로 두면 `retrieve.py` 의 `적용대상 IN ('창업기업','공통')` 을 통과해
주관기관 예산 규칙이 창업기업 판정에 섞인다. 그래서 여기서 명시적으로 준다.

    별지1 -> 주관기관   (판정 검색에서 제외된다. 의도한 것이다)
    별지2 -> 창업기업
    별지5 -> 공통       (공급기업 제재. 제32조에서 폐포로 내려온다)

retrieval_scope
───────────────
부속물 기본값은 `폐포전용` 이다 (`stage2_chunk.py:291` 주석의 실측 — 붙임 청크가
top-5 의 49.1% 를 먹는데 정답은 1건이었다). 그 규약을 지킨다. 예외는 별지2 뿐이다:

    🔴 **제2편(일반·기술트랙) 본문은 별지①②를 한 번도 인용하지 않는다.**
       실측했다 — 제2편 구간의 '별지' 언급 3건이 전부 별지 제5호다.
       인입 엣지가 없으므로 폐포전용으로 넣으면 **영원히 도달 불가**다.
       판정 재료로 쓰려면 진입점이어야 한다. 별지5 는 제32조 엣지가 이미
       `resolved` 로 서 있어 폐포전용으로 충분하다.

임베딩은 CPU. 청크 수십 건이라 팟을 열 이유가 없다 (GPU 금지).
🔴 모델·정규화가 본 파이프라인과 같아야 한다: KURE-v1 · normalize_embeddings=True.

실행:  PYTHONIOENCODING=utf-8 python scripts/_work/_모두의창업_별지적재.py
       PYTHONIOENCODING=utf-8 python scripts/_work/_모두의창업_별지적재.py --commit
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 🔴 stage2_* 는 각자 모듈 최상단에서 sys.stdout 을 래핑한다. 둘 이상 import 하면
#    밀려난 래퍼가 GC 될 때 fd 1 을 닫아 이후 print 가 전부 죽는다.
#    래퍼를 살려 두고 원래 stdout 으로 되돌린다 (_재도전2026_인덱스교체.py 와 같은 처리).
_원래_stdout = sys.stdout
_래퍼_보관: list = []

import psycopg                                                    # noqa: E402
import index_guard                                                # noqa: E402
import stage2_chunk as SC                                         # noqa: E402
_래퍼_보관.append(sys.stdout)
import stage2_bm25 as BM                                          # noqa: E402
_래퍼_보관.append(sys.stdout)
import stage2_embed as SE                                         # noqa: E402
_래퍼_보관.append(sys.stdout)
from pdftext import extract                                       # noqa: E402
sys.stdout = _원래_stdout

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
DOC = "모두의 창업 프로젝트 세부관리기준(개정본)"
PDF = (ROOT / "2026_Finance_DATA_FOR_RAG" / "창진원" / "모두의 창업 (일반-기술)"
       / "모두의 창업 프로젝트 세부관리기준(개정본).pdf")

RE_별지 = re.compile(r"\[별지 제([①②③④⑤])호\]")
_동그라미 = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5}

# (조번호, 적용대상, retrieval_scope). 여기 없는 번호는 넣지 않는다 — 로컬트랙 배제가
# 기본값이 되게 **화이트리스트**로 쓴다. 블랙리스트면 새 별지가 조용히 딸려 들어온다.
채택 = {
    1: ("별지1", "주관기관", "폐포전용"),
    2: ("별지2", "창업기업", "진입점"),
    5: ("별지5", "공통",     "폐포전용"),
}
RE_쪽 = re.compile(r"-\s*(\d+)\s*-")


def 조각내기() -> list[dict]:
    """PDF 에서 별지 5개를 잘라 채택분만 돌려준다."""
    본문, _ = extract(str(PDF))
    표시 = [(m.start(), _동그라미[m.group(1)]) for m in RE_별지.finditer(본문)]
    if len(표시) != 5:
        print(f"🔴 별지 헤더가 5개가 아니다 ({len(표시)}개) — PDF 가 바뀌었다. 중단한다.")
        sys.exit(1)
    경계 = [p for p, _ in 표시] + [len(본문)]

    out = []
    for i, (시작, 번호) in enumerate(표시):
        덩이 = 본문[시작:경계[i + 1]].strip()
        제목 = 덩이.split("\n", 1)[0].replace(f"[별지 제{'①②③④⑤'[번호-1]}호]", "").strip()
        if 번호 not in 채택:
            print(f"  건너뜀  별지{번호}  {제목[:52]}  (로컬트랙 — 범위 밖)")
            continue
        조번호, 적용, scope = 채택[번호]
        앞 = 본문[:시작]
        쪽 = int(RE_쪽.findall(앞)[-1]) if RE_쪽.findall(앞) else None
        out.append({"조번호": 조번호, "조제목": 제목, "본문": 덩이,
                    "페이지": 쪽, "적용대상": 적용, "scope": scope})
        print(f"  채택    {조번호}  {제목[:52]}  {len(덩이):,}자 · p{쪽} · "
              f"적용대상={적용} · {scope}")
    return out


def main() -> int:
    commit = "--commit" in sys.argv
    t0 = time.time()

    캐시 = Path.home() / ".cache/huggingface/hub/models--nlpai-lab--KURE-v1"
    if 캐시.exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    print(f"PDF 조각내기 — {PDF.name}")
    조각 = 조각내기()
    if not 조각:
        print("🔴 채택된 별지가 없다."); return 1

    from transformers import AutoTokenizer
    print(f"\n토크나이저 {SC.MODEL} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(SC.MODEL)

    with psycopg.connect(DSN) as conn:
        row = conn.execute("""SELECT layer, 기관ID, parse_quality, version, status,
                                     retrieval_scope, src_path, index_target
                                FROM corpus.documents WHERE doc_id=%s""", (DOC,)).fetchone()
        if not row:
            print(f"🔴 {DOC!r} 이 documents 에 없다."); return 1
        layer, 기관, pq, ver, status, _scope, src, idx = row
        if status != "active" or not idx:
            print(f"🔴 status={status!r} index_target={idx} — active·True 여야 한다."); return 1
        index_guard.assert_indexable(src or DOC, layer)

        # 🔴 이미 들어가 있으면 멈춘다. 두 번 돌려 중복 청크를 만들지 않는다.
        기존 = conn.execute("""SELECT 조번호, count(*) FROM corpus.chunks
                                WHERE doc_id=%s AND 조번호 = ANY(%s) GROUP BY 1""",
                           (DOC, [c["조번호"] for c in 조각])).fetchall()
        if 기존:
            print(f"🔴 이미 적재돼 있다: {기존}. 다시 넣으면 중복이다. 중단한다."); return 1

        사업 = SC.사업_of_doc.get(DOC)
        if not 사업:
            print(f"🔴 stage2_chunk.사업_of_doc 에 {DOC!r} 이 없다."); return 1

        # ── 청크 조립 — stage2_chunk 와 같은 코드 ────────────────────────
        rows, 임베딩입력 = [], []
        for a in 조각:
            조각들 = SC.병합(SC.분할(tok, a["본문"]))
            h = SC.헤더(layer, 사업, DOC, "", a["조번호"], a["조제목"])
            for 항호, txt in 조각들:
                rows.append({"조번호": a["조번호"], "조제목": a["조제목"],
                             "항호": 항호, "페이지": a["페이지"],
                             "적용대상": a["적용대상"], "scope": a["scope"], "text": txt})
                임베딩입력.append(f"{h}\n{txt}")

        길이 = SC.토큰수(tok, 임베딩입력)
        초과 = [i for i, n in enumerate(길이) if n > SC.GATE_TOK]
        print(f"\n청크 {len(rows)}건 · 토큰 최대 {max(길이)} 평균 {sum(길이)/len(길이):.0f} "
              f"초과 {len(초과)}")
        for k, v in sorted(Counter((r['조번호'], r['scope'], r['적용대상'])
                                   for r in rows).items()):
            print(f"    {k[0]:<6} {k[1]:<8} {k[2]:<6} {v}청크")
        if 초과:
            for i in 초과[:5]:
                print(f"  🔴 {길이[i]}토큰 {rows[i]['조번호']} {rows[i]['항호']}")
            print("🔴 게이트 실패 — 임베딩하면 꼬리가 잘린다."); return 1

        if not commit:
            print(f"\n(미리보기다. 실제로 쓰려면 --commit)  {time.time()-t0:.0f}초")
            return 0

        print("\n임베딩 (CPU) ...", flush=True)
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(SC.MODEL, device="cpu")
        m.max_seq_length = SE.MAX_SEQ
        vecs = m.encode(임베딩입력, batch_size=8, normalize_embeddings=True,
                        show_progress_bar=False, convert_to_numpy=True)
        print(f"  {vecs.shape} · {time.time()-t0:.0f}초")

        # ── 한 트랜잭션 ─────────────────────────────────────────────────
        with conn.cursor() as cur:
            aid: dict[str, int] = {}
            for a in 조각:
                cur.execute("""INSERT INTO corpus.doc_articles
                                 (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제)
                               VALUES (%s,%s,%s,NULL,%s,%s,FALSE)
                               RETURNING article_id""",
                            (DOC, a["조번호"], a["조제목"], a["본문"], a["페이지"]))
                aid[a["조번호"]] = cur.fetchone()[0]
            for r, v in zip(rows, vecs):
                cur.execute("""
                    INSERT INTO corpus.chunks
                      (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                       retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상,
                       text, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (DOC, aid[r["조번호"]], layer, 기관, "high", ver, status,
                     r["scope"], r["조번호"], r["조제목"], r["항호"], r["페이지"],
                     사업, r["적용대상"], r["text"],
                     "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
        conn.commit()

        print("\nBM25 재적재 ...", flush=True)
        전체 = [{"chunk_id": c, "text": t} for c, t in conn.execute(
            "SELECT chunk_id, text FROM corpus.chunks ORDER BY chunk_id").fetchall()]
        BM.적재(전체)

        print("\n확인:")
        for q in ("SELECT count(*) FROM corpus.chunks",
                  "SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL",
                  f"SELECT count(*) FROM corpus.chunks WHERE doc_id='{DOC}'"):
            print(f"  {conn.execute(q).fetchone()[0]:>8,}  {q}")
        for r in conn.execute("""SELECT 조번호, retrieval_scope, 적용대상, count(*)
                                   FROM corpus.chunks WHERE doc_id=%s AND 조번호 ~ '^별지'
                                  GROUP BY 1,2,3 ORDER BY 1""", (DOC,)).fetchall():
            print(f"    {r[0]:<6} {r[1]:<8} {r[2]:<6} {r[3]}청크")

    print(f"\n완료 — {time.time()-t0:.0f}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
