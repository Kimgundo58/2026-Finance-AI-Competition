# -*- coding: utf-8 -*-
"""0단계 — 부속표가 눕혀진 조를 `_tables.json` 마크다운 표로 갈아끼운다 (중앙 ai-4a).

`scripts/archive/work/_참고3_scoped_reload.py` 의 절차를 그대로 쓰되, **입력을 파일
재분해가 아니라 «조 지정»** 으로 바꾼다. 이유는 `table_splice.오염됐나()` 의 탐침
(`\n[가-힣]\n` — 세로 한 글자 줄)이 이번 대상의 절반을 «정상» 으로 읽기 때문이다.

실측(2026-09-06, DB):
    초격차 참고2   표행 0 · 오염 0   <- 탐침은 정상이라 하는데 본문은 표가 눕혀져 있다
                   ("재료비 · 금속, 보석, 원석 등은…" 처럼 비목명이 정의 «중간» 에 낀다)
    초격차 참고5   표행 0 · 오염 0
    모두의창업 별지1 표행 0 · 오염 0
    예비창업 붙임2 / 초기창업 붙임2 / 모두의창업 별지2  표행 0 · 오염 1
그래서 오염 게이트를 «우회» 하고 아래 대상 조만 명시적으로 갈아끼운다.
`_손실_보강()`(원문 누락 구간 축자 복구)은 그대로 태운다.

🔴 되돌림 기준: 갈아끼운 뒤 그 조에 걸린 골든셋 근거원문의 축자 적중이 «줄면» 그 조는 뺀다.
   (dry-run 에서 미리 재고, `--apply` 뒤에도 다시 잰다 — 사전 예측과 사후 실측은 다르다)

선행:  scratchpad/0단계_L1표추출.py --write    (L1 통합관리지침을 _tables.json 에 추가)
       scratchpad/0단계_표순서교정.py --write  (4분면 조판 문서 표를 읽는 순서로 재정렬)

실행:  PYTHONIOENCODING=utf-8 python scratchpad/0단계_표복구.py            # dry-run
       PYTHONIOENCODING=utf-8 python scratchpad/0단계_표복구.py --apply    # DB 반영
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import table_splice as TS          # noqa: E402
from _lib import db                # noqa: E402

# (doc_id, 조번호) — 계획서 0-2 대상 중 «아직 표가 안 든» 것만.
# 이미 마크다운 표가 든 조(창업중심대학 참고3·5 / 초격차 참고3·6 / 창업도약 별지서식 /
# TIPS 붙임3·5)는 손대지 않는다 — 「더 나은 걸 아는 경우에만 갈아끼운다」.
# 범위는 ai-36 이 좁혔다(2026-09-06): 「창업기업 비목 정의·집행기준·증빙」만.
#   제외 = 주관기관·운영사 해설표(창업중심대학 참고1·2 / 초격차 참고1·4 / 예비창업 붙임1
#          / 초기창업 붙임1) — 코퍼스 편입 기준(창업기업에 «직접» 지급되는가)의 축이 아니다
#   이번 판 «보류»(다음 판) = 모두의창업 별지3·4·5 / TIPS 별지1·별첨1~3
대상 = [
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고2"),
    ("초격차 스타트업 프로젝트 세부관리기준(제10차)", "참고5"),
    ("예비창업패키지 세부관리기준(2025년)", "붙임2"),
    ("초기창업패키지 세부관리기준(2025년)", "붙임2"),
    ("모두의 창업 프로젝트 세부관리기준(개정본)", "별지1"),
    ("모두의 창업 프로젝트 세부관리기준(개정본)", "별지2"),
    ("창업중심대학 세부관리기준2025년 개정", "참고4"),
    ("L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223", "제36조"),
]

RE_오염 = TS.RE_라벨오염


def 납작(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def 표행(본문: str) -> int:
    return sum(1 for ln in (본문 or "").splitlines() if ln.lstrip().startswith("|"))


def 헤더에_비목(본문: str) -> bool:
    """표 머리줄에 「비목」이 있나. 🔴 공백을 무시한다 — 원문 머리는 「비 목」이다
    (초격차 참고2 실측. 문자 그대로 찾으면 «표가 살았는데 false» 가 나온다)."""
    for ln in (본문 or "").splitlines():
        s = ln.strip()
        if s.startswith("|") and "비목" in 납작(s):
            return True
    return False


def 골든적중(cur, 본문: str) -> tuple[int, list[int]]:
    """이 본문 안에 근거원문이 «축자» 로 들어 있는 골든 문항 수(공백무시 대조).

    잣대는 `scratchpad/룰검산.py` 의 `납작()` 과 같다 — 두 곳이 다르면 같은 산출을
    두 잣대로 재게 된다. 20자 미만은 우연 일치가 나오므로 세지 않는다.
    """
    flat = 납작(본문)
    cur.execute("select gold_id, 근거원문 from eval.golden_set "
                "where 근거원문 is not null and 근거원문<>''")
    hit = [g for g, t in cur.fetchall() if len(납작(t)) >= 20 and 납작(t) in flat]
    return len(hit), hit


def 새본문(doc_id: str, 조번호: str, 원문: str) -> str | None:
    표본문 = TS.직렬화(doc_id, 조번호)
    if not 표본문:
        return None
    return TS._손실_보강(원문, 표본문)


def 조사(cur) -> tuple[list[dict], dict[str, dict[str, str]]]:
    보고: list[dict] = []
    바꿀것: dict[str, dict[str, str]] = {}
    for doc_id, 조 in 대상:
        cur.execute("select article_id, 조제목, 본문 from corpus.doc_articles "
                    "where doc_id=%s and 조번호=%s", (doc_id, 조))
        row = cur.fetchone()
        if row is None:
            보고.append({"doc_id": doc_id, "조번호": 조, "상태": "🔴 조 없음"})
            continue
        aid, 제목, 원문 = row
        cur.execute("select count(*) from corpus.chunks where doc_id=%s and 조번호=%s",
                    (doc_id, 조))
        청크전 = cur.fetchone()[0]
        n전, g전 = 골든적중(cur, 원문)

        새 = 새본문(doc_id, 조, 원문)
        if 새 is None:
            보고.append({"doc_id": doc_id, "조번호": 조, "article_id": aid,
                         "상태": "🔴 _tables.json 에 표 없음",
                         "표행_전": 표행(원문), "오염_전": len(RE_오염.findall(원문)),
                         "글자_전": len(원문), "청크_전": 청크전,
                         "골든적중_전": n전, "골든_전": g전})
            continue
        n후, g후 = 골든적중(cur, 새)
        잃음 = sorted(set(g전) - set(g후))
        보고.append({
            "doc_id": doc_id, "조번호": 조, "article_id": aid, "조제목": 제목,
            "상태": "교체가능" if not 잃음 else "🔴 골든적중 감소 — 보류",
            "표행_전": 표행(원문), "표행_후": 표행(새),
            "오염_전": len(RE_오염.findall(원문)), "오염_후": len(RE_오염.findall(새)),
            "글자_전": len(원문), "글자_후": len(새),
            "헤더에_비목": 헤더에_비목(새),
            "청크_전": 청크전,
            "골든적중_전": n전, "골든적중_후": n후, "잃은_gold_id": 잃음,
        })
        if not 잃음:
            바꿀것.setdefault(doc_id, {})[조] = 새
    return 보고, 바꿀것


def 출력(보고: list[dict]) -> None:
    for r in 보고:
        print("{:<36} {:<8} {:<24} 표행 {}->{}  글자 {}->{}  골든 {}->{}  비목헤더 {}  청크 {}->{}".format(
            r["doc_id"][:34], r["조번호"], r["상태"],
            r.get("표행_전", "-"), r.get("표행_후", "-"),
            r.get("글자_전", "-"), r.get("글자_후", "-"),
            r.get("골든적중_전", "-"), r.get("골든적중_후", "-"),
            r.get("헤더에_비목", "-"),
            r.get("청크_전", "-"), r.get("청크_후", "-")))


# ────────────────────────────────────────────────────────────────────────────
# --apply : doc_articles UPDATE -> 그 조의 chunks DELETE/INSERT -> KURE-v1 임베딩
#           절차·게이트는 `_참고3_scoped_reload.py` 와 «같다». 대상 선정만 다르다.
# ────────────────────────────────────────────────────────────────────────────
def 반영(conn, 바꿀것: dict[str, dict[str, str]]) -> dict:
    sys.path.insert(0, str(ROOT / "scripts" / "archive" / "indexing"))
    sys.path.insert(0, str(ROOT / "scripts" / "archive" / "eval"))
    # 🔴 stage2_chunk 는 import 시점에 sys.stdout 을 TextIOWrapper 로 갈아끼운다.
    #    두 번 겹치면 앞 래퍼가 GC 되며 «진짜 stdout» 을 닫는다 (0단계_표순서교정.py 실측).
    #    import 동안 만들어지는 래퍼를 붙잡아 두어 GC 를 막는다.
    보관: list = []
    원래_TIW = io.TextIOWrapper

    class 붙잡는_TIW(원래_TIW):                     # noqa: N801
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            보관.append(self)

    진짜 = sys.stdout
    io.TextIOWrapper = 붙잡는_TIW
    try:
        import stage2_chunk as s2                   # noqa: PLC0415
        import index_guard                          # noqa: PLC0415
    finally:
        io.TextIOWrapper = 원래_TIW
        sys.stdout = 진짜
    from scope import 범위밖_조                      # noqa: PLC0415

    총 = sum(len(v) for v in 바꿀것.values())

    # ── 1. doc_articles UPDATE ──────────────────────────────────────────
    with conn.cursor() as cur:
        for doc_id, 조들 in 바꿀것.items():
            for 조, 새 in 조들.items():
                cur.execute("UPDATE corpus.doc_articles SET 본문=%s "
                            "WHERE doc_id=%s AND 조번호=%s", (새, doc_id, 조))
                if cur.rowcount != 1:
                    sys.exit(f"🔴 UPDATE 행수 이상 {doc_id}/{조}: {cur.rowcount}")
    conn.commit()
    print(f"doc_articles UPDATE 완료 ({총}건)")

    # ── 2. 재청킹 ────────────────────────────────────────────────────────
    태그 = s2.TAT.태그맵(json.loads(s2.APPLY.read_text(encoding="utf-8"))["tags"])
    from transformers import AutoTokenizer          # noqa: PLC0415
    print(f"토크나이저 로딩 {s2.MODEL} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(s2.MODEL)

    new_rows: list[tuple] = []
    embed_inputs: list[str] = []
    건너뜀: list[str] = []
    with conn.cursor() as cur:
        for doc_id in 바꿀것:
            cur.execute("SELECT layer, 기관ID, parse_quality, version, status, "
                        "retrieval_scope, src_path FROM corpus.documents WHERE doc_id=%s",
                        (doc_id,))
            drow = cur.fetchone()
            if drow is None:
                sys.exit(f"🔴 corpus.documents 에 {doc_id} 가 없다.")
            layer, 기관, pq, ver, status, scope, src = drow
            # 🔴 CLAUDE.md — 「새 인덱싱 경로는 반드시 index_guard 를 태운다」
            index_guard.assert_indexable(src or doc_id, layer)

            cur.execute("SELECT article_id, 조번호, 조제목, 본문, 페이지, 삭제 "
                        "FROM corpus.doc_articles WHERE doc_id=%s ORDER BY article_id",
                        (doc_id,))
            arts = [dict(zip(("article_id", "조번호", "조제목", "본문", "페이지", "삭제"), r))
                    for r in cur.fetchall()]
            범위밖 = 범위밖_조(doc_id, arts)
            장 = s2.장맵(arts)
            사업 = s2.사업_of_doc.get(doc_id)

            for art in arts:
                조번호 = art["조번호"]
                if 조번호 not in 바꿀것[doc_id]:
                    continue
                if art["삭제"]:
                    건너뜀.append(f"{doc_id}/{조번호} 삭제조"); continue
                if s2.RE_첨부.match(조번호 or "") and s2.표인가(art["본문"]):
                    건너뜀.append(f"{doc_id}/{조번호} 박스표"); continue
                if 조번호 in 범위밖:
                    건너뜀.append(f"{doc_id}/{조번호} 범위밖"); continue
                if not (art["본문"] or "").strip():
                    건너뜀.append(f"{doc_id}/{조번호} 본문없음"); continue
                적용 = s2.TAT.적용대상_of(doc_id, 조번호, 태그)
                if 적용 is None:
                    건너뜀.append(f"{doc_id}/{조번호} 적용대상 미결"); continue

                부속 = bool(s2.RE_첨부.match(조번호 or ""))
                조각 = s2.병합(s2.분할(tok, art["본문"]))
                h = s2.헤더(layer, 사업, doc_id, 장.get(조번호, ""), 조번호, art["조제목"])
                for 항호, txt in 조각:
                    new_rows.append((doc_id, art["article_id"], layer, 기관, pq, ver, status,
                                     "폐포전용" if 부속 else scope,
                                     조번호, art["조제목"], 항호, art["페이지"],
                                     사업, 적용, txt))
                    embed_inputs.append(f"{h}\n{txt}")

    for s in 건너뜀:
        print(f"  건너뜀 — {s}")

    길이 = s2.토큰수(tok, embed_inputs) if embed_inputs else []
    초과 = [i for i, n in enumerate(길이) if n > s2.GATE_TOK]
    if 초과:
        for i in 초과[:5]:
            print(f"    {길이[i]}토큰  {new_rows[i][0][:40]} {new_rows[i][8]} {new_rows[i][10]}")
        sys.exit("🔴 게이트 실패 — 초과 청크가 있다. 임베딩하면 꼬리가 잘린다.")
    print(f"게이트 통과 — 새 청크 {len(new_rows)}건, 최대 {max(길이) if 길이 else 0}토큰")

    # ── 3. DELETE 옛 청크 + INSERT 새 청크 ───────────────────────────────
    # 🔴 여기서 한 번 죽었다 (2026-09-06 실측).
    #    `eval.golden_chunks.chunk_id` 는 chunks 를 ON DELETE SET NULL 로 참조하는데
    #    같은 표의 CHECK(golden_chunks_실패_check) 가 «매칭방법<>'실패' 면 chunk_id NOT NULL»
    #    을 건다 -> 핀이 걸린 청크를 지우면 CheckViolation 으로 트랜잭션이 통째로 죽는다.
    #    (doc_articles 는 이미 커밋된 뒤라 «본문만 새것, 청크는 옛것» 인 반쪽 상태가 된다)
    #    처방: 지울 청크에 걸린 핀을 «먼저» 걷어내고, 재청킹 뒤 그 (gold_id, 근거순번)
    #    만 다시 고정한다. `--재고정` 은 안 쓴다 — 그건 golden_chunks 를 통째로 지운다.
    조건0 = " OR ".join(["(c.doc_id=%s AND c.조번호=%s)"] * 총)
    파라0: list = []
    for doc_id, 조들 in 바꿀것.items():
        for 조 in 조들:
            파라0 += [doc_id, 조]
    with conn.cursor() as cur:
        cur.execute(f"""SELECT gc.gc_id, gc.gold_id, gc.근거순번, gc.doc_id, gc.조번호, gc.매칭방법
                          FROM eval.golden_chunks gc
                          JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id
                         WHERE {조건0}""", 파라0)
        핀백업 = [dict(zip(("gc_id", "gold_id", "근거순번", "doc_id", "조번호", "매칭방법"), r))
                  for r in cur.fetchall()]
    재고정쌍 = sorted({(p["gold_id"], p["근거순번"]) for p in 핀백업})
    (ROOT / "scratchpad" / "0단계_핀백업.json").write_text(
        json.dumps(핀백업, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"golden_chunks 영향 핀 {len(핀백업)}행 / (gold_id,근거순번) {len(재고정쌍)}쌍 "
          f"-> scratchpad/0단계_핀백업.json")

    삭제수: dict[str, int] = {}
    with conn.cursor() as cur:
        if 핀백업:
            cur.execute("DELETE FROM eval.golden_chunks WHERE gc_id = ANY(%s)",
                        ([p["gc_id"] for p in 핀백업],))
            print(f"golden_chunks DELETE {cur.rowcount}행 (재청킹 후 다시 고정한다)")
        for doc_id, 조들 in 바꿀것.items():
            for 조 in 조들:
                cur.execute("DELETE FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s",
                            (doc_id, 조))
                삭제수[f"{doc_id}|{조}"] = cur.rowcount
        if new_rows:
            cur.executemany("""
                INSERT INTO corpus.chunks
                  (doc_id, article_id, layer, 기관ID, parse_quality, version, status,
                   retrieval_scope, 조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, new_rows)
    conn.commit()
    print(f"corpus.chunks  DELETE {sum(삭제수.values())}건 -> INSERT {len(new_rows)}건")

    # ── 4. 로컬 CPU 임베딩 ───────────────────────────────────────────────
    if not new_rows:
        print("새 청크가 없다 — 임베딩 단계 건너뜀.")
        return {"삭제수": 삭제수, "삽입수": 0, "임베딩": 0}

    조건, 파라미터 = [], []
    for doc_id, 조들 in 바꿀것.items():
        for 조 in 조들:
            조건.append("(doc_id=%s AND 조번호=%s)")
            파라미터 += [doc_id, 조]
    with conn.cursor() as cur:
        cur.execute(f"SELECT chunk_id, doc_id, 조번호, 항호 FROM corpus.chunks "
                    f"WHERE {' OR '.join(조건)} ORDER BY chunk_id", 파라미터)
        재조회 = cur.fetchall()

    입력맵 = {}
    for row, txt in zip(new_rows, embed_inputs):
        입력맵[(row[0], row[8], row[10])] = txt
    ordered_ids, ordered_texts = [], []
    for cid, doc_id, 조번호, 항호 in 재조회:
        key = (doc_id, 조번호, 항호)
        if key not in 입력맵:
            sys.exit(f"🔴 임베딩 입력 매칭 실패: {key}")
        ordered_ids.append(cid)
        ordered_texts.append(입력맵[key])

    print(f"임베딩 대상 {len(ordered_texts)}건 — KURE-v1 CPU 로딩 중...", flush=True)
    from sentence_transformers import SentenceTransformer   # noqa: PLC0415
    model = SentenceTransformer(s2.MODEL, device="cpu")
    model.max_seq_length = 1024
    t0 = time.time()
    vecs = model.encode(ordered_texts, batch_size=16, normalize_embeddings=True,
                        show_progress_bar=False, convert_to_numpy=True)
    print(f"임베딩 완료 {time.time()-t0:.1f}초  shape={vecs.shape}")

    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _emb (chunk_id BIGINT PRIMARY KEY, v TEXT);")
        with cur.copy("COPY _emb (chunk_id, v) FROM STDIN") as cp:
            for cid, v in zip(ordered_ids, vecs):
                cp.write_row((cid, "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
        cur.execute("""UPDATE corpus.chunks c SET embedding = _emb.v::extensions.vector(1024)
                        FROM _emb WHERE _emb.chunk_id = c.chunk_id""")
        갱신 = cur.rowcount
    conn.commit()
    print(f"embedding UPDATE {갱신}건")

    # 🔴 임베딩이 NULL 로 남은 청크가 있으면 검색에서 조용히 빠진다 — 여기서 잡는다
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM corpus.chunks "
                    f"WHERE ({' OR '.join(조건)}) AND embedding IS NULL", 파라미터)
        널 = cur.fetchone()[0]
    if 널:
        sys.exit(f"🔴 embedding NULL 이 {널}건 남았다.")

    # ── 5. 걷어냈던 골든 핀을 «그 (gold_id, 근거순번) 만» 다시 고정한다 ───
    #    사다리(원문일치 -> 조번호 -> 조제목 -> 실패)는 pin_golden_chunks.매칭() 을
    #    그대로 쓴다. 여기서 따로 짜면 두 곳이 어긋난다.
    import pin_golden_chunks as PIN                  # noqa: PLC0415
    재고정통계 = {"원문일치": 0, "조번호": 0, "조제목": 0, "실패": 0}
    쓴행 = 0
    with conn.cursor() as cur:
        for gid, 순번 in 재고정쌍:
            cur.execute("SELECT 정답근거 FROM eval.golden_set WHERE gold_id=%s", (gid,))
            근거 = (cur.fetchone() or [None])[0] or []
            if 순번 >= len(근거):
                재고정통계["실패"] += 1
                cur.execute("INSERT INTO eval.golden_chunks "
                            "(gold_id, 근거순번, 매칭방법, 실패사유) VALUES (%s,%s,'실패',%s) "
                            "ON CONFLICT DO NOTHING",
                            (gid, 순번, f"근거순번 {순번} 이 정답근거 배열 밖 (재고정 시점)"))
                쓴행 += cur.rowcount
                continue
            g = 근거[순번]
            방법, ids, 사유 = PIN.매칭(cur, g.get("doc"), g.get("조번호"), g.get("원문"))
            재고정통계[방법] += 1
            if 방법 == "실패":
                cur.execute("INSERT INTO eval.golden_chunks "
                            "(gold_id, 근거순번, doc_id, 조번호, 매칭방법, 실패사유) "
                            "VALUES (%s,%s,%s,%s,'실패',%s) ON CONFLICT DO NOTHING",
                            (gid, 순번, g.get("doc"), g.get("조번호"), 사유))
                쓴행 += cur.rowcount
                continue
            for cid in ids:
                cur.execute("INSERT INTO eval.golden_chunks "
                            "(gold_id, 근거순번, chunk_id, article_id, doc_id, 조번호, 매칭방법) "
                            "SELECT %s, %s, c.chunk_id, c.article_id, c.doc_id, %s, %s "
                            "  FROM corpus.chunks c WHERE c.chunk_id = %s "
                            "ON CONFLICT DO NOTHING",
                            (gid, 순번, g.get("조번호"), 방법, cid))
                쓴행 += cur.rowcount
    conn.commit()
    print(f"golden_chunks 재고정 — {len(재고정쌍)}쌍 -> {쓴행}행  {재고정통계}")
    return {"삭제수": 삭제수, "삽입수": len(new_rows), "임베딩": 갱신,
            "핀_걷음": len(핀백업), "핀_재고정쌍": len(재고정쌍),
            "핀_재고정행": 쓴행, "핀_재고정통계": 재고정통계}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    out = ROOT / "scratchpad" / "0단계_검산.json"
    with db.connect() as conn, conn.cursor() as cur:
        보고, 바꿀것 = 조사(cur)
    출력(보고)
    print(f"\n교체 대상 {sum(len(v) for v in 바꿀것.values())}조 / {len(대상)}조")
    if not a.apply:
        out.write_text(json.dumps({"시점": "사전", "대상": 보고}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"-> {out}\ndry-run — DB 를 쓰지 않았다.")
        return 0

    with db.connect() as conn:
        결과 = 반영(conn, 바꿀것)
        # ── 사후 실측 — 사전 예측과 «따로» 잰다 ────────────────────────
        with conn.cursor() as cur:
            사후 = []
            for doc_id, 조 in 대상:
                cur.execute("select article_id, 본문 from corpus.doc_articles "
                            "where doc_id=%s and 조번호=%s", (doc_id, 조))
                r = cur.fetchone()
                if r is None:
                    continue
                aid, 본 = r
                cur.execute("select count(*) from corpus.chunks where doc_id=%s and 조번호=%s",
                            (doc_id, 조))
                청크후 = cur.fetchone()[0]
                n, g = 골든적중(cur, 본)
                사후.append({"doc_id": doc_id, "조번호": 조, "article_id": aid,
                             "표행_후_실측": 표행(본), "글자_후_실측": len(본),
                             "헤더에_비목_실측": 헤더에_비목(본),
                             "청크_후_실측": 청크후, "골든적중_후_실측": n, "골든_후": g})
    for r in 사후:
        print("{:<36} {:<8} 표행 {:3d}  청크 {:2d}  골든 {:2d}  비목헤더 {}".format(
            r["doc_id"][:34], r["조번호"], r["표행_후_실측"], r["청크_후_실측"],
            r["골든적중_후_실측"], r["헤더에_비목_실측"]))
    print(f"사후 합계 — 표행 {sum(r['표행_후_실측'] for r in 사후)} · "
          f"청크 {sum(r['청크_후_실측'] for r in 사후)} · "
          f"골든 {sum(r['골든적중_후_실측'] for r in 사후)}")
    out.write_text(json.dumps({"시점": "사전+사후", "대상": 보고, "반영": 결과, "사후": 사후},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
