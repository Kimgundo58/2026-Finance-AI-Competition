# -*- coding: utf-8 -*-
"""가로 띠(4분면 배치) 탐지 위험도 측정 — 8건 전수."""
import sys, json, pathlib
sys.path.insert(0, "scripts")
import pdfplumber

D = json.loads(pathlib.Path("scripts/_work/_extract_verify.json").read_text(encoding="utf-8"))

def band(page, minh=40, lo=0.30, hi=0.70):
    ws = page.extract_words()
    if len(ws) < 40: return None
    H = page.height
    ys = sorted((w["top"], w["bottom"]) for w in ws); mg = []
    for a, b in ys:
        if mg and a <= mg[-1][1] + 2: mg[-1][1] = max(mg[-1][1], b)
        else: mg.append([a, b])
    best = None
    for i in range(len(mg) - 1):
        g0, g1 = mg[i][1], mg[i + 1][0]
        mid = (g0 + g1) / 2
        if g1 - g0 >= minh and lo * H < mid < hi * H:
            if best is None or g1 - g0 > best[1]: best = (mid, g1 - g0)
    return best

print(f"{'문서':<16}{'쪽':>4}  가로띠 검출 쪽 / 전체   위치(중앙값)")
for name, r in D.items():
    if "checks" not in r: continue
    with pdfplumber.open(r["path"]) as pdf:
        pgs = pdf.pages[:10]
        bs = [band(p) for p in pgs]
        hit = [b for b in bs if b]
        mid = round(sorted(x for x, _ in hit)[len(hit)//2]) if hit else None
        H = int(pgs[0].height)
        print(f"{name:<16}{len(pgs):>4}  {len(hit):>3}/{len(pgs):<3}"
              f"   {(f'y{mid} (높이 {H}, {mid*100//H}%)' if mid else '-')}", flush=True)
