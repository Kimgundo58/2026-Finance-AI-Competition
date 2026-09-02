# -*- coding: utf-8 -*-
"""검색 품질 평가 — dense / BM25 / RRF 가 정답 조문을 실제로 찾아오는가.

🔴 **A·B 가 잰 것과 다르다.**
   A = 전량 스캔 대비 재현율   -> "임베딩 모델이 가깝다고 본 것"을 얼마나 재현하나
   B = 우리 SQL == bm25s       -> 구현 동등성
   둘 다 기계가 제대로 도는지를 봤을 뿐, **맞는 조문을 찾는지는 안 봤다.**
   여기가 그걸 잰다.

정답은 `eval.golden_set.정답근거` (jsonb: doc / 조번호 / 원문).
`doc` 이 `corpus.chunks.doc_id` 와 그대로 일치하고, `원문` 은 조문에서 그대로 따온
문장이라 **원문 부분일치로 정답 청크를 역추적**한다. 조번호는 항호까지 붙어 있어
("제20조(1)") 청크 단위와 어긋날 수 있으므로 보조 수단으로만 쓴다.

🔴 **검색 구현은 여기에 없다 — `scripts/retrieve.py` 를 부른다** (2026-08-31 분리).
   평가와 실전이 다른 코드를 쓰면 여기서 잰 숫자가 실전을 설명하지 못한다.
   분리 시점의 기준값(정답셋 70문항): **RRF hit@5 = 52.9%** · dense 47.1% · BM25 40.0%.
   이 값이 한 자리라도 바뀌면 검색 동작이 바뀐 것이다.

지표
    hit@k   정답 청크가 상위 k 안에 하나라도 있는가 (판정에는 이게 1순위 —
            근거를 못 가져오면 LLM 이 무엇을 해도 틀린다)
    MRR     정답이 처음 나온 순위의 역수 평균
    RRF     dense 와 BM25 를 합쳤을 때 (Agent.md (3)-e)

실행:
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --k 20
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --overlap      # C6 BM25 vs dense 겹침
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --c7           # C7 사업필터 on/off 짝비교
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --사업필터      # 사업필터 켠 채로 전체 재측정
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --dangling     # DANGLING_WARN 발화 여부
    PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --폐포          # 참조 확장 포함 커버리지
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import psycopg

# 🔴 sys.stdout 을 여기서 감싸지 않는다. stage2_bm25 를 import 하면 그쪽이 다시 감싸고,
#    이쪽 래퍼가 GC 되면서 밑의 버퍼를 닫아버린다 ("I/O operation on closed file").
#    출력 인코딩은 PYTHONIOENCODING=utf-8 로 준다 (훅이 강제한다).

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
import retrieve  # noqa: E402  검색 구현 기준 문서

DSN = db.DSN


def 정규화(s: str) -> str:
    """공백·괄호·문장부호 차이를 흡수한다. 원문 인용이 완전히 같지 않을 수 있다."""
    return re.sub(r"[\s　·,.\"'()（）「」『』]", "", s or "")


def 정답청크(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> chunk_id 집합. 원문 부분일치가 1차, 조번호가 2차."""
    found: set[int] = set()
    for g in 근거 or []:
        doc, 조, 원문 = g.get("doc"), g.get("조번호") or "", g.get("원문") or ""
        핵심 = 정규화(원문)[:40]          # 앞 40자면 조문을 특정하기 충분하다
        if doc and 핵심:
            cur.execute("""SELECT chunk_id FROM corpus.chunks
                            WHERE doc_id = %s
                              AND regexp_replace(text, '[\\s·,."''()（）「」『』]', '', 'g')
                                  LIKE '%%' || %s || '%%'""", (doc, 핵심))
            found |= {r[0] for r in cur.fetchall()}
        if not found and doc and 조:
            조번호 = re.match(r"(제\d+조(?:의\d+)?)", 조)
            if 조번호:
                cur.execute("SELECT chunk_id FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s",
                            (doc, 조번호.group(1)))
                found |= {r[0] for r in cur.fetchall()}
    return found


def 정답청크_고정(cur, gold_id: int) -> set[int]:
    """D3 `eval.golden_chunks` 고정 매핑. 매 실행 원문 부분일치로 되짚지 않는다.

    🔴 위의 `정답청크()` 는 **부속물 좌표를 통째로 놓치고 있었다** — `[붙임2]`·`참고2`·
       `별표1`·`별지4` 를 `제N조` 정규식이 못 잡는다. 그래서 평가 가능 문항이 70 이었고
       고정 매핑으로는 **74** 다. 늘어난 4문항은 난이도가 다르므로
       🔴 **70문항 시절의 hit율과 직접 비교하면 안 된다** (계약 §7).
       `--gold-chunks` 는 그래서 74문항 지표와 **70문항 부분집합 지표를 같이** 찍는다.
    """
    cur.execute("""SELECT chunk_id FROM eval.golden_chunks
                    WHERE gold_id = %s AND chunk_id IS NOT NULL""", (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 정답조(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> `doc_articles.article_id` 집합. 참조 확장은 조 단위라 청크로는 못 잰다."""
    out: set[int] = set()
    for g in 근거 or []:
        조 = re.match(r"(제\d+조(?:의\d+)?)", g.get("조번호") or "")
        if not (g.get("doc") and 조):
            continue
        cur.execute("SELECT article_id FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
                    (g["doc"], 조.group(1)))
        out |= {r[0] for r in cur.fetchall()}
    return out


# ── 미스 원인 분해 ──────────────────────────────────────────────────────────
# hit@5 를 놓친 문항을 **네 갈래**로 가른다. 이게 갈리지 않으면 내일 무엇을 고칠지
# 또 추측하게 된다 (계약 §1). "검색이 나쁘다" 는 진단이 아니다.
#
#   필터밖    정답 청크가 pre-filter 를 통과하지 못한다 (status·scope·적용대상·사업명)
#             -> 검색기를 아무리 고쳐도 안 잡힌다. 태깅·적재 쪽 문제다
#   결손      정답 근거에 해당하는 청크 자체가 없다 (부속물 미청킹 등)
#             -> 규정 모음 문제. 청킹·룰 테이블 쪽
#   후보밖    필터는 통과하는데 dense 전량 순위가 후보K 밖이다
#             -> 임베딩/질의 표현 문제. 리랭커로도 못 구한다
#   랭킹      후보 안에는 있는데 top-5 밖이다
#             -> 결합·가중 문제. 여기만이 검색기 튜닝으로 줄어드는 구간이다
_적격 = f"""SELECT chunk_id FROM corpus.chunks
             WHERE chunk_id = ANY(%(g)s) AND {retrieve.FILTER}"""

_순위 = f"""SELECT count(*) + 1 FROM corpus.chunks
             WHERE {retrieve.FILTER}
               AND (embedding <=> %(v)s::extensions.vector(1024))
                 < (SELECT embedding <=> %(v)s::extensions.vector(1024)
                      FROM corpus.chunks WHERE chunk_id = %(c)s)"""


def 진단하기(cur, 데이터, r_res, 벡터들, a, 미해결) -> None:
    from collections import Counter
    갈래 = Counter()
    사례: dict[str, list] = {}
    for (gid, 세트, q, 정답, 사업, _근거), (순위, _), vec in zip(데이터, r_res, 벡터들):
        if any(c in 정답 for c in 순위[:5]):
            갈래["적중"] += 1
            continue
        if not 정답:
            갈래["결손"] += 1
            사례.setdefault("결손", []).append((gid, 세트, 사업))
            continue
        cur.execute(_적격, {"g": list(정답)})
        적격 = [r[0] for r in cur.fetchall()]
        if not 적격:
            갈래["필터밖"] += 1
            사례.setdefault("필터밖", []).append((gid, 세트, 사업))
            continue
        최선 = min((cur.execute(_순위, {"v": vec, "c": c}).fetchone()[0]) for c in 적격)
        키 = "랭킹" if 최선 <= a.k else "후보밖"
        갈래[키] += 1
        사례.setdefault(키, []).append((gid, 세트, 사업, 최선))
    n = sum(갈래.values())
    print(f"\n[진단] hit@5 미스 원인 분해 (n={n} · 사업필터={'켬' if a.사업필터 else '끔'})")
    for k in ("적중", "랭킹", "후보밖", "필터밖", "결손"):
        if 갈래[k]:
            print(f"  {k:6} {갈래[k]:3}건 ({갈래[k]/n*100:5.1f}%)")
    for k in ("필터밖", "결손", "후보밖", "랭킹"):
        if 사례.get(k):
            print(f"    {k}: {사례[k][:12]}")
    if 미해결:
        # 평가 분모 밖이라 위 백분율에는 안 들어간다. 규정 모음 결손이므로 따로 센다.
        print(f"  (평가 제외 {len(미해결)}건 — 정답 청크가 코퍼스에 없다: "
              f"{[(m[0], m[1]) for m in 미해결]})")
    if 사례.get("필터밖"):
        print("    필터밖 상세 — 어느 조건에서 막혔나:")
        전체 = [c for d in 데이터 if d[0] in {x[0] for x in 사례['필터밖']} for c in d[3]]
        필터진단(cur, 전체)


def 필터진단(cur, chunk_ids: list[int]) -> None:
    """`필터밖` 으로 떨어진 청크가 **어느 조건**에서 막혔는지 하나씩 본다."""
    조건 = [("status", "status='active'"), ("parse_quality", "parse_quality='high'"),
            ("retrieval_scope", "retrieval_scope='진입점'"), ("layer", "layer IN ('L1','L2')"),
            ("적용대상", "적용대상 IN ('창업기업','공통')"), ("embedding", "embedding IS NOT NULL")]
    for 이름, 절 in 조건:
        cur.execute(f"SELECT count(*) FROM corpus.chunks WHERE chunk_id = ANY(%s) AND NOT ({절})",
                    (chunk_ids,))
        n = cur.fetchone()[0]
        if n:
            print(f"      {이름:16} 에서 {n}건 탈락")


def 지표(결과: list[tuple[list[int], set[int]]], ks: list[int]) -> dict:
    out = {f"hit@{k}": 0 for k in ks}
    rr = []
    for 순위, 정답 in 결과:
        첫 = next((i for i, c in enumerate(순위, 1) if c in 정답), None)
        rr.append(1.0 / 첫 if 첫 else 0.0)
        for k in ks:
            if 첫 and 첫 <= k:
                out[f"hit@{k}"] += 1
    n = len(결과)
    return {**{k: v / n * 100 for k, v in out.items()}, "MRR": sum(rr) / n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50, help="검색 깊이")
    ap.add_argument("--진단", action="store_true",
                    help="hit@5 미스의 원인 분해 — 필터 밖 / 후보 밖 / 랭킹 실패")
    ap.add_argument("--기록", action="store_true",
                    help="결과를 eval.runs 에 종류='retrieval' 로 남긴다 (D4 eval_store)")
    ap.add_argument("--gold-chunks", action="store_true",
                    help="정답 청크를 D3 eval.golden_chunks 고정 매핑에서 읽는다 "
                         "(평가 가능 70 -> 74문항. 70문항 부분집합 지표도 같이 찍는다)")
    ap.add_argument("--사업필터", action="store_true",
                    help="C7 — `사업명 IS NULL OR :사업 = ANY(사업명)` 를 켠다")
    ap.add_argument("--overlap", action="store_true",
                    help="C6 — BM25 vs dense top-20 겹침률 (RAG.md §2-4)")
    ap.add_argument("--c7", action="store_true",
                    help="C7 — 사업 필터 on/off 를 같은 임베딩으로 짝지어 비교")
    ap.add_argument("--dangling", action="store_true",
                    help="top-5 진입점이 물고 오는 dangling 참조 — A 의 DANGLING_WARN 발화 여부")
    ap.add_argument("--폐포", action="store_true",
                    help="top-5 + 참조 폐포까지 합쳤을 때의 조 단위 커버리지")
    a = ap.parse_args()
    KS = [1, 5, 10, 20, a.k]

    # 🔴 읽기 전용인데도 트랜잭션을 붙들면 다른 세션의 DDL 과 교착이 난다
    #    (2026-08-31 8세션 병렬 중 DeadlockDetected 실측). autocommit 으로 푼다.
    with psycopg.connect(DSN, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("""SELECT gold_id, 세트, 질문, 정답근거, 사업명 FROM eval.golden_set
                        WHERE 정답근거 IS NOT NULL ORDER BY gold_id""")
        rows = cur.fetchall()

        # 정답 청크 역추적. 못 찾은 문항은 평가에서 빼고 그 사실을 보고한다 —
        # 조용히 빼면 지표가 부풀려진다.
        데이터, 미해결 = [], []
        레거시70: set[int] = set()      # 원문 부분일치로도 잡히던 문항 — §7 비교용 부분집합
        for gid, 세트, q, 근거, 사업 in rows:
            구 = 정답청크(cur, 근거)
            if 구:
                레거시70.add(gid)
            정답 = 정답청크_고정(cur, gid) if a.gold_chunks else 구
            (데이터 if 정답 else 미해결).append((gid, 세트, q, 정답, 사업, 근거))
        출처 = "eval.golden_chunks 고정매핑" if a.gold_chunks else "원문 부분일치 역추적"
        print(f"골든셋 {len(rows)}문항 중 정답 청크를 찾은 것 {len(데이터)}건, "
              f"못 찾은 것 {len(미해결)}건  ({출처})")
        if 미해결:
            print("  못 찾은 문항 (평가 제외):")
            for gid, 세트, q, *_ in 미해결[:8]:
                print(f"    gold_id={gid} [{세트}] {q[:52]}")
            if len(미해결) > 8:
                print(f"    ... 외 {len(미해결)-8}건")
        if not 데이터:
            sys.exit("정답 청크를 하나도 못 찾았다. 매칭 규칙을 고칠 것.")

        # `공통(지침 제14차)` `공통` 은 사업명이 아니라 적용범위다 (D2). 필터에 넣으면
        # 존재하지 않는 사업으로 걸러 후보가 통째로 사라진다 — 여기서는 None 으로 본다.
        def 사업키(s: str | None) -> str | None:
            return None if not s or s.startswith("공통") else s

        print(f"\n질문 {len(데이터)}건 임베딩 (KURE-v1, CPU) ...", flush=True)
        t = time.time()
        retrieve.워밍업()
        벡터들 = [retrieve.임베딩(d[2]) for d in 데이터]
        print(f"  {time.time()-t:.0f}초\n")

        d_res, b_res, r_res = [], [], []
        겹침 = []
        참조확장res = []
        for (gid, 세트, q, 정답, 사업, 근거), vec in zip(데이터, 벡터들):
            사업 = 사업키(사업)
            dn = retrieve.dense(cur, vec, k=a.k, 사업명=사업, 사업필터=a.사업필터)
            dense = [c for c, _ in dn]
            bm = retrieve.sparse(cur, q, k=a.k, 사업명=사업, 사업필터=a.사업필터)
            d_res.append((dense, 정답)); b_res.append((bm, 정답))
            r_res.append((retrieve.rrf([dense, bm])[:a.k], 정답))
            if a.overlap:
                겹침.append(len(set(dense[:20]) & set(bm[:20])))
            if a.폐포:
                top5 = r_res[-1][0][:5]
                pae, _, _ = retrieve.폐포수집(cur, top5)
                # 조 단위로 맞춘다 — top-5 청크의 article_id + 참조 확장 article_id
                cur.execute("SELECT article_id FROM corpus.chunks WHERE chunk_id = ANY(%s)",
                            (top5,))
                도달 = [r[0] for r in cur.fetchall()]
                참조확장res.append((도달, 도달 + pae, 정답조(cur, 근거)))

        print(f"{'검색기':10} " + " ".join(f"{'hit@'+str(k):>8}" for k in KS) + f"{'MRR':>8}")
        print("-" * (10 + 9 * len(KS) + 8))
        for 이름, res in (("dense", d_res), ("BM25", b_res), ("RRF", r_res)):
            g = 지표(res, KS)
            print(f"{이름:10} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in KS)
                  + f" {g['MRR']:7.3f}")

        if a.gold_chunks:
            # 🔴 §7 — 문항 수가 바뀌면 hit율을 직접 비교하면 안 된다. 늘어난 4문항은
            #    전부 부속물(별표·별지) 근거라 난이도가 다르다. 같은 70문항으로 맞춰 찍는다.
            부분 = [r for r, d in zip(r_res, 데이터) if d[0] in 레거시70]
            새것 = [(d[0], d[1]) for d in 데이터 if d[0] not in 레거시70]
            print(f"\n[§7 비교 보정] 위는 {len(데이터)}문항 기준이다. "
                  f"70문항 시절과 비교하려면 아래를 봐라 — 같은 문항집합이다.")
            g = 지표(부분, KS)
            print(f"{'RRF(70)':10} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in KS)
                  + f" {g['MRR']:7.3f}")
            print(f"  고정매핑으로 새로 평가된 문항 {len(새것)}건: {새것}")

        print("\n세트별 RRF hit@%d" % a.k)
        for 세트 in sorted({d[1] for d in 데이터}):
            부분 = [r for r, d in zip(r_res, 데이터) if d[1] == 세트]
            print(f"  {세트:8} {len(부분):3}건  {지표(부분, [a.k])['hit@'+str(a.k)]:5.1f}%")

        if a.dangling:
            # A3 DANGLING_WARN 이 L1·L2 경로에서 실제로 발화하는지. 판정 인덱스 **안의**
            # 끊긴 참조만 신호다 — top-5 진입점에서 출발한 것만 센다 (RAG.md §4-3).
            빈, 있음 = 0, []
            for (gid, _세트, _q, _정답, _사업, _근거), row in zip(데이터, r_res):
                _, _, dang = retrieve.폐포수집(cur, row[0][:5])
                if dang:
                    있음.append((gid, dang))
                else:
                    빈 += 1
            print()
            print(f"[dangling] 70문항 중 발화 {len(있음)}건 / 없음 {빈}건 "
                  f"(사업필터={'켬' if a.사업필터 else '끔'})")
            for gid, d in 있음[:10]:
                print(f"    gold_id={gid:3}  {len(d)}건  {d[:3]}")
            if len(있음) > 10:
                print(f"    ... 외 {len(있음)-10}문항")

        if a.c7:
            # 🔴 필터 on/off 를 **같은 임베딩으로 짝지어** 잰다. 따로 두 번 돌리면
            #    CPU 부하 차이가 섞여 어느 쪽이 원인인지 못 가린다.
            #    공통 문항은 사업명이 NULL 이라 필터가 no-op — 분모에서 뺀다.
            지정idx = [i for i, d in enumerate(데이터) if 사업키(d[4])]
            켬 = []
            for i in 지정idx:
                _gid, _세트, q, 정답, 사업, _근거 = 데이터[i]
                사업 = 사업키(사업)
                dn = [c for c, _ in retrieve.dense(cur, 벡터들[i], k=a.k,
                                                   사업명=사업, 사업필터=True)]
                bm = retrieve.sparse(cur, q, k=a.k, 사업명=사업, 사업필터=True)
                켬.append((retrieve.rrf([dn, bm])[:a.k], 정답))
            끔 = [r_res[i] for i in 지정idx]
            print()
            print(f"[C7] 사업 필터 — 사업이 지정된 {len(지정idx)}문항만 "
                  f"(공통 {len(데이터)-len(지정idx)}문항은 no-op 이라 제외)")
            print(f"{'':10} " + " ".join(f"{'hit@'+str(k):>8}" for k in KS) + f"{'MRR':>8}")
            for 이름, res in (("필터 끔", 끔), ("필터 켬", 켬)):
                g = 지표(res, KS)
                print(f"{이름:10} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in KS)
                      + f" {g['MRR']:7.3f}")
            def 적중(row):
                return any(c in row[1] for c in row[0][:5])
            뒤집 = [(데이터[i][0], 데이터[i][4], 적중(끔[n]), 적중(켬[n]))
                    for n, i in enumerate(지정idx) if 적중(끔[n]) != 적중(켬[n])]
            print(f"  hit@5 가 뒤집힌 문항 {len(뒤집)}건 (gold_id, 사업, 끔, 켬):")
            for x in 뒤집:
                print("   ", x)

        if a.사업필터:
            지정 = [(r, d) for r, d in zip(r_res, 데이터) if 사업키(d[4])]
            print(f"\n사업 지정 문항만 {len(지정)}건  "
                  f"RRF hit@5 {지표([r for r, _ in 지정], [5])['hit@5']:.1f}%")

        if a.overlap:
            겹침.sort()
            n = len(겹침)
            print(f"\n[C6] BM25 vs dense top-20 겹침 (n={n})")
            print(f"  평균 {sum(겹침)/n:.2f}/20 ({sum(겹침)/n/20*100:.1f}%) · "
                  f"중앙 {겹침[n//2]} · 최소 {겹침[0]} · 최대 {겹침[-1]}")
            print(f"  겹침 0건인 질문 {sum(1 for x in 겹침 if x == 0)}건 "
                  f"({sum(1 for x in 겹침 if x == 0)/n*100:.1f}%)")

        if a.진단:
            진단하기(cur, 데이터, r_res, 벡터들, a, 미해결)

        if a.기록:
            # D4 `eval_store.기록()`. 🔴 `설정` 을 반드시 채운다 — 내일 아침 "이 숫자가
            # 무엇 때문에 나왔나" 를 답할 수 있는 건 이 필드뿐이다 (eval_store 독스트링).
            import eval_store
            지표들 = {f"{이름}.{k}": v
                      for 이름, res in (("dense", d_res), ("BM25", b_res), ("RRF", r_res))
                      for k, v in 지표(res, KS).items()}
            if a.overlap and 겹침:
                지표들["C6.겹침평균_20"] = sum(겹침) / len(겹침)
                지표들["C6.겹침률_%"] = sum(겹침) / len(겹침) / 20 * 100
            run_id = eval_store.기록(
                {"종류": "retrieval",
                 "설정": {"검색기": "dense+BM25 -> RRF",
                          "W_DENSE": retrieve.W_DENSE, "W_SPARSE": retrieve.W_SPARSE,
                          "RRF_K": retrieve.RRF_K, "후보K": a.k, "폐포깊이": retrieve.깊이,
                          "사업필터": bool(a.사업필터),
                          "정답출처": "golden_chunks" if a.gold_chunks else "원문부분일치",
                          "임베딩": "KURE-v1/cpu", "세션": "C"},
                 "문항수": len(데이터), "지표": 지표들,
                 "라벨": f"C/eval_retrieval 사업필터={'on' if a.사업필터 else 'off'}"
                         f" 정답={'고정' if a.gold_chunks else '역추적'}",
                 # 🔴 비고는 표를 훑는 사람이 읽는 유일한 자유 텍스트다. 조건이 다른 행끼리
                 #    hit@5 를 빼는 오독이 가장 위험하므로 그 경고를 여기 박는다.
                 "비고": ("조건: 사업필터="
                          + ("on" if a.사업필터 else "off")
                          + " · 정답출처="
                          + ("golden_chunks(74문항)" if a.gold_chunks else "원문역추적(70문항)")
                          + ". 🔴 사업필터나 정답출처가 다른 run 과 hit@k 를 직접 빼지 마라"
                            " — 필터도 분모도 다르다. 같은 문항집합 비교는 --gold-chunks"
                            " 실행 출력의 'RRF(70)' 행을 쓴다.")},
                [{"gold_id": d[0], "예측": "hit" if any(c in r[1] for c in r[0][:5]) else "miss",
                  "정답": "hit", "적중": any(c in r[1] for c in r[0][:5]),
                  "원출력": {"top5": r[0][:5], "정답청크수": len(d[3]), "세트": d[1]}}
                 for d, r in zip(데이터, r_res)])
            print(f"\n[기록] eval.runs run_id={run_id} (종류=retrieval)")

        if a.폐포:
            def cov(idx):
                맞 = sum(1 for row in 참조확장res if set(row[idx]) & row[2])
                return 맞 / len(참조확장res) * 100
            분모 = sum(1 for row in 참조확장res if row[2])
            print(f"\n[폐포] 조 단위 커버리지 (정답 조를 역추적한 {분모}/{len(참조확장res)}문항 기준)")
            print(f"  top-5 만        {cov(0):5.1f}%")
            print(f"  top-5 + 폐포    {cov(1):5.1f}%")
            추가 = sum(len(set(row[1]) - set(row[0])) for row in 참조확장res) / len(참조확장res)
            print(f"  폐포가 더한 조  평균 {추가:.1f}개/문항")


if __name__ == "__main__":
    main()
