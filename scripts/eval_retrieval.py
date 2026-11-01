# -*- coding: utf-8 -*-
"""검색 품질 평가 CLI: dense / BM25 / RRF 가 골든셋의 정답 조문을 상위 k 안에 가져오는지 잰다.

검색은 serving 과 같은 `retrieve.py` 함수를 호출한다 — 평가와 실전이 다른 코드를 쓰면 이 숫자가 실전을 설명하지 못한다.
정답은 `eval.golden_set.정답근거`(doc / 조번호 / 원문)를 원문 부분일치로 역추적하거나, `--gold-chunks` 면
`eval.golden_chunks` 고정 매핑에서 읽는다. `--기록` 일 때만 `eval.runs`/`eval.run_items` 에 쓴다.
지표: hit@k(정답 청크가 상위 k 안에 있는가), MRR(첫 정답 순위의 역수 평균). 사업 필터 기본값은 serving 과 같다.

실행: PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --k 20 [--overlap] [--필터비교] [--dangling] [--참조확장] [--진단]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
import retrieve  # noqa: E402
from eval_store import 사업키  # noqa: E402


class 문항(NamedTuple):
    gold_id: int
    세트: str
    질문: str
    정답: set[int]          # 정답 chunk_id 집합
    사업명: str | None      # 골든셋 원표기. 검색에는 사업키() 로 바꿔 넘긴다
    근거: list[dict]        # eval.golden_set.정답근거


@dataclass
class 검색결과:
    """문항별 검색 결과. 각 목록은 `데이터` 와 같은 순서다. 순위 항목은 (chunk_id 순위, 정답 집합)."""
    벡터들: list[str]
    dense: list[tuple[list[int], set[int]]] = field(default_factory=list)
    bm25: list[tuple[list[int], set[int]]] = field(default_factory=list)
    rrf: list[tuple[list[int], set[int]]] = field(default_factory=list)
    겹침: list[int] = field(default_factory=list)       # --overlap: dense·BM25 top-20 공통 개수
    참조확장: list[tuple[list[int], list[int], set[int]]] = field(default_factory=list)
    # --참조확장: (top-5 의 article_id, top-5 + 참조 확장 article_id, 정답 article_id)


def _사업필터_기본() -> bool:
    """serving(orchestrate.사업필터)과 같은 기본값. SUDDOE_PROGRAM_FILTER(없으면 예전 이름 SUDDOE_사업필터)가
    '0' 이 아니면 켠다."""
    return (os.environ.get("SUDDOE_PROGRAM_FILTER") or os.environ.get("SUDDOE_사업필터", "1")) != "0"


# ── 정답 역추적 ──────────────────────────────────────────────────────────────
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
    """`eval.golden_chunks` 고정 매핑에서 정답 chunk_id 를 읽는다.

    `정답청크()` 는 `[붙임2]`·`별표1`·`별지4` 같은 부속물 위치를 `제N조` 정규식으로 잡지 못해
    평가 가능한 문항이 더 적다. 늘어난 문항은 난이도가 달라, `--gold-chunks` 는 원문 부분일치로도
    정답을 찾는 부분집합의 지표를 함께 출력한다.
    """
    cur.execute("""SELECT chunk_id FROM eval.golden_chunks
                    WHERE gold_id = %s AND chunk_id IS NOT NULL""", (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 정답조(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> `doc_articles.article_id` 집합. 참조 확장은 조 단위라 청크로는 잴 수 없다."""
    out: set[int] = set()
    for g in 근거 or []:
        조 = re.match(r"(제\d+조(?:의\d+)?)", g.get("조번호") or "")
        if not (g.get("doc") and 조):
            continue
        cur.execute("SELECT article_id FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
                    (g["doc"], 조.group(1)))
        out |= {r[0] for r in cur.fetchall()}
    return out


def 정답_적재(cur, gold_chunks: bool) -> tuple[list[문항], list[문항], set[int]]:
    """골든셋을 읽어 문항별 정답 청크를 찾는다.

    반환: (평가할 문항, 정답을 못 찾아 제외한 문항, 원문 부분일치로 정답을 찾은 gold_id 집합).
    제외한 문항은 출력으로 알린다 — 말없이 빼면 지표가 부풀려진다. 평가할 문항이 0건이면 종료한다.
    """
    cur.execute("""SELECT gold_id, 세트, 질문, 정답근거, 사업명 FROM eval.golden_set
                    WHERE 정답근거 IS NOT NULL ORDER BY gold_id""")
    rows = cur.fetchall()

    데이터, 미해결 = [], []
    부분일치: set[int] = set()
    for gid, 세트, q, 근거, 사업 in rows:
        구 = 정답청크(cur, 근거)
        if 구:
            부분일치.add(gid)
        정답 = 정답청크_고정(cur, gid) if gold_chunks else 구
        (데이터 if 정답 else 미해결).append(문항(gid, 세트, q, 정답, 사업, 근거))
    출처 = "eval.golden_chunks 고정매핑" if gold_chunks else "원문 부분일치 역추적"
    print(f"골든셋 {len(rows)}문항 중 정답 청크를 찾은 것 {len(데이터)}건, "
          f"못 찾은 것 {len(미해결)}건  ({출처})")
    if 미해결:
        print("  못 찾은 문항 (평가 제외):")
        for m in 미해결[:8]:
            print(f"    gold_id={m.gold_id} [{m.세트}] {m.질문[:52]}")
        if len(미해결) > 8:
            print(f"    ... 외 {len(미해결)-8}건")
    if not 데이터:
        sys.exit("정답 청크를 하나도 못 찾았다. 매칭 규칙을 고칠 것.")
    return 데이터, 미해결, 부분일치


# ── 검색 실행 ────────────────────────────────────────────────────────────────
def 검색_실행(cur, 데이터: list[문항], a) -> 검색결과:
    """문항마다 dense·BM25·RRF 순위를 구한다. --overlap / --참조확장 이면 그 재료도 모은다."""
    print(f"\n질문 {len(데이터)}건 임베딩 (KURE-v1, CPU) ...", flush=True)
    t = time.time()
    retrieve.워밍업()
    결과 = 검색결과(벡터들=[retrieve.임베딩(d.질문) for d in 데이터])
    print(f"  {time.time()-t:.0f}초\n")

    for d, vec in zip(데이터, 결과.벡터들):
        사업 = 사업키(d.사업명)
        dense = [c for c, _ in retrieve.dense(cur, vec, k=a.k, 사업명=사업, 사업필터=a.사업필터)]
        bm = retrieve.sparse(cur, d.질문, k=a.k, 사업명=사업, 사업필터=a.사업필터)
        결과.dense.append((dense, d.정답))
        결과.bm25.append((bm, d.정답))
        결과.rrf.append((retrieve.rrf([dense, bm])[:a.k], d.정답))
        if a.overlap:
            결과.겹침.append(len(set(dense[:20]) & set(bm[:20])))
        if a.참조확장:
            top5 = 결과.rrf[-1][0][:5]
            확장조, _, _ = retrieve.참조확장_수집(cur, top5)
            # 조 단위로 맞춘다 — top-5 청크의 article_id + 참조 확장 article_id
            cur.execute("SELECT article_id FROM corpus.chunks WHERE chunk_id = ANY(%s)",
                        (top5,))
            도달 = [r[0] for r in cur.fetchall()]
            결과.참조확장.append((도달, 도달 + 확장조, 정답조(cur, d.근거)))
    return 결과


def 지표(결과: list[tuple[list[int], set[int]]], ks: list[int]) -> dict:
    """hit@k(%) 와 MRR. 결과가 비어 있으면 ZeroDivisionError 이므로 호출 쪽에서 거른다."""
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


def _지표행(이름: str, res, ks: list[int]) -> str:
    g = 지표(res, ks)
    return (f"{이름:10} " + " ".join(f"{g['hit@'+str(k)]:7.1f}%" for k in ks)
            + f" {g['MRR']:7.3f}")


# ── 보고 ─────────────────────────────────────────────────────────────────────
def 보고_기본표(데이터: list[문항], 결과: 검색결과, 부분일치: set[int], a, ks: list[int]) -> None:
    """검색기별 hit@k·MRR 표, (--gold-chunks) 부분일치 부분집합 지표, 세트별 RRF hit@k."""
    print(f"{'검색기':10} " + " ".join(f"{'hit@'+str(k):>8}" for k in ks) + f"{'MRR':>8}")
    print("-" * (10 + 9 * len(ks) + 8))
    for 이름, res in (("dense", 결과.dense), ("BM25", 결과.bm25), ("RRF", 결과.rrf)):
        print(_지표행(이름, res, ks))

    if a.gold_chunks:
        # 문항 수가 바뀌면 hit율을 직접 비교할 수 없다 — 고정 매핑으로 늘어난 문항은 부속물
        # (별표·별지) 근거라 난이도가 다르다. 원문 부분일치로 찾는 문항 집합으로 맞춰 다시 계산한다.
        부분 = [r for r, d in zip(결과.rrf, 데이터) if d.gold_id in 부분일치]
        새것 = [(d.gold_id, d.세트) for d in 데이터 if d.gold_id not in 부분일치]
        print(f"\n[부분일치 부분집합] 위는 {len(데이터)}문항 기준이다. "
              f"원문 부분일치 역추적 run 과 비교하려면 아래를 봐라 — 같은 문항집합이다.")
        print(_지표행(f"RRF({len(부분)})", 부분, ks))
        print(f"  고정매핑으로 새로 평가된 문항 {len(새것)}건: {새것}")

    print("\n세트별 RRF hit@%d" % a.k)
    for 세트 in sorted({d.세트 for d in 데이터}):
        부분 = [r for r, d in zip(결과.rrf, 데이터) if d.세트 == 세트]
        print(f"  {세트:8} {len(부분):3}건  {지표(부분, [a.k])['hit@'+str(a.k)]:5.1f}%")


def 보고_dangling(cur, 데이터: list[문항], 결과: 검색결과, a) -> None:
    """top-5 진입점에서 출발한 끊긴 참조가 있는 문항 — 판정에서 DANGLING_WARN 강등이 붙을 수 있는 문항이다."""
    빈, 있음 = 0, []
    for d, row in zip(데이터, 결과.rrf):
        _, _, dang = retrieve.참조확장_수집(cur, row[0][:5])
        if dang:
            있음.append((d.gold_id, dang))
        else:
            빈 += 1
    print()
    print(f"[dangling] {len(데이터)}문항 중 끊긴 참조 있음 {len(있음)}건 / 없음 {빈}건 "
          f"(사업필터={'켬' if a.사업필터 else '끔'})")
    for gid, dang in 있음[:10]:
        print(f"    gold_id={gid:3}  {len(dang)}건  {dang[:3]}")
    if len(있음) > 10:
        print(f"    ... 외 {len(있음)-10}문항")


def 보고_필터비교(cur, 데이터: list[문항], 결과: 검색결과, a, ks: list[int]) -> None:
    """(--필터비교) 사업 필터 on/off 를 같은 임베딩으로 짝지어 비교하고, (--사업필터) 사업 지정 문항만의 hit@5 를 출력한다.

    사업이 지정된 문항이 없으면 아무것도 출력하지 않는다.
    """
    지정idx = [i for i, d in enumerate(데이터) if 사업키(d.사업명)]
    if a.필터비교 and 지정idx:
        # 필터 on/off 를 같은 임베딩으로 짝지어 잰다 — 따로 두 번 돌리면 CPU 부하
        # 차이가 섞여 어느 쪽이 원인인지 가릴 수 없다. 공통 문항은 사업명이 NULL 이라
        # 필터가 no-op — 분모에서 뺀다.
        켬 = []
        for i in 지정idx:
            d = 데이터[i]
            사업 = 사업키(d.사업명)
            dn = [c for c, _ in retrieve.dense(cur, 결과.벡터들[i], k=a.k,
                                               사업명=사업, 사업필터=True)]
            bm = retrieve.sparse(cur, d.질문, k=a.k, 사업명=사업, 사업필터=True)
            켬.append((retrieve.rrf([dn, bm])[:a.k], d.정답))
        끔 = [결과.rrf[i] for i in 지정idx]
        print()
        print(f"[필터비교] 사업 필터 — 사업이 지정된 {len(지정idx)}문항만 "
              f"(공통 {len(데이터)-len(지정idx)}문항은 no-op 이라 제외)")
        print(f"{'':10} " + " ".join(f"{'hit@'+str(k):>8}" for k in ks) + f"{'MRR':>8}")
        for 이름, res in (("필터 끔", 끔), ("필터 켬", 켬)):
            print(_지표행(이름, res, ks))

        def 적중(row):
            return any(c in row[1] for c in row[0][:5])
        뒤집 = [(데이터[i].gold_id, 데이터[i].사업명, 적중(끔[n]), 적중(켬[n]))
                for n, i in enumerate(지정idx) if 적중(끔[n]) != 적중(켬[n])]
        print(f"  hit@5 가 뒤집힌 문항 {len(뒤집)}건 (gold_id, 사업, 끔, 켬):")
        for x in 뒤집:
            print("   ", x)

    if a.사업필터 and 지정idx:
        지정 = [결과.rrf[i] for i in 지정idx]
        print(f"\n사업 지정 문항만 {len(지정)}건  "
              f"RRF hit@5 {지표(지정, [5])['hit@5']:.1f}%")


def 보고_겹침(겹침: list[int]) -> None:
    """(--overlap) dense·BM25 top-20 이 얼마나 겹치는지. 겹침이 낮을수록 RRF 결합의 이득이 크다."""
    겹침 = sorted(겹침)
    n = len(겹침)
    print(f"\n[겹침] BM25 vs dense top-20 겹침 (n={n})")
    print(f"  평균 {sum(겹침)/n:.2f}/20 ({sum(겹침)/n/20*100:.1f}%) · "
          f"중앙 {겹침[n//2]} · 최소 {겹침[0]} · 최대 {겹침[-1]}")
    print(f"  겹침 0건인 질문 {sum(1 for x in 겹침 if x == 0)}건 "
          f"({sum(1 for x in 겹침 if x == 0)/n*100:.1f}%)")


# 미스 원인 분해 — hit@5 를 놓친 문항을 네 가지 원인으로 나눈다. "검색이 나쁘다" 는 진단이 아니다.
#   필터밖    정답 청크가 pre-filter 를 통과하지 못한다 (status·scope·적용대상·사업명)
#             -> 검색기를 아무리 고쳐도 안 잡힌다. 태깅·적재 쪽 문제다
#   결손      정답 근거에 해당하는 청크 자체가 없다 (부속물 미청킹 등)
#             -> 코퍼스 문제. 청킹·룰 테이블 쪽
#   후보밖    필터는 통과하는데 dense 전체 순위가 k 밖이다
#             -> 임베딩/질의 표현 문제. 리랭커로도 구할 수 없다
#   랭킹      후보 안에는 있는데 top-5 밖이다
#             -> 결합·가중 문제. 검색기 튜닝으로 줄일 수 있는 구간은 여기뿐이다
_적격 = f"""SELECT chunk_id FROM corpus.chunks
             WHERE chunk_id = ANY(%(g)s) AND {retrieve.FILTER}"""

_순위 = f"""SELECT count(*) + 1 FROM corpus.chunks
             WHERE {retrieve.FILTER}
               AND (embedding <=> %(v)s::extensions.vector(1024))
                 < (SELECT embedding <=> %(v)s::extensions.vector(1024)
                      FROM corpus.chunks WHERE chunk_id = %(c)s)"""


def 보고_진단(cur, 데이터: list[문항], 결과: 검색결과, a, 미해결: list[문항]) -> None:
    """(--진단) hit@5 미스를 원인별로 센다. 원인 정의는 위 주석 참조."""
    원인 = Counter()
    사례: dict[str, list] = {}
    for d, (순위, _), vec in zip(데이터, 결과.rrf, 결과.벡터들):
        if any(c in d.정답 for c in 순위[:5]):
            원인["적중"] += 1
            continue
        if not d.정답:
            원인["결손"] += 1
            사례.setdefault("결손", []).append((d.gold_id, d.세트, d.사업명))
            continue
        cur.execute(_적격, {"g": list(d.정답)})
        적격 = [r[0] for r in cur.fetchall()]
        if not 적격:
            원인["필터밖"] += 1
            사례.setdefault("필터밖", []).append((d.gold_id, d.세트, d.사업명))
            continue
        최선 = min((cur.execute(_순위, {"v": vec, "c": c}).fetchone()[0]) for c in 적격)
        키 = "랭킹" if 최선 <= a.k else "후보밖"
        원인[키] += 1
        사례.setdefault(키, []).append((d.gold_id, d.세트, d.사업명, 최선))
    n = sum(원인.values())
    print(f"\n[진단] hit@5 미스 원인 분해 (n={n} · 사업필터={'켬' if a.사업필터 else '끔'})")
    for k in ("적중", "랭킹", "후보밖", "필터밖", "결손"):
        if 원인[k]:
            print(f"  {k:6} {원인[k]:3}건 ({원인[k]/n*100:5.1f}%)")
    for k in ("필터밖", "결손", "후보밖", "랭킹"):
        if 사례.get(k):
            print(f"    {k}: {사례[k][:12]}")
    if 미해결:
        # 평가 분모 밖이라 위 백분율에는 안 들어간다. 코퍼스 결손이므로 따로 센다.
        print(f"  (평가 제외 {len(미해결)}건 — 정답 청크가 코퍼스에 없다: "
              f"{[(m.gold_id, m.세트) for m in 미해결]})")
    if 사례.get("필터밖"):
        print("    필터밖 상세 — 어느 조건에서 막혔나:")
        막힌 = {x[0] for x in 사례["필터밖"]}
        필터진단(cur, [c for d in 데이터 if d.gold_id in 막힌 for c in d.정답])


def 필터진단(cur, chunk_ids: list[int]) -> None:
    """`필터밖` 으로 떨어진 청크가 어느 조건에서 막혔는지 하나씩 본다."""
    조건 = [("status", "status='active'"), ("parse_quality", "parse_quality='high'"),
            ("retrieval_scope", "retrieval_scope='진입점'"), ("layer", "layer IN ('L1','L2')"),
            ("적용대상", "적용대상 IN ('창업기업','공통')"), ("embedding", "embedding IS NOT NULL")]
    for 이름, 절 in 조건:
        cur.execute(f"SELECT count(*) FROM corpus.chunks WHERE chunk_id = ANY(%s) AND NOT ({절})",
                    (chunk_ids,))
        n = cur.fetchone()[0]
        if n:
            print(f"      {이름:16} 에서 {n}건 탈락")


def 결과_기록(데이터: list[문항], 결과: 검색결과, a, ks: list[int]) -> None:
    """(--기록) 지표와 문항별 hit@5 를 `eval_store.기록()` 으로 eval.runs / eval.run_items 에 쓴다.

    `설정` 은 이 숫자가 어떤 조건에서 나왔는지 답할 수 있는 유일한 필드라 반드시 채운다.
    """
    import eval_store
    지표들 = {f"{이름}.{k}": v
              for 이름, res in (("dense", 결과.dense), ("BM25", 결과.bm25), ("RRF", 결과.rrf))
              for k, v in 지표(res, ks).items()}
    if a.overlap and 결과.겹침:
        # 키 이름의 'C6' 은 예전 출력 표시다. 지난 eval.runs 행과 비교할 수 있게 저장 키는 바꾸지 않는다
        지표들["C6.겹침평균_20"] = sum(결과.겹침) / len(결과.겹침)
        지표들["C6.겹침률_%"] = sum(결과.겹침) / len(결과.겹침) / 20 * 100
    필터 = "on" if a.사업필터 else "off"
    정답출처 = "golden_chunks" if a.gold_chunks else "원문역추적"
    run_id = eval_store.기록(
        {"종류": "retrieval",
         "설정": {"검색기": "dense+BM25 -> RRF",
                  "W_DENSE": retrieve.W_DENSE, "W_SPARSE": retrieve.W_SPARSE,
                  "RRF_K": retrieve.RRF_K, "후보K": a.k, "폐포깊이": retrieve.깊이,
                  "사업필터": bool(a.사업필터),
                  "정답출처": "golden_chunks" if a.gold_chunks else "원문부분일치",
                  "임베딩": "KURE-v1/cpu"},
         "문항수": len(데이터), "지표": 지표들,
         "라벨": f"eval_retrieval 사업필터={필터}"
                 f" 정답={'고정' if a.gold_chunks else '역추적'}",
         # 비고는 표를 훑는 사람이 읽는 유일한 자유 텍스트다 — 조건이 다른 행끼리
         # hit@k 를 빼는 오독을 막는 경고를 여기 남긴다.
         "비고": (f"조건: 사업필터={필터} · 정답출처={정답출처}({len(데이터)}문항). "
                  "사업필터나 정답출처가 다른 run 과는 필터와 분모가 달라 hit@k 를 직접 비교하지 않는다. "
                  "같은 문항집합 비교는 --gold-chunks 실행 출력의 부분일치 부분집합 행을 쓴다.")},
        [{"gold_id": d.gold_id, "예측": "hit" if any(c in r[1] for c in r[0][:5]) else "miss",
          "정답": "hit", "적중": any(c in r[1] for c in r[0][:5]),
          "원출력": {"top5": r[0][:5], "정답청크수": len(d.정답), "세트": d.세트}}
         for d, r in zip(데이터, 결과.rrf)])
    print(f"\n[기록] eval.runs run_id={run_id} (종류=retrieval)")


def 보고_참조확장(참조확장res: list[tuple[list[int], list[int], set[int]]]) -> None:
    """(--참조확장) 조 단위 커버리지: 정답 조를 역추적한 문항 중 top-5(+참조 확장)가 정답 조에 닿은 비율."""
    해소 = [row for row in 참조확장res if row[2]]

    def cov(idx):
        if not 해소:
            return 0.0
        맞 = sum(1 for row in 해소 if set(row[idx]) & row[2])
        return 맞 / len(해소) * 100
    print(f"\n[참조확장] 조 단위 커버리지 (정답 조를 역추적한 {len(해소)}/{len(참조확장res)}문항 기준)")
    print(f"  top-5 만          {cov(0):5.1f}%")
    print(f"  top-5 + 참조확장  {cov(1):5.1f}%")
    추가 = sum(len(set(row[1]) - set(row[0])) for row in 참조확장res) / len(참조확장res)
    print(f"  참조확장이 더한 조  평균 {추가:.1f}개/문항")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50, help="검색 깊이")
    ap.add_argument("--진단", action="store_true",
                    help="hit@5 미스의 원인 분해 — 필터 밖 / 후보 밖 / 랭킹 실패")
    ap.add_argument("--기록", action="store_true",
                    help="결과를 eval.runs 에 종류='retrieval' 로 남긴다")
    ap.add_argument("--gold-chunks", action="store_true",
                    help="정답 청크를 eval.golden_chunks 고정 매핑에서 읽는다 "
                         "(원문 부분일치 부분집합 지표도 같이 출력한다)")
    ap.add_argument("--사업필터", action=argparse.BooleanOptionalAction, default=_사업필터_기본(),
                    help="`사업명 IS NULL OR :사업 = ANY(사업명)` 를 건다. "
                         "기본값은 SUDDOE_PROGRAM_FILTER(기본 켬), --no-사업필터 로 끈다")
    ap.add_argument("--overlap", action="store_true",
                    help="BM25 vs dense top-20 겹침률")
    ap.add_argument("--필터비교", "--c7", dest="필터비교", action="store_true",
                    help="사업 필터 on/off 를 같은 임베딩으로 짝지어 비교")
    ap.add_argument("--dangling", action="store_true",
                    help="top-5 진입점에서 출발한 끊긴 참조 — DANGLING_WARN 강등이 붙을 수 있는 문항")
    ap.add_argument("--참조확장", "--폐포", dest="참조확장", action="store_true",
                    help="top-5 + 참조 확장까지 합쳤을 때의 조 단위 커버리지")
    a = ap.parse_args()
    # 검색 깊이(a.k)보다 큰 k 는 hit@a.k 와 같아 의미가 없고, 같은 k 가 두 번 들어가면 표에 열이 겹친다
    ks = sorted({k for k in (1, 5, 10, 20) if k <= a.k} | {a.k})

    # 읽기 전용이라도 트랜잭션을 열어 두면 다른 세션의 DDL 과 락 대기가 생길 수 있어 autocommit 으로 연다.
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        데이터, 미해결, 부분일치 = 정답_적재(cur, a.gold_chunks)
        결과 = 검색_실행(cur, 데이터, a)
        보고_기본표(데이터, 결과, 부분일치, a, ks)
        if a.dangling:
            보고_dangling(cur, 데이터, 결과, a)
        보고_필터비교(cur, 데이터, 결과, a, ks)
        if a.overlap:
            보고_겹침(결과.겹침)
        if a.진단:
            보고_진단(cur, 데이터, 결과, a, 미해결)
        if a.기록:
            결과_기록(데이터, 결과, a, ks)
        if a.참조확장:
            보고_참조확장(결과.참조확장)


if __name__ == "__main__":
    main()
