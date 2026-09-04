# -*- coding: utf-8 -*-
"""중앙(ai-c5) 검증 — 워커가 낸 골든셋 json 을 «다른 닻» 으로 다시 친다.

워커의 자기검증을 그대로 받지 않는다. 여기서 다시 재는 것:
  A. 적재 가능성  — eval.golden_set 의 컬럼·CHECK·FK 를 실제 DB 에서 읽어 대조
  B. 근거 실재    — 정답근거의 (doc_id, 조번호) 가 corpus.doc_articles 에 있나
  C. 원문 실재    — 정답근거.원문 이 그 조 본문에 문자 그대로 있나 (verbatim / 공백무시 / 라벨제거 / 실패)
  D. 5축 분포     — 판정·비목·이유·대상·출처. 균형 위반을 «위반» 으로 센다
  E. 범위 위반    — 대상='주관기관' · 평가범위 '범위밖*' · 사업명 FK 불일치

🔴 「통과」를 verbatim 으로 읽히게 쓰지 않는다 — 대조방식을 건별로 남기고 합계도 갈라 센다.
🔴 DB 는 읽기만 한다. 적재는 별도 스크립트에서 명시적으로 한다.

    python 중앙_검증_ai-c5.py [경로...]      # 생략하면 _골든셋_2026/*.json 전부
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import _lib.db as db  # noqa: E402

골든셋_DIR = Path(__file__).resolve().parents[1]

# ── 정규화 ────────────────────────────────────────────────────────────────────
_공백 = re.compile(r"\s+")
# HWP→PDF 4분면 표에서 문장 중간에 흘러드는 컬럼 라벨 글자. 인용 쪽은 절대 안 건드린다.
_라벨 = re.compile(r"[기준유의사항증빙서류정의]")


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _압축(s: str) -> str:
    return _공백.sub("", _nfkc(s))


# 조문 본문 한가운데 박히는 페이지 표시("예외적\n- 17 -\n으로 구매가능하다").
# 🔴 코퍼스 파싱 잔재이지 인용자의 잘못이 아니다 — 그래서 방식을 갈라 센다.
_페이지 = re.compile(r"-\s*\d{1,4}\s*-")
# 인용자가 줄인 자리. 생략은 «인용»이 아니므로 통과로 치지 않고 별도 방식으로만 표시한다.
_생략 = re.compile(r"[…]|\.\.\.")


def 대조(원문: str, 본문: str) -> str:
    """원문이 본문 안에 있나. 어떤 방식으로 찾았는지를 돌려준다."""
    if not 원문:
        return "원문없음"
    if _nfkc(원문) in _nfkc(본문):
        return "verbatim"
    if _압축(원문) in _압축(본문):
        return "공백무시"
    본문p = _페이지.sub("", _압축(본문))
    if _압축(원문) in 본문p:
        return "페이지마커제거후"
    if _라벨.sub("", _압축(원문)) in _라벨.sub("", 본문p):
        return "라벨제거후"
    # 생략부호로 끊어 쓴 인용 — 조각이 전부 본문에 순서대로 있으면 «생략인용» 으로 표시한다.
    조각 = [_압축(s) for s in _생략.split(원문) if _압축(s)]
    if len(조각) > 1:
        pos = 0
        for s in 조각:
            i = 본문p.find(s, pos)
            if i < 0:
                break
            pos = i + len(s)
        else:
            return "생략인용"
    return "실패"


# ── A. 적재 스키마 (DB 에서 읽는다 — 문서를 믿지 않는다) ──────────────────────
def 스키마(cur) -> dict:
    cur.execute(
        "select column_name, is_nullable from information_schema.columns "
        "where table_schema='eval' and table_name='golden_set' order by ordinal_position"
    )
    컬럼 = {r[0]: (r[1] == "NO") for r in cur.fetchall()}
    cur.execute("select pg_get_constraintdef(oid) from pg_constraint "
                "where conrelid='eval.golden_set'::regclass")
    제약 = [r[0] for r in cur.fetchall()]
    판정값 = set(re.findall(r"'([^']+)'::text", next(
        (c for c in 제약 if "정답판정" in c), "")))
    cur.execute("select 사업명 from corpus.programs")
    사업 = {r[0] for r in cur.fetchall()}
    return {"컬럼": 컬럼, "판정값": 판정값, "사업명": 사업}


def 검사(cur, 문항: dict, sc: dict) -> list[str]:
    """한 문항의 위반 목록. 빈 리스트면 통과."""
    나쁨: list[str] = []
    for c, 필수 in sc["컬럼"].items():
        if c == "gold_id":
            continue
        if 필수 and 문항.get(c) in (None, ""):
            나쁨.append(f"필수누락:{c}")
    남는키 = set(문항) - set(sc["컬럼"])
    if 남는키:
        나쁨.append(f"스키마밖키:{sorted(남는키)}  → 입력필드 jsonb 안으로")

    if 문항.get("정답판정") not in sc["판정값"]:
        나쁨.append(f"정답판정 CHECK 위반:{문항.get('정답판정')!r}")
    if 문항.get("사업명") not in sc["사업명"]:
        나쁨.append(f"사업명 FK 위반:{문항.get('사업명')!r}")
    if 문항.get("대상") not in ("창업기업", "공통"):
        나쁨.append(f"대상 범위밖:{문항.get('대상')!r} (주관기관 저작 금지)")
    if (문항.get("평가범위") or "").startswith("범위밖"):
        나쁨.append(f"평가범위 범위밖:{문항.get('평가범위')!r}")
    if 문항.get("verified") is not False:
        나쁨.append("verified 는 워커가 true 로 두지 않는다")
    if not isinstance(문항.get("채점대상"), list) or not 문항["채점대상"]:
        나쁨.append("채점대상 배열 비어있음")

    for i, g in enumerate(문항.get("정답근거") or []):
        doc, 조 = g.get("doc"), g.get("조번호")
        if not doc or not 조:
            나쁨.append(f"근거[{i}] doc/조번호 없음")
            continue
        조핵 = re.sub(r"[①-⑳].*$", "", 조).strip()
        cur.execute("select 본문 from corpus.doc_articles "
                    "where doc_id=%s and (조번호=%s or 조번호=%s)", [doc, 조, 조핵])
        행 = cur.fetchall()
        if not 행:
            나쁨.append(f"근거[{i}] 조 부재: {doc} / {조}")
            continue
        방식 = max((대조(g.get("원문", ""), r[0] or "") for r in 행),
                   key=lambda m: ["실패", "원문없음", "생략인용", "라벨제거후", "페이지마커제거후", "공백무시", "verbatim"].index(m))
        g["_대조방식"] = 방식
        if 방식 in ("실패", "원문없음"):
            나쁨.append(f"근거[{i}] 원문 코퍼스에 없음({방식}): {(g.get('원문') or '')[:40]}")
        elif 방식 == "생략인용":
            # 조각은 다 실재하지만 «…» 로 끊은 인용은 사람이 원문 대조를 못 한다.
            나쁨.append(f"근거[{i}] 생략인용(「…」) — 연속 구간으로 다시 떠라: "
                        f"{(g.get('원문') or '')[:40]}")
    if not (문항.get("정답근거") or []):
        나쁨.append("정답근거 0건")
    return 나쁨


# ── D. 5축 분포 ───────────────────────────────────────────────────────────────
def 분포(문항들: list[dict]) -> list[str]:
    n = len(문항들)
    말 = []
    판정 = Counter(q.get("정답판정") for q in 문항들)
    비목 = Counter(q.get("비목") for q in 문항들)
    이유 = Counter()
    for q in 문항들:
        v = (q.get("입력필드") or {}).get("판정이유", q.get("판정이유"))
        이유.update(v if isinstance(v, list) else [v])
    세트 = Counter(q.get("세트") for q in 문항들)
    말.append(f"  문항 {n} · 판정 {dict(판정)} · 세트 {dict(세트)}")
    말.append(f"  비목 {len(비목)}종 (최다 {비목.most_common(1)[0] if 비목 else '-'})")
    말.append(f"  이유 {dict(이유)}")
    if n and 판정.get("가능", 0) < 2:
        말.append(f"  🔴 «가능» 트랩 {판정.get('가능', 0)}건 — 축 규격은 ≥2")
    if len(비목) < 8:
        말.append(f"  🔴 비목 {len(비목)}종 — 축 규격은 ≥8")
    if n and 비목 and 비목.most_common(1)[0][1] / n > 0.30:
        말.append(f"  🔴 한 비목 쏠림 {비목.most_common(1)[0][1]}/{n} — 축 규격은 ≤30%")
    빠진이유 = {"한도초과", "목적외", "증빙미비", "시기위반", "대상외"} - set(이유)
    if 빠진이유:
        말.append(f"  ⚠️ 이유축 미사용: {sorted(빠진이유)}")
    return 말


def main(경로들: list[str]) -> int:
    파일 = [Path(p) for p in 경로들] or sorted(
        p for p in 골든셋_DIR.glob("*.json") if not p.name.startswith("_"))
    if not 파일:
        print("검증할 json 이 없다.")
        return 0
    나쁜문항 = 0
    방식합 = Counter()
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        sc = 스키마(cur)
        for f in 파일:
            문항들 = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(문항들, dict):
                문항들 = 문항들.get("문항") or 문항들.get("items") or []
            print(f"\n=== {f.name}")
            for i, q in enumerate(문항들):
                나쁨 = 검사(cur, q, sc)
                for g in q.get("정답근거") or []:
                    방식합[g.get("_대조방식", "?")] += 1
                if 나쁨:
                    나쁜문항 += 1
                    print(f"  🔴 [{q.get('no', i)}] {(q.get('질문') or '')[:38]}")
                    for m in 나쁨:
                        print(f"       - {m}")
            for line in 분포(문항들):
                print(line)
    print(f"\n── 근거 원문 대조방식 합계: {dict(방식합)}")
    print("   🔴 이 합계를 «verbatim 통과» 로 읽지 마라. verbatim 이 아닌 건 인용 신뢰가 낮다.")
    print(f"── 위반 문항 {나쁜문항}건")
    return 1 if 나쁜문항 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
