# -*- coding: utf-8 -*-
"""Stage 0.5 준비 — 조 후보 추출. 판정은 하지 않는다.

코드는 여기까지만 한다. 조 경계가 맞는지 / 비단조가 진짜인지 / 붙임이 섹션인지는
원문을 눈으로 봐야 알 수 있다 (RAG.md §3-1). 그래서 이 스크립트는 후보만 뽑고
이상 신호를 표시한다.
"""
import sys, re, json, pathlib
sys.path.insert(0, "scripts")
from pdftext import extract

DOCS = {
 "예비창업_2025": "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/예비창업패키지/예비창업패키지 세부관리기준(2025년).pdf",
 "초기창업_2025": "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지/초기창업패키지 세부관리기준(2025년).pdf",
}
OUT = pathlib.Path("scripts/_work")
RE_JO = re.compile(r"제\s*(\d+)\s*조\s*\(([^)]{1,40})\)")

def clean(s):
    return "".join(c for c in s if c != "\x00" and not (0xD800 <= ord(c) <= 0xDFFF))

result = {}
for name, rel in DOCS.items():
    cache = OUT / f"{name}.txt"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
        dup = None
    else:
        text, dup = extract(pathlib.Path(rel))
        text = clean(text)
        cache.write_text(text, encoding="utf-8")
    hits = list(RE_JO.finditer(text))
    arts, seen = [], {}
    for i, m in enumerate(hits):
        end = hits[i+1].start() if i+1 < len(hits) else len(text)
        body = re.sub(r"[ \t]+", " ", text[m.end():end]).strip()
        n = int(m.group(1))
        flags = []
        if n in seen: flags.append("중복번호")
        if i and n < int(hits[i-1].group(1)): flags.append("비단조")
        if len(body) > 3000: flags.append("본문3000자초과")
        if len(body) < 50: flags.append("본문50자미만")
        seen[n] = 1
        arts.append({"idx": i, "조번호": f"제{n}조", "n": n, "조제목": m.group(2).strip(),
                     "pos_pct": round(m.start()/len(text)*100, 1),
                     "len": len(body), "flags": flags, "본문": body})
    result[name] = arts
    bad = [a for a in arts if a["flags"]]
    print(f"{name}: 조 {len(arts)} / 이상 {len(bad)}건 / 총 {len(text):,}자"
          + (f" / 문자중복 {dup}" if dup is not None else " / 캐시"))
    for a in bad:
        print(f"   [{a['idx']:>2}] {a['조번호']}({a['조제목']}) {a['len']}자 "
              f"문서{a['pos_pct']}% -> {','.join(a['flags'])}")

(OUT / "_jo_candidates.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n저장: scripts/_work/_jo_candidates.json")
