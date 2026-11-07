# -*- coding: utf-8 -*-
"""검색 품질 평가 CLI: dense / BM25 / RRF 가 골든셋의 정답 조문을 상위 k 안에 가져오는지 잰다.

검색은 운영 판정 경로와 같은 `retrieve.py` 함수를 호출한다 — 평가와 실전이 다른 코드를 쓰면 이 숫자가 실전을 설명하지 못한다.
정답은 `eval.golden_set.정답근거`(doc_id / 조번호 / 원문)를 원문 부분일치로 코퍼스에서 찾거나, `--gold-chunks` 면
`eval.golden_chunks` 고정 매핑에서 읽는다. `--기록` 일 때만 `eval.runs`/`eval.run_items` 에 쓴다.
지표: hit@k(정답 청크가 상위 k 안에 있는가), MRR(첫 정답 순위의 역수 평균). 사업 필터 기본값은 운영 판정 경로와 같다.

세트를 고르지 않으면 정답셋 v2 행은 읽지 않는다(기존 문항만). `--세트 v2_dev` 는 v2 문항을 읽고, 정답을 결정 근거 조문으로 잡아
근거 단위 · 문항 단위 포함률(상위 5 + 참조 확장)을 함께 낸다. 결정 근거가 L3 기관 규정뿐인 문항은 run 에 평가 제외로 남긴다. v2 문항 본문은 출력하지 않는다.

`--candidates ontology|search|both` 는 새 경로의 후보 커버율을 잰다(P5-1, docs/5_룰/5-9_검증과_측정.md 커버율 프로토콜). 후보는 판정 경로의 함수를 그대로 부른다:
routes_normalize._온톨로지_정규화 → orchestrate.온톨로지_조회 · orchestrate._새_검색_스레드 → ontology_context.문맥_구성. 글자 상한으로 빠진 항목은 포함으로 세지 않는다.
입력 비목은 질의 변환 추정값과 정답 비목(정답 비목은 후보를 재는 입력일 뿐 판정에 넘기지 않는다) 둘 다 잰다. L3 문항은 기관규정문을 미연결 조문 행으로 문맥_구성에 넘긴다(DB 쓰기 없음).

실행: PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --k 20 [--overlap] [--필터비교] [--dangling] [--참조확장] [--진단]
      PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --세트 v2_dev --사업 "모두의 창업 프로젝트" --k 20 --기록
      PYTHONIOENCODING=utf-8 python scripts/eval_retrieval.py --세트 v2_dev --사업 "모두의 창업 프로젝트" --candidates both --기록
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
from eval_store import 기존세트_SQL, 평가용_사업명  # noqa: E402


class 문항(NamedTuple):
    gold_id: int
    세트: str
    질문: str
    정답: set[int]          # 정답 chunk_id 집합
    사업명: str | None      # 골든셋 원표기. 검색에는 평가용_사업명() 으로 바꿔 넘긴다
    근거: list[dict]        # eval.golden_set.정답근거
    결정조문: frozenset[int] = frozenset()   # v2: 결정 근거의 article_id
    L3: bool = False        # v2: 기관 규정 반사실 문항
    비목: str | None = None  # v2: 정답 비목. --candidates 의 입력비목=정답 에서만 읽는다
    메타: dict | None = None # v2: 문항메타(답들 · 기관규정문). --candidates 에서만 읽는다


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
    """운영 판정 경로(orchestrate.사업필터)와 같은 기본값. SUDDOE_PROGRAM_FILTER 가 '0' 이 아니면 켠다."""
    return os.environ.get("SUDDOE_PROGRAM_FILTER", "1") != "0"


# ── 정답 청크 찾기(원문 부분일치) ───────────────────────────────────────────────
def 텍스트_정규화(s: str) -> str:
    """공백·괄호·문장부호 차이를 무시하고 비교한다. 원문 인용이 완전히 같지 않을 수 있다."""
    return re.sub(r"[\s　·,.\"'()（）「」『』]", "", s or "")


def 정답청크(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> chunk_id 집합. 원문 부분일치가 1차, 조번호가 2차."""
    found: set[int] = set()
    for g in 근거 or []:
        doc, 조, 원문 = g.get("doc_id"), g.get("조번호") or "", g.get("원문") or ""
        핵심 = 텍스트_정규화(원문)[:40]     # 앞 40자면 조문을 특정하기 충분하다
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

    `정답청크()` 는 `[붙임2]`·`별표1`·`별지4` 같은 첨부 위치를 `제N조` 정규식으로 잡지 못해
    평가 가능한 문항이 더 적다. 늘어난 문항은 난이도가 달라, `--gold-chunks` 는 원문 부분일치로도
    정답을 찾는 부분집합의 지표를 함께 출력한다.
    """
    cur.execute("""SELECT chunk_id FROM eval.golden_chunks
                    WHERE gold_id = %s AND chunk_id IS NOT NULL""", (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 정답조문(cur, 근거: list[dict]) -> set[int]:
    """정답근거 -> `doc_articles.article_id` 집합. 참조 확장은 조 단위라 청크로는 잴 수 없다."""
    out: set[int] = set()
    for g in 근거 or []:
        조 = re.match(r"(제\d+조(?:의\d+)?)", g.get("조번호") or "")
        if not (g.get("doc_id") and 조):
            continue
        cur.execute("SELECT article_id FROM corpus.doc_articles WHERE doc_id=%s AND 조번호=%s",
                    (g["doc_id"], 조.group(1)))
        out |= {r[0] for r in cur.fetchall()}
    return out


def 골든셋_불러오기(cur, gold_chunks: bool) -> tuple[list[문항], list[문항], set[int]]:
    """골든셋을 읽어 문항별 정답 청크를 찾는다.

    반환: (평가할 문항, 정답을 못 찾아 제외한 문항, 원문 부분일치로 정답을 찾은 gold_id 집합).
    제외한 문항은 출력으로 알린다 — 말없이 빼면 지표가 부풀려진다. 평가할 문항이 0건이면 종료한다.
    """
    cur.execute(f"""SELECT gold_id, 세트, 질문, 정답근거, 사업명 FROM eval.golden_set
                     WHERE 정답근거 IS NOT NULL AND {기존세트_SQL} ORDER BY gold_id""")
    rows = cur.fetchall()

    데이터, 정답미확인 = [], []
    부분일치: set[int] = set()
    for gid, 세트, q, 근거, 사업 in rows:
        구 = 정답청크(cur, 근거)
        if 구:
            부분일치.add(gid)
        정답 = 정답청크_고정(cur, gid) if gold_chunks else 구
        (데이터 if 정답 else 정답미확인).append(문항(gid, 세트, q, 정답, 사업, 근거))
    출처 = "eval.golden_chunks 고정매핑" if gold_chunks else "원문 부분일치"
    print(f"골든셋 {len(rows)}문항 중 정답 청크를 찾은 것 {len(데이터)}건, "
          f"못 찾은 것 {len(정답미확인)}건  ({출처})")
    if 정답미확인:
        print("  못 찾은 문항 (평가 제외):")
        for m in 정답미확인[:8]:
            print(f"    gold_id={m.gold_id} [{m.세트}] {m.질문[:52]}")
        if len(정답미확인) > 8:
            print(f"    ... 외 {len(정답미확인)-8}건")
    if not 데이터:
        sys.exit("정답 청크를 하나도 못 찾았다. 매칭 규칙을 고칠 것.")
    return 데이터, 정답미확인, 부분일치


def v2_불러오기(cur, 세트: str, 사업: str | None, L3: str) -> tuple[list[문항], list[문항]]:
    """v2 문항을 읽는다. 정답 = 결정 근거의 조문과 그 청크(근거 단위가 조라서 원문 부분일치를 쓰지 않는다).

    반환: (평가할 문항, 평가 제외 문항). 평가 제외는 결정 근거가 L3 가상 규정뿐이어서 검색으로 찾을 조문이 없는 문항이다.
    L3 는 검색하지 않고 org 별로 통째로 넣으므로(retrieve.py FILTER) 이 문항은 L3 연결 org 경로에서 따로 잰다.
    문항 본문은 출력하지 않는다(docs/5_룰/5-9_검증과_측정.md).
    """
    조건, 인자 = "세트 = %s", [세트]
    if 사업:
        조건 += " AND 사업명 = %s"
        인자.append(사업)
    if L3 != "전체":
        조건 += " AND coalesce((문항메타->>'L3')::boolean, false) = %s"
        인자.append(L3 == "만")
    cur.execute(f"""SELECT gold_id, 세트, 질문, 정답근거, 사업명, coalesce((문항메타->>'L3')::boolean, false), 비목, 문항메타
                      FROM eval.golden_set WHERE {조건} ORDER BY gold_id""", 인자)
    rows = cur.fetchall()
    if not rows:
        sys.exit(f"세트 {세트!r} (사업 {사업!r}) 문항이 없다. scripts/golden_v2_load.py 를 먼저 실행한다.")

    데이터, 제외 = [], []
    for gid, 이름, q, 근거, 사업명, l3, 비목, 메타 in rows:
        결정 = [g for g in 근거 or [] if "결정" in (g.get("역할") or "")]
        cur.execute("""SELECT a.article_id FROM corpus.doc_articles a
                         JOIN unnest(%s::text[], %s::text[]) AS k(doc_id, 조번호)
                           ON a.doc_id = k.doc_id AND a.조번호 = k.조번호""",
                    ([g["doc_id"] for g in 결정], [g["조번호"] for g in 결정]))
        조문 = frozenset(r[0] for r in cur.fetchall())
        cur.execute("SELECT chunk_id FROM corpus.chunks WHERE article_id = ANY(%s)", (list(조문),))
        정답 = {r[0] for r in cur.fetchall()}
        (데이터 if 조문 else 제외).append(문항(gid, 이름, q, 정답, 사업명, 근거, 조문, l3, 비목, 메타))
    print(f"세트 {세트}{f' · 사업 {사업}' if 사업 else ''}: {len(rows)}문항 · 평가 {len(데이터)} · 평가 제외(결정 근거가 코퍼스에 없음) {len(제외)}")
    if not 데이터:
        sys.exit("평가할 문항이 없다.")
    return 데이터, 제외


# ── 검색 실행 ────────────────────────────────────────────────────────────────
def 검색_실행(cur, 데이터: list[문항], a) -> 검색결과:
    """문항마다 dense·BM25·RRF 순위를 구한다. --overlap / --참조확장 이면 그 지표에 필요한 값도 모은다."""
    print(f"\n질문 {len(데이터)}건 임베딩 (KURE-v1, CPU) ...", flush=True)
    t = time.time()
    retrieve.워밍업()
    결과 = 검색결과(벡터들=[retrieve.임베딩(d.질문) for d in 데이터])
    print(f"  {time.time()-t:.0f}초\n")

    for d, vec in zip(데이터, 결과.벡터들):
        사업 = 평가용_사업명(d.사업명)
        dense = [c for c, _ in retrieve.dense(cur, vec, k=a.k, 사업명=사업, 사업필터=a.사업필터)]
        bm = retrieve.sparse(cur, d.질문, k=a.k, 사업명=사업, 사업필터=a.사업필터)
        결과.dense.append((dense, d.정답))
        결과.bm25.append((bm, d.정답))
        결과.rrf.append((retrieve.rrf([dense, bm])[:a.k], d.정답))
        if a.overlap:
            결과.겹침.append(len(set(dense[:20]) & set(bm[:20])))
        if a.참조확장:
            top5 = 결과.rrf[-1][0][:5]
            확장조문, _, _ = retrieve.참조확장_수집(cur, top5)
            # 조 단위로 맞춘다 — top-5 청크의 article_id + 참조 확장 article_id
            cur.execute("SELECT article_id FROM corpus.chunks WHERE chunk_id = ANY(%s)",
                        (top5,))
            top5_조문 = [r[0] for r in cur.fetchall()]
            결과.참조확장.append((top5_조문, top5_조문 + 확장조문, set(d.결정조문) or 정답조문(cur, d.근거)))
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
        # 문항 수가 바뀌면 hit율을 직접 비교할 수 없다 — 고정 매핑으로 늘어난 문항은 첨부
        # (별표·별지) 근거라 난이도가 다르다. 원문 부분일치로 찾는 문항 집합으로 맞춰 다시 계산한다.
        부분 = [r for r, d in zip(결과.rrf, 데이터) if d.gold_id in 부분일치]
        새것 = [(d.gold_id, d.세트) for d in 데이터 if d.gold_id not in 부분일치]
        print(f"\n[부분일치 부분집합] 위는 {len(데이터)}문항 기준이다. "
              f"원문 부분일치 run 과 비교하려면 아래 행을 본다 — 같은 문항집합이다.")
        print(_지표행(f"RRF({len(부분)})", 부분, ks))
        print(f"  고정매핑으로 새로 평가된 문항 {len(새것)}건: {새것}")

    print("\n세트별 RRF hit@%d" % a.k)
    for 세트 in sorted({d.세트 for d in 데이터}):
        부분 = [r for r, d in zip(결과.rrf, 데이터) if d.세트 == 세트]
        print(f"  {세트:8} {len(부분):3}건  {지표(부분, [a.k])['hit@'+str(a.k)]:5.1f}%")


def 보고_dangling(cur, 데이터: list[문항], 결과: 검색결과, a) -> None:
    """top-5 청크에서 출발한 미해결 참조가 있는 문항 — 판정에서 검증코드 DANGLING_WARN 이 붙을 수 있는 문항이다."""
    빈, 있음 = 0, []
    for d, row in zip(데이터, 결과.rrf):
        _, _, 미해결참조 = retrieve.참조확장_수집(cur, row[0][:5])
        if 미해결참조:
            있음.append((d.gold_id, 미해결참조))
        else:
            빈 += 1
    print()
    print(f"[dangling] {len(데이터)}문항 중 미해결 참조 있음 {len(있음)}건 / 없음 {빈}건 "
          f"(사업필터={'켬' if a.사업필터 else '끔'})")
    for gid, 미해결참조 in 있음[:10]:
        print(f"    gold_id={gid:3}  {len(미해결참조)}건  {미해결참조[:3]}")
    if len(있음) > 10:
        print(f"    ... 외 {len(있음)-10}문항")


def 보고_필터비교(cur, 데이터: list[문항], 결과: 검색결과, a, ks: list[int]) -> None:
    """(--필터비교) 사업 필터 on/off 를 같은 임베딩으로 짝지어 비교하고, (--사업필터) 사업 지정 문항만의 hit@5 를 출력한다.

    사업이 지정된 문항이 없으면 아무것도 출력하지 않는다.
    """
    지정idx = [i for i, d in enumerate(데이터) if 평가용_사업명(d.사업명)]
    if a.필터비교 and 지정idx:
        # 필터 on/off 를 같은 임베딩으로 짝지어 잰다 — 따로 두 번 돌리면 CPU 부하
        # 차이가 섞여 어느 쪽이 원인인지 가릴 수 없다. 공통 문항은 사업명이 NULL 이라
        # 필터가 no-op — 분모에서 뺀다.
        켬 = []
        for i in 지정idx:
            d = 데이터[i]
            사업 = 평가용_사업명(d.사업명)
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


# 미스 원인 분류 — hit@5 를 놓친 문항을 네 가지 원인으로 나눈다. "검색이 나쁘다" 는 진단이 아니다.
#   필터제외      정답 청크가 사전 필터를 통과하지 못한다 (status·scope·적용대상·사업명)
#                 -> 검색기를 아무리 고쳐도 안 잡힌다. 태깅·적재 쪽 문제다
#   정답청크없음  정답 근거에 해당하는 청크 자체가 없다 (첨부 미청킹 등)
#                 -> 코퍼스 문제. 청킹·룰 테이블 쪽
#   후보미포함    필터는 통과하는데 dense 전체 순위가 k 밖이다
#                 -> 임베딩/질의 표현 문제. 리랭커로도 구할 수 없다
#   순위밖        후보 안에는 있는데 top-5 밖이다
#                 -> 결합·가중 문제. 검색기 튜닝으로 줄일 수 있는 구간은 여기뿐이다
_필터통과_SQL = f"""SELECT chunk_id FROM corpus.chunks
                    WHERE chunk_id = ANY(%(g)s) AND {retrieve.FILTER}"""

_순위_SQL = f"""SELECT count(*) + 1 FROM corpus.chunks
                 WHERE {retrieve.FILTER}
                   AND (embedding <=> %(v)s::extensions.vector(1024))
                     < (SELECT embedding <=> %(v)s::extensions.vector(1024)
                          FROM corpus.chunks WHERE chunk_id = %(c)s)"""


def 보고_진단(cur, 데이터: list[문항], 결과: 검색결과, a, 정답미확인: list[문항]) -> None:
    """(--진단) hit@5 미스를 원인별로 센다. 원인 정의는 위 주석 참조."""
    원인 = Counter()
    사례: dict[str, list] = {}
    for d, (순위, _), vec in zip(데이터, 결과.rrf, 결과.벡터들):
        if any(c in d.정답 for c in 순위[:5]):
            원인["적중"] += 1
            continue
        if not d.정답:
            원인["정답청크없음"] += 1
            사례.setdefault("정답청크없음", []).append((d.gold_id, d.세트, d.사업명))
            continue
        cur.execute(_필터통과_SQL, {"g": list(d.정답)})
        필터통과 = [r[0] for r in cur.fetchall()]
        if not 필터통과:
            원인["필터제외"] += 1
            사례.setdefault("필터제외", []).append((d.gold_id, d.세트, d.사업명))
            continue
        최선 = min((cur.execute(_순위_SQL, {"v": vec, "c": c}).fetchone()[0]) for c in 필터통과)
        키 = "순위밖" if 최선 <= a.k else "후보미포함"
        원인[키] += 1
        사례.setdefault(키, []).append((d.gold_id, d.세트, d.사업명, 최선))
    n = sum(원인.values())
    print(f"\n[진단] hit@5 미스 원인 분류 (n={n} · 사업필터={'켬' if a.사업필터 else '끔'})")
    for k in ("적중", "순위밖", "후보미포함", "필터제외", "정답청크없음"):
        if 원인[k]:
            print(f"  {k:6} {원인[k]:3}건 ({원인[k]/n*100:5.1f}%)")
    for k in ("필터제외", "정답청크없음", "후보미포함", "순위밖"):
        if 사례.get(k):
            print(f"    {k}: {사례[k][:12]}")
    if 정답미확인:
        # 평가 분모 밖이라 위 백분율에는 안 들어간다. 코퍼스 누락이므로 따로 센다.
        print(f"  (평가 제외 {len(정답미확인)}건 — 정답 청크가 코퍼스에 없다: "
              f"{[(m.gold_id, m.세트) for m in 정답미확인]})")
    if 사례.get("필터제외"):
        print("    필터제외 상세 — 어느 조건에서 막혔나:")
        막힌 = {x[0] for x in 사례["필터제외"]}
        필터진단(cur, [c for d in 데이터 if d.gold_id in 막힌 for c in d.정답])


def 필터진단(cur, chunk_ids: list[int]) -> None:
    """`필터제외` 로 분류된 청크가 어느 조건에서 막혔는지 하나씩 본다."""
    조건 = [("status", "status='active'"), ("parse_quality", "parse_quality='high'"),
            ("retrieval_scope", "retrieval_scope='검색대상'"), ("layer", "layer IN ('L1','L2')"),
            ("적용대상", "적용대상 IN ('창업기업','공통')"), ("embedding", "embedding IS NOT NULL")]
    for 이름, 절 in 조건:
        cur.execute(f"SELECT count(*) FROM corpus.chunks WHERE chunk_id = ANY(%s) AND NOT ({절})",
                    (chunk_ids,))
        n = cur.fetchone()[0]
        if n:
            print(f"      {이름:16} 에서 {n}건 탈락")


def 근거단위_포함률(데이터: list[문항], 결과: 검색결과) -> dict:
    """결정 근거 포함률(상위 5 + 참조 확장). 근거 단위 = 조문, 문항 단위 = 그 문항의 결정 근거 전부 포함."""
    def 묶음(idx: list[int]) -> dict:
        분모 = sum(len(데이터[i].결정조문) for i in idx)
        상위5, 확장, 문항전부 = 0, 0, 0
        for i in idx:
            top5, 합, 정답 = 결과.참조확장[i]
            상위5 += len(set(top5) & 정답)
            확장 += len(set(합) & 정답)
            문항전부 += 정답 <= set(합)
        return {"문항": len(idx), "근거": 분모, "상위5": 상위5, "상위5_확장": 확장, "문항전부": 문항전부,
                "근거단위_pct": round(확장 / 분모 * 100, 1) if 분모 else None,
                "문항단위_pct": round(문항전부 / len(idx) * 100, 1) if idx else None}
    전체 = list(range(len(데이터)))
    return {"전체": 묶음(전체), "L3아님": 묶음([i for i in 전체 if not 데이터[i].L3]),
            "L3": 묶음([i for i in 전체 if 데이터[i].L3])}


def 보고_근거단위(포함: dict) -> None:
    print("\n[결정 근거 포함률] 상위 5 + 참조 확장 (근거 단위 = 조문, 문항 단위 = 결정 근거 전부)")
    for 이름, g in 포함.items():
        if g["문항"]:
            print(f"  {이름:6} {g['문항']:3}문항 · 근거 {g['근거']:3}개 · 상위5 {g['상위5']:3} · 상위5+확장 {g['상위5_확장']:3} "
                  f"({g['근거단위_pct']}%) · 문항 전부 {g['문항전부']:3} ({g['문항단위_pct']}%)")


def 결과_기록(데이터: list[문항], 결과: 검색결과, a, ks: list[int], 평가제외: list[문항] | None = None) -> None:
    """(--기록) 지표와 문항별 hit@5 를 `eval_store.기록()` 으로 eval.runs / eval.run_items 에 쓴다.

    `설정` 은 이 숫자가 어떤 조건에서 나왔는지 답할 수 있는 유일한 필드라 반드시 채운다.
    `--세트` 면 문항별 결정 조문과 포함 조문, 평가 제외 문항(적중 NULL)도 남긴다. 문항 본문은 남기지 않는다.
    """
    import eval_store
    지표들 = {f"{이름}.{k}": v
              for 이름, res in (("dense", 결과.dense), ("BM25", 결과.bm25), ("RRF", 결과.rrf))
              for k, v in 지표(res, ks).items()}
    if a.overlap and 결과.겹침:
        지표들["겹침평균_top20"] = sum(결과.겹침) / len(결과.겹침)
        지표들["겹침률_top20"] = sum(결과.겹침) / len(결과.겹침) / 20 * 100
    필터 = "on" if a.사업필터 else "off"
    정답출처 = "golden_chunks" if a.gold_chunks else "원문부분일치"
    if a.세트:
        지표들["결정근거"] = 근거단위_포함률(데이터, 결과)
        제외수 = len(평가제외 or [])
        run_id = eval_store.기록(
            {"종류": "retrieval",
             "설정": {"검색기": "dense+BM25 -> RRF", "세트": a.세트, "사업": a.사업, "L3": a.L3,
                      "W_DENSE": retrieve.W_DENSE, "W_SPARSE": retrieve.W_SPARSE,
                      "RRF_K": retrieve.RRF_K, "후보K": a.k, "참조확장깊이": retrieve.깊이,
                      "사업필터": bool(a.사업필터), "질의": "대화.첫질문", "정답출처": "결정 근거 조문",
                      "임베딩": "KURE-v1/cpu"},
             "문항수": len(데이터) + 제외수, "지표": 지표들,
             "라벨": f"eval_retrieval 세트={a.세트} 사업={a.사업 or '전체'} L3={a.L3} 사업필터={필터}",
             "비고": (f"조건: 세트={a.세트} · 사업={a.사업 or '전체'} · L3={a.L3} · 평가 {len(데이터)}문항 + 평가 제외 {제외수}문항 · "
                      "질의=첫질문 · 라벨은 AI, 사람 미검수. 세트가 다른 run 과 hit@k 를 직접 비교하지 않는다.")},
            [{"gold_id": d.gold_id, "예측": "hit" if any(c in r[1] for c in r[0][:5]) else "miss",
              "정답": "hit", "적중": any(c in r[1] for c in r[0][:5]),
              "원출력": {"top5": r[0][:5], "세트": d.세트, "L3": d.L3, "결정조문": sorted(d.결정조문),
                         "상위5조문": sorted(set(x[0])), "포함조문": sorted(set(x[1]) & x[2])}}
             for d, r, x in zip(데이터, 결과.rrf, 결과.참조확장)]
            + [{"gold_id": d.gold_id, "예측": "평가제외", "정답": "hit", "적중": None,
                "원출력": {"세트": d.세트, "L3": d.L3, "사유": "결정 근거가 코퍼스에 없음(L3 가상 규정)"}}
               for d in 평가제외 or []])
        print(f"\n[기록] eval.runs run_id={run_id} (종류=retrieval, {len(데이터) + 제외수}문항 연결)")
        return
    run_id = eval_store.기록(
        {"종류": "retrieval",
         "설정": {"검색기": "dense+BM25 -> RRF",
                  "W_DENSE": retrieve.W_DENSE, "W_SPARSE": retrieve.W_SPARSE,
                  "RRF_K": retrieve.RRF_K, "후보K": a.k, "참조확장깊이": retrieve.깊이,
                  "사업필터": bool(a.사업필터),
                  "정답출처": 정답출처,
                  "임베딩": "KURE-v1/cpu"},
         "문항수": len(데이터), "지표": 지표들,
         "라벨": f"eval_retrieval 사업필터={필터}"
                 f" 정답={'고정' if a.gold_chunks else '원문부분일치'}",
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
    """(--참조확장) 조 단위 커버리지: 정답 조문을 찾은 문항 중 top-5(+참조 확장)가 정답 조문을 포함한 비율."""
    정답있음 = [row for row in 참조확장res if row[2]]

    def cov(idx):
        if not 정답있음:
            return 0.0
        맞 = sum(1 for row in 정답있음 if set(row[idx]) & row[2])
        return 맞 / len(정답있음) * 100
    print(f"\n[참조확장] 조 단위 커버리지 (정답 조문을 찾은 {len(정답있음)}/{len(참조확장res)}문항 기준)")
    print(f"  top-5 만          {cov(0):5.1f}%")
    print(f"  top-5 + 참조확장  {cov(1):5.1f}%")
    추가 = sum(len(set(row[1]) - set(row[0])) for row in 참조확장res) / len(참조확장res)
    print(f"  참조확장이 더한 조  평균 {추가:.1f}개/문항")


# ── 후보 커버율 (--candidates, P5-1) ─────────────────────────────────────────
_L3_문서 = "L3_기관규정문"                # 정답 근거의 가상 문서. golden_v2_load._L3_문서 와 같다
_후보표시 = {"ontology": "온톨로지", "search": "검색", "both": "병합"}
_놓친유형 = ("계층 상속", "준용", "비목 경계", "주체", "사건", "표", "별지", "그 밖")


def 입력글(m: 문항, 입력: str) -> str:
    """화면 첫 입력에 넣을 글. `대화` 는 첫질문 뒤에 각본 답(사용자 진술)을 잇는다."""
    if 입력 != "대화":
        return m.질문
    return " ".join([m.질문, *((m.메타 or {}).get("답들") or [])])


def _정답_비목(비목: str | None) -> str | None:
    """정답 라벨의 비목 → 화면 비목 10종. 10종 밖이거나 없으면 None(그 문항은 추정값으로 재고 따로 센다)."""
    from server._common import 비목_ENUM
    from server.catalog import _비목_표준화
    값 = re.sub(r"\(.*?\)", "", 비목 or "").strip()
    값 = _비목_표준화({"창업활동자금": "창업활동비"}.get(값, 값))   # 진행상황 E1: 화면 비목 창업활동비 = 비목:창업활동자금/*
    return 값 if 값 in 비목_ENUM else None


def _L3_행(m: 문항) -> list[dict]:
    """기관규정문 → 문맥_구성 입력 행. 연결을 확인하기 전 문서(concept_ids NULL)와 같고, L3 연결 화면(P4-5)이 없어 미연결만 있다."""
    글 = (m.메타 or {}).get("기관규정문")
    if not m.L3 or not 글:
        return []
    조 = re.search(r"제\s*\d+\s*조(?:의\s*\d+)?", 글)
    return [{"article_id": -m.gold_id, "조번호": 조.group(0).replace(" ", "") if 조 else "제1조", "조제목": None,
             "본문": 글, "concept_ids": None}]


def 후보_문맥(cur, m: 문항, 변환: dict, 비목: str | None, 세목: str | None, 종류들: tuple[str, ...], 글: str) -> dict:
    """후보 집합별 문맥. orchestrate._온톨로지단계 · _새_검색단계 와 같은 함수를 같은 값으로 부른다. 조회 오류는 위로 올린다."""
    import ontology_context
    import ontology_lookup
    import orchestrate
    조회, 노드없음 = None, None
    if 비목:
        입력 = orchestrate.게이트_입력(변환.get("금액"), (변환.get("수치") or {}).get("부가세구분"), None)
        try:
            조회 = orchestrate.온톨로지_조회(cur, m.사업명, 비목, 세목, 입력, 확정=True, 품목=변환.get("품목"))
        except LookupError as e:
            노드없음 = str(e)
    근거행 = 조회["근거행"] if 조회 else []
    공통 = {"대상노드": 조회["노드들"] if 조회 else set(), "l3": _L3_행(m), "schema": ontology_lookup._스키마(),
          "자금": 조회.get("자금") if 조회 else None}
    검색 = None
    if "ontology" not in 종류들 or len(종류들) > 1:
        검색, 오류 = orchestrate._새_검색_스레드(변환.get("질의문") or 글, m.사업명)
        if 오류:
            raise RuntimeError(f"검색 실패: {오류}")
    문맥들 = {}
    for 종류 in 종류들:
        문맥들[종류] = ontology_context.문맥_구성(
            cur, 근거행=[] if 종류 == "search" else 근거행, 검색=None if 종류 == "ontology" else 검색, **공통)
    return {"문맥들": 문맥들, "비목_id": 조회["대상"]["비목_id"] if 조회 else None, "노드없음": 노드없음}


def 문맥_조문(cur, 문맥: dict) -> tuple[dict[tuple[str, str], set], bool]:
    """문맥에 실제로 남은 항목 → ({(doc_id, 조번호): 항 키 집합}, L3 조문 있음). 키 None 은 항으로 나뉘지 않은 조 전체다."""
    표: dict[tuple[str, str], set] = {}
    조문, 청크, l3있음 = {}, {}, False
    for _, 묶음 in 문맥["블록"]:
        for 종류, id_, _헤더, _조각, 키 in 묶음:
            if 종류 == "l3":
                l3있음 = True
            else:
                (조문 if 종류 == "article" else 청크).setdefault(id_, []).append(키)
    if 조문:
        for aid, doc, 조 in cur.execute('SELECT article_id, doc_id, "조번호" FROM corpus.doc_articles WHERE article_id = ANY(%s)',
                                       (list(조문),)).fetchall():
            표.setdefault((doc, 조), set()).update(조문[aid])
    if 청크:
        for cid, doc, 조 in cur.execute('SELECT chunk_id, doc_id, "조번호" FROM corpus.chunks WHERE chunk_id = ANY(%s)',
                                       (list(청크),)).fetchall():
            표.setdefault((doc, 조), set()).update(청크[cid])
    return 표, l3있음


def 근거_포함(m: 문항, 표: dict, l3있음: bool) -> dict:
    """결정 근거를 조 · 항 단위로 대조한다. 근거 항호가 항 기호가 아니면(`제2호` 등) 조 단위로 본다.

    반환: 근거(조·항) 분모 · 포함, 조 분모 · 포함, L3 근거 분모 · 포함, 문항 전부 포함(조·항), 놓친 근거.
    """
    import ontology_context
    결정 = [g for g in m.근거 or [] if "결정" in (g.get("역할") or "")]
    조항 = {(g["doc_id"], g["조번호"], ontology_context.항호_키(g.get("항호")), g.get("항호")) for g in 결정 if g["doc_id"] != _L3_문서}
    조들 = {(d, 조) for d, 조, _, _ in 조항}
    l3근거 = sum(1 for g in 결정 if g["doc_id"] == _L3_문서)

    def 있음(d, 조, 키) -> bool:
        키들 = 표.get((d, 조))
        if 키들 is None:
            return False
        return 키 is None or None in 키들 or any((k or "").split("#")[0] == 키 for k in 키들)
    놓침 = sorted({(d, 조, 항호 or "") for d, 조, 키, 항호 in 조항 if not 있음(d, 조, 키)})
    return {"근거": len(조항), "포함": len(조항) - len(놓침), "조": len(조들), "조포함": sum(1 for x in 조들 if x in 표),
            "L3근거": 1 if l3근거 else 0, "L3포함": 1 if l3근거 and l3있음 else 0,
            "전부": not 놓침 and (not l3근거 or l3있음), "놓침": 놓침}


def 놓친_분류(cur, 놓침: list[tuple], 비목_id: str | None, 사업: str | None) -> list[dict]:
    """놓친 근거 → 유형(5-9): 별지 · 표(조번호 · 항호), 준용(준용 · 위임 관계의 도착 조, 연결종류를 함께 적는다),
    계층 상속 · 비목 경계 · 주체 · 사건(그 조를 근거로 갖는 개념 · 규범의 노드), 그 밖(온톨로지에 연결 없음). 앞의 것이 먼저다."""
    import ontology_lookup
    모든비목 = ontology_lookup.모든비목
    키표 = {v: k for k, v in (ontology_lookup._스키마().get("문서키") or {}).items()}
    사업들 = [사업] if 사업 else []
    out = []
    for doc, 조, 항호 in 놓침:
        행 = {"doc_id": doc, "조번호": 조, "항호": 항호, "유형": "그 밖", "연결종류": None}
        out.append(행)
        if 조.startswith("별지") or 항호.startswith("/"):
            행["유형"] = "별지"
            continue
        if 조.startswith(("별표", "붙임", "표")) or "표" in 항호:
            행["유형"] = "표"
            continue
        i = ontology_lookup._조문ID_만들기(doc, 조, None, 키표)
        if i is None:
            continue
        준용 = cur.execute("""SELECT "연결종류" FROM corpus.concept_relations
                              WHERE "관계" IN ('준용', '위임') AND left(dst_id, length(%s)) = %s
                                AND ("적용사업" IS NULL OR "적용사업" = ANY(%s)) LIMIT 1""", (i, i, 사업들)).fetchone()
        if 준용:
            행.update(유형="준용", 연결종류=준용[0])
            continue
        노드들 = {r[0] for r in cur.execute(
            """SELECT owner_id FROM corpus.provenance WHERE owner_kind = '개념' AND doc_id = %s AND "조번호" = %s
               UNION
               SELECT unnest(n."대상") FROM corpus.provenance p JOIN corpus.norms n ON n.norm_id = p.owner_id
                WHERE p.owner_kind = '규범' AND p.doc_id = %s AND p."조번호" = %s""", (doc, 조, doc, 조)).fetchall()}
        분류노드 = [n for n in 노드들 if n.startswith(("비목:", "세목:", "품목:"))]
        같은비목 = any(n == 모든비목 or n == 비목_id or (비목_id and 비목_id in {p for _, p in ontology_lookup._상위_경로(cur, n, 사업들)})
                    for n in 분류노드)
        if 같은비목:
            행["유형"] = "계층 상속"
        elif 분류노드:
            행["유형"] = "비목 경계"
        elif any(n.startswith("주체:") for n in 노드들):
            행["유형"] = "주체"
        elif any(n.startswith(("절차:", "시간:")) for n in 노드들):
            행["유형"] = "사건"
    return out


def 커버율_집계(결과행: list[dict], 종류: str) -> dict:
    """문항별 대조 → 묶음별 지표. 분모는 근거(조·항) · 조 · 문항 셋을 다 적는다. 변환 실패 문항은 분모에서 빼고 따로 센다."""
    def 묶음(행들: list[dict]) -> dict:
        합 = {k: sum(r["후보"][종류][k] for r in 행들) for k in ("근거", "포함", "조", "조포함", "L3근거", "L3포함")}
        전부 = sum(1 for r in 행들 if r["후보"][종류]["전부"])

        def 률(a, b):
            return round(a / b * 100, 1) if b else None
        return {"문항": len(행들), **합, "문항전부": 전부, "근거단위_pct": 률(합["포함"], 합["근거"]),
                "조단위_pct": 률(합["조포함"], 합["조"]), "문항단위_pct": 률(전부, len(행들))}
    재기 = [r for r in 결과행 if not r.get("실패")]
    불일치 = [r for r in 재기 if r.get("비목불일치")]
    return {"전체": 묶음(재기), "L3아님": 묶음([r for r in 재기 if not r["L3"]]), "L3": 묶음([r for r in 재기 if r["L3"]]),
            "추정비목_다름": 묶음(불일치)}


def 커버율_실행(cur, 데이터: list[문항], a) -> dict[str, list[dict]]:
    """문항마다 질의 변환 1회(LLM) 뒤 입력 비목별로 후보를 구성해 대조한다."""
    from llm_qwen import LLM실패
    from server.models import 정규화요청
    from server.routes_normalize import _온톨로지_정규화
    종류들 = ("ontology", "search", "both") if a.candidates == "both" else (a.candidates,)
    방식들 = ("추정", "정답") if a.입력비목 == "둘다" else (a.입력비목,)
    결과행: dict[str, list[dict]] = {x: [] for x in 방식들}
    for n, m in enumerate(데이터, 1):
        글 = 입력글(m, a.입력)
        try:
            변환 = (_온톨로지_정규화(정규화요청(질문=글, 사업명=m.사업명)) or (None,))[0]
        except LLM실패 as e:
            변환, 실패 = None, f"LLM실패: {str(e)[:80]}"
        else:
            실패 = None if 변환 else "온톨로지 경로 대상이 아님"
        후보들 = (변환 or {}).get("분류후보") or []
        첫후보 = 후보들[0] if 후보들 else {}
        정답 = _정답_비목(m.비목)
        for 방식 in 방식들:
            기본 = {"gold_id": m.gold_id, "세트": m.세트, "L3": m.L3}
            if 실패:
                결과행[방식].append({**기본, "실패": 실패})
                continue
            if 방식 == "정답" and 정답:
                비목 = 정답
                세목 = next((c.get("세목") for c in 후보들 if c.get("비목") == 정답), None)
            else:
                비목, 세목 = 첫후보.get("비목"), 첫후보.get("세목")
            r = 후보_문맥(cur, m, 변환, 비목, 세목, 종류들, 글)
            대조 = {}
            for 종류, 문맥 in r["문맥들"].items():
                표, l3있음 = 문맥_조문(cur, 문맥)
                대조[종류] = {**근거_포함(m, 표, l3있음), "항목수": 문맥["항목수"], "글자수": 문맥["글자수"],
                            "빠진수": 문맥["빠진수"], "L3_초과": 문맥["L3_초과"], "온톨로지항목수": 문맥["온톨로지항목수"]}
            마지막 = 대조[종류들[-1]]
            결과행[방식].append({**기본, "후보": 대조, "비목": 비목, "세목": 세목,
                              "비목불일치": bool(정답) and 첫후보.get("비목") != 정답, "정답비목없음": not 정답,
                              "추정없음": not 첫후보, "노드없음": r["노드없음"],
                              "놓친유형": 놓친_분류(cur, 마지막["놓침"], r["비목_id"], m.사업명)})
        print(f"  {n}/{len(데이터)} gold_id={m.gold_id} 변환={'실패' if 실패 else '완료'} 후보수={len(후보들)}", flush=True)
    return 결과행


def 커버율_보고(결과행: dict[str, list[dict]], a) -> None:
    종류들 = ("ontology", "search", "both") if a.candidates == "both" else (a.candidates,)
    for 방식, 행들 in 결과행.items():
        실패 = [r for r in 행들 if r.get("실패")]
        재기 = [r for r in 행들 if not r.get("실패")]
        print(f"\n[후보 커버율] 입력비목={방식} · 세트 {a.세트} · 사업 {a.사업} · L3 {a.L3} · 질의={a.입력} · 변환 실패 {len(실패)}문항")
        print(f"  추정 후보 없음 {sum(1 for r in 재기 if r['추정없음'])} · 추정 ≠ 정답 비목 {sum(1 for r in 재기 if r['비목불일치'])} · "
              f"정답 비목 10종 밖 {sum(1 for r in 재기 if r['정답비목없음'])} · 노드 없음 {sum(1 for r in 재기 if r['노드없음'])}")
        if not 재기:
            continue
        for 종류 in 종류들:
            집계 = 커버율_집계(재기, 종류)
            for 이름, g in 집계.items():
                if g["문항"]:
                    print(f"  {_후보표시[종류]:4} {이름:6} {g['문항']:3}문항 · 근거(조·항) {g['포함']:3}/{g['근거']:3} ({g['근거단위_pct']}%) · "
                          f"조 {g['조포함']:3}/{g['조']:3} ({g['조단위_pct']}%) · 문항 전부 {g['문항전부']:3} ({g['문항단위_pct']}%) · L3 근거 {g['L3포함']}/{g['L3근거']}")
        유형 = Counter(x["유형"] for r in 재기 for x in r["놓친유형"])
        연결 = Counter(x["연결종류"] for r in 재기 for x in r["놓친유형"] if x["연결종류"])
        print(f"  [놓친 근거 유형 · {_후보표시[종류들[-1]]}] " + " · ".join(f"{k} {유형[k]}" for k in _놓친유형 if 유형[k])
              + (f" · 준용 연결종류 {dict(연결)}" if 연결 else ""))
        빠짐 = sum(r["후보"][종류들[-1]]["빠진수"] for r in 재기)
        print(f"  글자 상한으로 뺀 항목 {빠짐}개 · L3 상한 초과 {sum(1 for r in 재기 if r['후보'][종류들[-1]]['L3_초과'])}문항")


def 커버율_기록(결과행: dict[str, list[dict]], a) -> None:
    """입력 비목별로 eval.runs 1행. 조건 · 분모는 설정 · 지표 · 비고에 남고 날짜는 시작 칸이다. 문항 본문은 남기지 않는다."""
    import eval_store
    import orchestrate
    from ontology_context import 글자_상한
    종류들 = ("ontology", "search", "both") if a.candidates == "both" else (a.candidates,)
    최종 = 종류들[-1]
    for 방식, 행들 in 결과행.items():
        재기 = [r for r in 행들 if not r.get("실패")]
        지표들 = {_후보표시[k]: 커버율_집계(재기, k) for k in 종류들} if 재기 else {}
        지표들["놓친유형"] = dict(Counter(x["유형"] for r in 재기 for x in r["놓친유형"]))
        지표들["변환실패"] = len(행들) - len(재기)
        run_id = eval_store.기록(
            {"종류": "retrieval",
             "설정": {"측정": "후보 커버율", "후보": a.candidates, "입력비목": 방식, "세트": a.세트, "사업": a.사업, "L3": a.L3,
                      "질의": a.입력, "dry": a.dry, "검색상위": 10, "글자상한": 글자_상한, "사업필터": orchestrate.사업필터,
                      "SUDDOE_ONTOLOGY_PROGRAMS": os.environ.get("SUDDOE_ONTOLOGY_PROGRAMS"),
                      "질의변환모델": os.environ.get("SUDDOE_QUERY_MODEL") or os.environ.get("SUDDOE_QWEN_MODEL")},
             "문항수": len(행들), "지표": 지표들,
             "라벨": f"후보 커버율 세트={a.세트} 사업={a.사업} L3={a.L3} 입력비목={방식} 후보={a.candidates}",
             "비고": (f"조건: 세트={a.세트} · 사업={a.사업} · {len(재기)}문항(변환 실패 {len(행들) - len(재기)} 제외) · 입력비목={방식} · 질의={a.입력} · "
                      "분모 = 결정 근거(조·항) · 조 · 문항. 목표 95% 는 근거 단위. 라벨은 AI, 사람 미검수.")},
            [{"gold_id": r["gold_id"], "예측": "변환실패" if r.get("실패") else ("전부포함" if r["후보"][최종]["전부"] else "일부놓침"),
              "정답": "전부포함", "적중": None if r.get("실패") else r["후보"][최종]["전부"], "원출력": r}
             for r in 행들])
        print(f"[기록] eval.runs run_id={run_id} (종류=retrieval, 입력비목={방식}, {len(행들)}문항)")


def 커버율_측정(cur, a) -> None:
    """--candidates 실행. 새 경로 사업으로 환경변수를 잡고 질의 변환 · 후보 함수를 부른다."""
    import eval_store
    if not (a.세트 and a.사업):
        sys.exit("--candidates 는 --세트 와 --사업 이 필요하다")
    os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = a.사업
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if a.dry:                                    # LLM 없이 연결 확인: 질의문 = 입력 글, 분류 후보 없음
        import normalize_run
        원래 = normalize_run.질의변환
        normalize_run.질의변환 = lambda 입력, 레이블, **k: 원래(입력, 레이블, dry=True)
    평가, 제외 = v2_불러오기(cur, a.세트, a.사업, a.L3)
    # 결정 근거가 L3 뿐인 문항도 후보에서는 L3 조문으로 잴 수 있다
    데이터 = sorted([*평가, *제외], key=lambda m: m.gold_id)
    if any("답들" not in (m.메타 or {}) for m in 데이터):
        sys.exit("문항메타에 답들 · 기관규정문이 없다. scripts/golden_v2_load.py 를 다시 실행한다.")
    print(f"후보 커버율은 위 평가 제외 문항도 L3 조문으로 잰다: 전체 {len(데이터)}문항" + (f" 중 {a.limit}개" if a.limit else ""))
    데이터 = eval_store.고르게_뽑기(데이터, a.limit, lambda m: m.L3)
    retrieve.워밍업()
    결과행 = 커버율_실행(cur, 데이터, a)
    커버율_보고(결과행, a)
    if a.기록:
        커버율_기록(결과행, a)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50, help="검색 깊이")
    ap.add_argument("--candidates", choices=("ontology", "search", "both"), default=None,
                    help="새 경로 후보 커버율(5-9): 온톨로지 후보 · 검색 후보 · 병합. both 는 셋 다 잰다. --세트 · --사업 필요")
    ap.add_argument("--입력비목", choices=("추정", "정답", "둘다"), default="둘다",
                    help="(--candidates) 후보 조회에 넣을 비목. 추정 = 질의 변환 첫 후보(화면 기본값), 정답 = 정답 라벨. 5-9 는 둘 다 잰다")
    ap.add_argument("--입력", choices=("첫질문", "대화"), default="첫질문",
                    help="(--candidates) 질의 변환 입력. 기준선(5-9)과 같은 첫질문이 기본, 대화 = 첫질문 + 각본 답")
    ap.add_argument("--limit", type=int, default=None, help="(--candidates) 소량 실행. L3 비율을 맞춰 고르게 뽑는다")
    ap.add_argument("--dry", action="store_true", help="(--candidates) 질의 변환 LLM 없이 연결만 확인한다. 추정 비목 후보가 없다")
    ap.add_argument("--진단", action="store_true",
                    help="hit@5 미스의 원인 분류 — 필터 제외 / 정답 청크 없음 / 후보 미포함 / 순위 밖")
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
    ap.add_argument("--필터비교", dest="필터비교", action="store_true",
                    help="사업 필터 on/off 를 같은 임베딩으로 짝지어 비교")
    ap.add_argument("--dangling", action="store_true",
                    help="top-5 청크에서 출발한 미해결 참조 — 검증코드 DANGLING_WARN 이 붙을 수 있는 문항")
    ap.add_argument("--참조확장", dest="참조확장", action="store_true",
                    help="top-5 + 참조 확장까지 합쳤을 때의 조 단위 커버리지")
    ap.add_argument("--세트", choices=("v2_dev", "v2_test"), default=None,
                    help="정답셋 v2 를 읽는다(golden_v2_load.py 로 적재). 없으면 기존 문항만 읽는다")
    ap.add_argument("--사업", default=None, help="(--세트) 이 사업 문항만")
    ap.add_argument("--L3", choices=("전체", "제외", "만"), default="전체", help="(--세트) L3 문항 포함 여부")
    a = ap.parse_args()
    if a.gold_chunks and a.세트:
        ap.error("--세트 는 --gold-chunks 와 같이 쓸 수 없다")
    if a.세트:
        a.참조확장 = True       # v2 포함률은 상위 5 + 참조 확장 기준이다
    # 검색 깊이(a.k)보다 큰 k 는 hit@a.k 와 같아 의미가 없고, 같은 k 가 두 번 들어가면 표에 열이 겹친다
    ks = sorted({k for k in (1, 5, 10, 20) if k <= a.k} | {a.k})

    # 읽기 전용이라도 트랜잭션을 열어 두면 다른 세션의 DDL 과 락 대기가 생길 수 있어 autocommit 으로 연다.
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        if a.candidates:
            커버율_측정(cur, a)
            return
        if a.세트:
            데이터, 정답미확인 = v2_불러오기(cur, a.세트, a.사업, a.L3)
            부분일치: set[int] = set()
        else:
            데이터, 정답미확인, 부분일치 = 골든셋_불러오기(cur, a.gold_chunks)
        결과 = 검색_실행(cur, 데이터, a)
        보고_기본표(데이터, 결과, 부분일치, a, ks)
        if a.dangling:
            보고_dangling(cur, 데이터, 결과, a)
        보고_필터비교(cur, 데이터, 결과, a, ks)
        if a.overlap:
            보고_겹침(결과.겹침)
        if a.진단:
            보고_진단(cur, 데이터, 결과, a, 정답미확인)
        if a.세트:
            보고_근거단위(근거단위_포함률(데이터, 결과))
        if a.기록:
            결과_기록(데이터, 결과, a, ks, 정답미확인 if a.세트 else None)
        if a.참조확장:
            보고_참조확장(결과.참조확장)


if __name__ == "__main__":
    main()
