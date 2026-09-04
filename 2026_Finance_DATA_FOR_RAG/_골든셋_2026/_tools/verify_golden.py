# -*- coding: utf-8 -*-
"""골든셋 2026 기계검증 — 객관 닻. `eval.golden_set` 적재 모양 그대로 검사한다.

입력: 행 dict 의 JSON **배열**(적재 모양). 각 행은 17컬럼.

무엇을 검사하나 (판정이 맞는지는 «검사하지 않는다» — 뽑기가 맞는지만 본다):
  1. 컬럼 집합·enum — 정답판정 4값 · 세트 4값 · 대상 2값 · 평가범위 '유효' · 채점모드 'full'
  2. 사업명이 corpus.programs 에 실재하는가 (FK)
  3. 정답근거[].doc 가 corpus.documents 에, 조번호가 corpus.doc_articles 에 실재하는가
  4. 원문이 그 조 본문에 있는가 — **대조방식을 셋으로 갈라 기록한다**
       verbatim   : 공백만 정규화하면 연속 부분문자열로 그대로 있다
       페이지마커제거후 : 조문 한가운데 박힌 PDF 페이지번호('- 17 -')만 지우면 연속
       라벨제거후 : '…' 로 끊어 넣은 조각들이 순서대로는 있으나 연속이 아니다 → **실패로 친다**
                    (생략부호가 들어가면 사람이 원문 대조를 못 한다 — 인용정확도 채점이 죽는다)
       실패       : 조각 하나라도 못 찾았다
     🔴 「통과」를 verbatim 으로 읽지 마라 — 방식별 개수를 따로 낸다.
  5. 근거원문(대표 1개)이 정답근거[].원문 중 하나와 일치하는가
  6. extraction='vlm' 문서 인용은 인용신뢰 B 강등 대상으로 경고

통과 기준: 실패 0 · 커버리지 평균 ≥ 0.9.

사용:  python verify_golden.py <골든셋.json> [...]
DB 는 read-only 조회만 한다.
"""
import json
import re
import sys
from collections import Counter

import psycopg

DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"

컬럼 = ["세트", "no", "사업명", "질문", "정답판정", "정답근거", "근거원문", "해야할일",
        "채점대상", "verified", "검수메모", "비목", "입력필드", "적용범위", "대상",
        "평가범위", "채점모드"]
ENUM = {
    "정답판정": {"가능", "조건부", "불가", "판단불가"},
    "세트": {"본세트", "보강", "공식", "적대적"},
    "대상": {"창업기업", "공통"},
    "평가범위": {"유효"},
    "채점모드": {"full"},
}

_ART = re.compile(r"^(제\d+조(?:의\d+)?|부칙 제\d+조|붙임\d+)")


def art_key(조번호: str) -> str:
    m = _ART.match((조번호 or "").strip())
    return m.group(1) if m else (조번호 or "").strip()


def norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("․", "·").replace("‧", "·").replace("・", "·")
    return re.sub(r"\s+", "", s)


_PAGE = re.compile(r"-\s*\d{1,4}\s*-")


def 대조(quote: str, body: str, body_nopage: str | None = None):
    """(방식, matched, total).

    verbatim         : 공백만 정규화해도 연속 부분문자열
    페이지마커제거후 : 본문에서 '- 17 -' 같은 PDF 페이지 번호를 지우면 연속
                       (조문 한가운데 페이지 번호가 박힌 파싱 잔재 — 인용은 원문 그대로다)
    라벨제거후       : '…' 조각이 순서대로는 있으나 연속이 아니다
    실패             : 조각 하나라도 없다
    """
    whole = norm(re.sub(r"[…]+", "", quote))
    if whole and whole in body:
        return "verbatim", len(whole), len(whole)
    if body_nopage is None:
        body_nopage = _PAGE.sub("", body)
    if whole and whole in body_nopage:
        return "페이지마커제거후", len(whole), len(whole)
    frags = [f for f in re.split(r"[…]+", quote) if norm(f)]
    pos, matched, total, ok = 0, 0, 0, True
    for f in frags:
        nf = norm(f)
        total += len(nf)
        idx = body.find(nf, pos)
        if idx >= 0:
            matched += len(nf)
            pos = idx + len(nf)
        else:
            ok = False
    return ("라벨제거후" if ok else "실패"), matched, total


def verify(path):
    rows = json.load(open(path, encoding="utf-8"))
    if not isinstance(rows, list):
        print(f"== {path}\n   ✗ 최상위가 배열이 아니다 — 적재 모양이 아니다")
        return False

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        cur.execute("select doc_id, extraction from corpus.documents")
        docs = dict(cur.fetchall())
        cur.execute("select doc_id, 조번호, 본문 from corpus.doc_articles")
        arts = {(d, a): norm(b) for d, a, b in cur.fetchall()}
        cur.execute("select 사업명 from corpus.programs")
        progs = {r[0] for r in cur.fetchall()}

    fails, warns, covs, 방식 = [], [], [], Counter()
    for r in rows:
        no = r.get("no")
        missing = set(컬럼) - set(r)
        extra = set(r) - set(컬럼)
        if missing:
            fails.append(f"[{no}] 컬럼 누락: {sorted(missing)}")
        if extra:
            fails.append(f"[{no}] 컬럼 초과(적재 시 튕긴다): {sorted(extra)}")
        for k, allowed in ENUM.items():
            if r.get(k) not in allowed:
                fails.append(f"[{no}] {k}={r.get(k)!r} 은 허용값 {sorted(allowed)} 밖")
        if r.get("사업명") not in progs:
            fails.append(f"[{no}] 사업명 FK 실패: {r.get('사업명')!r}")
        if r.get("verified") is not False:
            fails.append(f"[{no}] verified 는 false 여야 한다 (검증은 중앙 몫)")

        for ev in r.get("정답근거") or []:
            doc, 조 = ev.get("doc"), ev.get("조번호")
            key = (doc, art_key(조))
            if doc not in docs:
                fails.append(f"[{no}] 문서 없음: {doc}")
                continue
            if key not in arts:
                fails.append(f"[{no}] 조 없음: {doc} / {조}")
                continue
            if docs[doc] == "vlm":
                warns.append(f"[{no}] extraction=vlm — 인용신뢰 B 강등 대상: {doc}")
            m, mt, tt = 대조(ev.get("원문", ""), arts[key])
            방식[m] += 1
            cov = (mt / tt) if tt else 0.0
            covs.append(cov)
            if m in ("실패", "라벨제거후") or cov < 0.9:
                fails.append(f"[{no}] 대조 {m} · 커버리지 {cov:.2f} — {doc} {조}")
            # 입력필드.대조방식 이 실제 대조 결과와 같아야 한다
            기록 = ((r.get("입력필드") or {}).get("대조방식") or {}).get(조)
            if 기록 and 기록 != m:
                fails.append(f"[{no}] 입력필드.대조방식[{조}]={기록!r} 인데 실제는 {m!r}")

        if r.get("근거원문") and r.get("정답근거"):
            if norm(r["근거원문"]) not in {norm(e.get("원문", "")) for e in r["정답근거"]}:
                fails.append(f"[{no}] 근거원문 이 정답근거[].원문 어디와도 일치하지 않는다")

    print(f"== {path}")
    print(f"   행 {len(rows)} · 인용 {len(covs)} · 평균 커버리지 "
          f"{(sum(covs)/len(covs) if covs else 0):.3f} · 대조방식 {dict(방식)}")
    print(f"   판정 {dict(Counter(r.get('정답판정') for r in rows))} · "
          f"세트 {dict(Counter(r.get('세트') for r in rows))}")
    비목 = Counter(r.get("비목") for r in rows)
    최다 = 비목.most_common(1)[0] if 비목 else ("-", 0)
    print(f"   비목 {len(비목)}종 · 최다 {최다[0]} {최다[1]}/{len(rows)} "
          f"({최다[1]/max(len(rows),1):.1%})")
    for w in warns:
        print("   ⚠ " + w)
    for f in fails:
        print("   ✗ " + f)
    print("   " + ("통과" if not fails else f"실패 {len(fails)}건"))
    return not fails


if __name__ == "__main__":
    sys.exit(0 if all([verify(p) for p in sys.argv[1:]]) else 1)
