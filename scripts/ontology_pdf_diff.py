#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DB 본문과 PDF 원본을 조 단위로 대조해 차이 목록을 쓴다 (docs/5_룰/5-7_원문_추출_절차.md "PDF 대조").

방법: pdfplumber 로 쪽 텍스트 추출 → 조 경계(`제N조(`, `[별지 제②호]`)로 자르기 → 발췌 정규화 뒤 공백을 지우고 문자 단위 diff.
표 조문은 줄 순서가 달라 문자 diff 가 넓게 나오므로 어절 집합도 함께 비교한다. 차이의 종류(셀 풀림 · 누락 · 페이지 번호 섞임 ·
글자 깨짐)는 코드가 1차 분류하고 사람 · 에이전트가 표 복원 때 확인한다.

실행: PYTHONIOENCODING=utf-8 python scripts/ontology_pdf_diff.py --doc <doc_id> --pdf <경로> [--조 제33조,제36조,별지2] --out <md 경로>
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db  # noqa: E402
from ontology_norm import 발췌_정규화  # noqa: E402

_RE_조머리 = re.compile(r"(?m)^\s*(제\s*\d+\s*조(?:의\s*\d+)?)\s*\(")
_RE_별지 = re.compile(r"(?m)^\s*\[\s*별지\s*제?\s*([①-⑳\d]+)\s*호?\s*\]")
# 목차의 `부칙 ······ 30` 은 조 경계가 아니다. 표제 줄 `부 칙 < 2026. 06. 23. 개정 >` 만 본다.
_RE_부칙 = re.compile(r"(?m)^\s*부\s*칙\s*(?:[<〈(].*)?$")
_원문자 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _별지번호(s: str) -> str:
    return str(_원문자.index(s) + 1) if s in _원문자 else s


def pdf_조문(pdf경로: str) -> dict[str, dict]:
    """PDF → {조번호: {본문, 쪽}}. 같은 조번호가 다시 나오면(제3편 재시작 없음 전제) 처음 것을 쓴다."""
    쪽글: list[tuple[int, str]] = []
    with pdfplumber.open(pdf경로) as pdf:
        for i, p in enumerate(pdf.pages, 1):
            쪽글.append((i, p.extract_text() or ""))
    전체 = ""
    쪽시작: list[tuple[int, int]] = []
    for i, t in 쪽글:
        쪽시작.append((len(전체), i))
        전체 += t + "\n"
    경계: list[tuple[int, str]] = []
    for m in _RE_조머리.finditer(전체):
        경계.append((m.start(), re.sub(r"\s+", "", m.group(1))))
    for m in _RE_별지.finditer(전체):
        경계.append((m.start(), f"별지{_별지번호(m.group(1))}"))
    for m in _RE_부칙.finditer(전체):
        경계.append((m.start(), "부칙"))
    경계.sort()

    def 쪽(pos: int) -> int:
        r = 쪽시작[0][1]
        for 시작, n in 쪽시작:
            if 시작 <= pos:
                r = n
        return r

    out: dict[str, dict] = {}
    for k, (st, 조) in enumerate(경계):
        끝 = 경계[k + 1][0] if k + 1 < len(경계) else len(전체)
        # 본문이 별지를 인용한 줄이 앞에 있을 수 있어 별지는 뒤의 것(실제 별지 쪽)을 쓴다.
        if 조 not in out or 조.startswith("별지"):
            out[조] = {"본문": 전체[st:끝], "쪽": 쪽(st)}
    return out


def _접기(s: str) -> str:
    return re.sub(r"\s+", "", 발췌_정규화(s))


def _어절(s: str) -> set[str]:
    return {w for w in re.split(r"[\s|]+", 발췌_정규화(s)) if w and not set(w) <= set("-:")}


def 조_대조(조번호: str, db본문: str, pdf본문: str | None) -> list[dict]:
    if pdf본문 is None:
        return [{"위치": "전체", "종류": "PDF에서 못 찾음", "DB": "", "PDF": ""}]
    a, b = _접기(db본문), _접기(pdf본문)
    if a == b:
        return []
    표조문 = "|" in db본문
    if 표조문 or 조번호.startswith("별지"):
        없음 = sorted(_어절(pdf본문) - _어절(db본문))
        남음 = sorted(_어절(db본문) - _어절(pdf본문))
        if not 없음 and not 남음:
            return [{"위치": "표", "종류": "셀 순서만 다름(어절 일치)", "DB": "", "PDF": ""}]
        return [{"위치": "표", "종류": "셀 풀림/어절 차이", "DB": " ".join(남음)[:300], "PDF": " ".join(없음)[:300]}]
    out = []
    for 태그, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if 태그 == "equal":
            continue
        d, p = a[i1:i2], b[j1:j2]
        종류 = "누락" if 태그 in ("insert", "delete") else "글자 다름"
        if re.fullmatch(r"-?\d{1,3}-?", p or d or ""):
            종류 = "페이지 번호 섞임"
        out.append({"위치": a[max(0, i1 - 12):i1], "종류": 종류, "DB": d[:200], "PDF": p[:200]})
    return out


def 실행(doc_id: str, pdf경로: str, 조목록: list[str] | None, 출력: Path) -> dict:
    with db.connect(connect_timeout=5, autocommit=True) as conn:
        행 = conn.execute('SELECT "조번호", "본문" FROM corpus.doc_articles WHERE doc_id=%s '
                          'ORDER BY "조순번" NULLS LAST, article_id', (doc_id,)).fetchall()
    pdf = pdf_조문(pdf경로)
    대상 = [r for r in 행 if not 조목록 or r[0] in 조목록]
    if not 대상:
        raise ValueError("대조할 DB 조문이 없다")
    줄 = [f"# PDF 대조 — {doc_id}", "", f"PDF: `{pdf경로}`", "",
          "| 조 | PDF 쪽 | 위치 | 종류 | DB | PDF |", "|---|---|---|---|---|---|"]
    합계 = {"조": 0, "차이있는조": 0, "차이": 0}
    for 조번호, 본문 in 대상:
        합계["조"] += 1
        p = pdf.get(조번호)
        차이 = 조_대조(조번호, 본문 or "", p["본문"] if p else None)
        if 차이:
            합계["차이있는조"] += 1
            합계["차이"] += len(차이)
        for d in 차이:
            셀 = [str(d[k]).replace("|", "\\|").replace("\n", " ") for k in ("위치", "종류", "DB", "PDF")]
            줄.append(f"| {조번호} | {p['쪽'] if p else '-'} | " + " | ".join(셀) + " |")
    db조 = {r[0] for r in 대상}
    for 조번호, p in pdf.items():
        if 조번호 == "부칙" or 조번호 in db조 or (조목록 and 조번호 not in 조목록):
            continue
        합계["조"] += 1
        합계["차이있는조"] += 1
        합계["차이"] += 1
        줄.append(f"| {조번호} | {p['쪽']} | 전체 | DB에서 못 찾음 | | |")
    줄 += ["", f"합계: 조 {합계['조']} · 차이 있는 조 {합계['차이있는조']} · 차이 {합계['차이']}", ""]
    if "부칙" in pdf:
        줄 += [f"부칙(PDF p.{pdf['부칙']['쪽']}): {발췌_정규화(pdf['부칙']['본문'])[:200]}", ""]
    출력.parent.mkdir(parents=True, exist_ok=True)
    출력.write_text("\n".join(줄), encoding="utf-8")
    return 합계


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--조", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(실행(a.doc, a.pdf, [x for x in a.조.split(",") if x] or None, Path(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
