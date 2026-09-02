# -*- coding: utf-8 -*-
"""새 판별식 3종을 8건 전수로 검증한다.
  (a) 부칙 = 줄머리 마커 + 뒤 300자에 '시행'   -> 첫 마커
  (b) 목차 = 점선 10회 이상이고 마지막이 30% 이전 -> 그 뒤부터 본문
  (c) 개요형(TIPS) = 단조 L2 항목 10개 이상
"""
import sys, re, pathlib
sys.path.insert(0, "scripts")
from stage0_articles import RE_BUCHIK

C = pathlib.Path("scripts/_work/_text_cache")
RE_DOTS = re.compile(r"·{5,}[^\n]*")
RE_L2 = re.compile(r"(?:^|\n)[ \t]*(\d{1,2})\.[ \t]*([^\n]{1,40})")

for p in sorted(C.glob("*.txt")):
    t = p.read_text(encoding="utf-8"); L = len(t)
    # (a)
    cands = [(m.start(), "시행" in t[m.end():m.end() + 300]) for m in RE_BUCHIK.finditer(t)]
    real = [s for s, ok in cands if ok]
    # (b)
    dots = list(RE_DOTS.finditer(t))
    toc = dots[-1].end() if (len(dots) >= 10 and dots[-1].start() < L * 0.30) else 0
    # (c)
    body = t[toc:real[0]] if real else t[toc:]
    acc, last = [], 0
    for m in RE_L2.finditer(body):
        n = int(m.group(1))
        if last < n <= last + 3:
            acc.append(n); last = n
    print(f"{p.stem:<16} len={L:>6} | 부칙후보={len(cands)} 진짜={len(real)} 첫={real[0] if real else None}"
          f" ({(real[0]/L if real else 0):.0%}) | 목차컷={toc} | L2단조={len(acc)} max={last}")
