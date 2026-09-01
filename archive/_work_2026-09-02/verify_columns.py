# -*- coding: utf-8 -*-
"""컬럼 분리 검증 — 조 번호 단조성이 판정 기준.

2단이면 조 추출 순서가 반드시 섞인다(제4 -> 제9 -> 제5 -> 제10).
분리가 제대로 되면 단조 증가해야 한다. 이게 유일한 객관 지표다.
"""
import sys, re, pathlib, json, time
sys.path.insert(0, "scripts")
import pdftext

DOCS = {
 "예비창업 2025":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/예비창업패키지/예비창업패키지 세부관리기준(2025년).pdf",
 "초기창업 2025":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지/초기창업패키지 세부관리기준(2025년).pdf",
 "재도전 2025":    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/재도전성공패키지/재도전성공패키지 세부관리기준(2025년).pdf",
 "창업중심대학":    "2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf",
 "도약 2025":      "2026_Finance_DATA_FOR_RAG/창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년).pdf",
 "초격차 제10차":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).pdf",
 "모두의창업":     "2026_Finance_DATA_FOR_RAG/창진원/모두의 창업 (일반-기술)/모두의 창업 프로젝트 세부관리기준(개정본).pdf",
 "TIPS 2026":     "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)/2026/붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문.pdf",
 "통합관리지침 14차": "2026_Finance_DATA_FOR_RAG/중기부/중소기업창업 지원사업 통합관리지침(중소벤처기업부고시 제2025-95호)(20251223).pdf",
}
RE_JO = re.compile(r"제\s*(\d+)\s*조\s*\(")

def inversions(ns):
    """비단조 지점 수. 0 이면 완전 단조."""
    return sum(1 for i in range(len(ns)-1) if ns[i+1] < ns[i])

out = {}
print(f"{'문서':<18}{'거터':>7}{'조':>5}{'역전':>6}{'최장본문':>9}  판정")
for name, rel in DOCS.items():
    p = pathlib.Path(rel)
    if not p.exists():
        print(f"{name:<18}  파일없음: {rel[:50]}"); continue
    t0 = time.time()
    text, meta = pdftext.extract_meta(p)
    hits = list(RE_JO.finditer(text))
    ns = [int(m.group(1)) for m in hits]
    lens = [ (hits[i+1].start()-hits[i].end()) if i+1 < len(hits) else len(text)-hits[i].end()
             for i in range(len(hits)) ]
    inv = inversions(ns)
    g = meta["gutter"]
    ok = "OK" if inv == 0 else ("역전잔존" if g else "1단판정")
    out[name] = {"gutter": g, "jo": len(ns), "inv": inv, "maxlen": max(lens) if lens else 0,
                 "sec": round(time.time()-t0,1), "dedupe": meta["dedupe"]}
    print(f"{name:<18}{(f'x{int(g)}' if g else '-'):>7}{len(ns):>5}{inv:>6}"
          f"{(max(lens) if lens else 0):>9}  {ok}")
pathlib.Path("scripts/_work/_column_verify.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
