# -*- coding: utf-8 -*-
"""추출 무결성 전수 검사 — 컬럼 외 오류 유형까지.

refs / rules / chunks 가 전부 이 텍스트 위에 올라가므로, 여기서 새는 것은
아래로 전부 번진다. 실패를 숨기지 않고 그대로 보고한다.
"""
import sys, re, json, pathlib, time, collections
sys.path.insert(0, "scripts")
import pdftext, pdfplumber

ROOT = pathlib.Path(".")
DOCS = {
 "예비창업 2025":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/예비창업패키지/예비창업패키지 세부관리기준(2025년).pdf",
 "초기창업 2025":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지/초기창업패키지 세부관리기준(2025년).pdf",
 "재도전 2025":    "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/재도전성공패키지/재도전성공패키지 세부관리기준(2025년).pdf",
 "창업중심대학":    "2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf",
 "도약 2025":      "2026_Finance_DATA_FOR_RAG/창진원/창업도약패키지/창업도약패키지 세부관리기준(2025년).pdf",
 "초격차 제10차":  "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).pdf",
 "모두의창업":     "2026_Finance_DATA_FOR_RAG/창진원/모두의 창업 (일반-기술)/모두의 창업 프로젝트 세부관리기준(개정본).pdf",
 "TIPS 2026":     "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)/2026/붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문.pdf",
}
RE_JO   = re.compile(r"제\s*(\d+)\s*조(?:의\s*(\d+))?\s*\(")
RE_REF  = re.compile(r"(?:지침|법|령|규정|기준|요령)\s*제\s*\d+\s*조|별표\s*\d+|제\s*\d+\s*조\s*제\s*\d+\s*항")
RE_PGNO = re.compile(r"-\s*\d+\s*-")
RE_BUCH = re.compile(r"부\s*칙")

def multi_gutters(page, minw=18):
    """빈 띠를 전부 센다. 2개 이상이면 3단 이상 의심."""
    ws = page.extract_words()
    if len(ws) < 30: return []
    W = page.width; lo, hi = 0.12*W, 0.88*W
    spans = sorted((w["x0"], w["x1"]) for w in ws); merged=[]
    for a,b in spans:
        if merged and a <= merged[-1][1]: merged[-1][1]=max(merged[-1][1],b)
        else: merged.append([a,b])
    out=[]
    for i in range(len(merged)-1):
        g0,g1 = merged[i][1], merged[i+1][0]; mid=(g0+g1)/2
        if g1-g0 >= minw and lo < mid < hi: out.append((round(mid), round(g1-g0)))
    return out

report = {}
for name, rel in DOCS.items():
    p = ROOT / rel
    r = {"path": rel}
    if not p.exists():
        r["FAIL"] = ["파일없음"]; report[name] = r; continue
    t0 = time.time()
    text, meta = pdftext.extract_meta(p)
    r["sec"] = round(time.time()-t0, 1)
    r.update(dedupe=meta["dedupe"], gutter=meta["gutter"], pages=meta["pages"], chars=len(text))

    hits = list(RE_JO.finditer(text))
    ns   = [int(m.group(1)) for m in hits]
    lens = [(hits[i+1].start()-hits[i].end()) if i+1 < len(hits) else len(text)-hits[i].end()
            for i in range(len(hits))]
    cnt  = collections.Counter(ns)

    with pdfplumber.open(p) as pdf:
        pgs = pdf.pages[:8]
        gut_counts = [len(multi_gutters(x)) for x in pgs]

    r["checks"] = {
      "V1 텍스트추출":   {"val": len(text), "ok": len(text) > 1000},
      "V2 조추출":       {"val": len(ns),   "ok": len(ns) >= 5},
      "V3 단조성":       {"val": sum(1 for i in range(len(ns)-1) if ns[i+1] < ns[i]), "ok": None},
      "V4 최장조본문":   {"val": max(lens) if lens else 0, "ok": (max(lens) if lens else 0) < 3000},
      "V5 조번호중복":   {"val": sum(v-1 for v in cnt.values() if v > 1), "ok": None},
      "V6 3단이상의심":  {"val": max(gut_counts) if gut_counts else 0, "ok": (max(gut_counts) if gut_counts else 0) <= 1},
      "V7 쪽번호혼입":   {"val": len(RE_PGNO.findall(text)), "ok": None},
      "V8 부칙표기":     {"val": len(RE_BUCH.findall(text)), "ok": None},
      "V9 조의N표기":    {"val": sum(1 for m in hits if m.group(2)), "ok": None},
      "V10 참조표기수":  {"val": len(RE_REF.findall(text)), "ok": None},
    }
    r["checks"]["V3 단조성"]["ok"] = r["checks"]["V3 단조성"]["val"] == 0
    r["checks"]["V5 조번호중복"]["ok"] = r["checks"]["V5 조번호중복"]["val"] <= 1  # 부칙 제1조 1건 허용
    r["jo_seq"] = ns
    report[name] = r
    print(f"{name}: 완료 {r['sec']}초", flush=True)

pathlib.Path("scripts/_work/_extract_verify.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장: scripts/_work/_extract_verify.json")
