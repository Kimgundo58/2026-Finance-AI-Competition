# -*- coding: utf-8 -*-
"""RE_ATTACH 후보의 위치 분포 + 본문 조 헤딩과의 관계를 8건 전수 측정.
45% 임계를 지울 수 있는지 근거를 만든다."""
import sys, json, pathlib
sys.path.insert(0, "scripts")
import pdftext
from stage0_articles import RE_ATTACH, RE_JO, RE_BUCHIK, _clean

D = json.loads(pathlib.Path("scripts/_work/_extract_verify.json").read_text(encoding="utf-8"))
CACHE = pathlib.Path("scripts/_work/_text_cache"); CACHE.mkdir(exist_ok=True)

for name, r in D.items():
    if "checks" not in r:
        continue
    c = CACHE / (name.replace(" ", "_") + ".txt")
    if c.exists():
        t = c.read_text(encoding="utf-8")
    else:
        t, _ = pdftext.extract_meta(pathlib.Path(r["path"]))
        t = _clean(t)
        c.write_text(t, encoding="utf-8")
    L = len(t)
    bm = RE_BUCHIK.search(t)
    bpos = bm.start() if bm else None
    print(f"\n== {name}  len={L}  부칙={bpos}({bpos/L:.0%})" if bpos else f"\n== {name}  len={L}  부칙=없음")
    for m in RE_ATTACH.finditer(t):
        pos = m.start()
        tail = t[pos:]
        n_jo = len(RE_JO.findall(tail))          # 이후 구간의 조 헤딩 수
        after = "부칙뒤" if (bpos is not None and pos > bpos) else "부칙앞"
        print(f"   {pos:>6} {pos/L:5.0%}  {after}  이후조={n_jo:>3}  {m.group(0).strip()[:52]!r}")
