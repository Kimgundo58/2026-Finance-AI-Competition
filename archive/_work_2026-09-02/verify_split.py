# -*- coding: utf-8 -*-
"""split_articles() 기준 최종 검증. raw 정규식이 아니라 실제 파이프라인 경로."""
import sys, json, pathlib
sys.path.insert(0, "scripts")
import pdftext
from stage0_articles import split_articles, validate

D = json.loads(pathlib.Path("scripts/_work/_extract_verify.json").read_text(encoding="utf-8"))
out = {}
print(f"{'문서':<16}{'거터':>7}{'4up':>5}{'전략':>11}{'조':>5}{'본칙':>5}{'최장':>7}  검증")
for name, r in D.items():
    if "checks" not in r: continue
    text, meta = pdftext.extract_meta(pathlib.Path(r["path"]))
    arts, strat = split_articles(text)
    v = validate(arts, strat)
    본칙 = [a for a in arts if a["조번호_int"] is not None]
    lens = [len(a["본문"]) for a in arts] or [0]
    nums = [a["조번호_int"] for a in 본칙]
    inv = sum(1 for i in range(len(nums)-1) if nums[i+1] < nums[i])
    out[name] = {"gutter": meta["gutter"], "quad": meta.get("quad"), "strategy": strat, "n": len(arts),
                 "본칙": len(본칙), "inv": inv, "max": max(lens),
                 "validate": v, "조번호": [a["조번호"] for a in arts]}
    print(f"{name:<16}{(str(int(meta['gutter'])) if meta['gutter'] else '-'):>7}{('4up' if meta.get('quad') else '-'):>5}{strat:>11}"
          f"{len(arts):>5}{len(본칙):>5}{max(lens):>7}  inv={inv} {v['flags'] or 'OK'}", flush=True)
pathlib.Path("scripts/_work/_split_verify.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장: scripts/_work/_split_verify.json")
