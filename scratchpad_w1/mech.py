# -*- coding: utf-8 -*-
"""문체 규칙 기계 적용 — 코드펜스 언어 지정 · 특수괄호 · 빨강표시 정리.

코드 블록 «안» 은 건드리지 않는다. 증거이고 계약이라 원문 그대로 둔다.
"""
import io, re, sys

SHELL = re.compile(r"^\s*(\$|#\s|curl|python|psql|gcloud|git|npm|docker|bash|export|pytest|uvicorn)")

def lang_of(body):
    first = next((l for l in body if l.strip()), "")
    s = first.strip()
    if SHELL.match(s):
        return "bash"
    if s.startswith("{") or s.startswith("["):
        return "json"
    return "text"

def run(path, keep_red=3):
    src = io.open(path, encoding="utf-8").read().split("\n")
    out, i, fence, red, stats = [], 0, False, 0, {"lang":0,"br":0,"red":0}
    while i < len(src):
        l = src[i]
        if l.startswith("```"):
            if not fence:
                if l.strip() == "```":
                    j = i + 1
                    body = []
                    while j < len(src) and not src[j].startswith("```"):
                        body.append(src[j]); j += 1
                    l = "```" + lang_of(body); stats["lang"] += 1
                fence = True
            else:
                fence = False
            out.append(l); i += 1; continue
        if not fence:
            n = l
            for a, b in (("«", '"'), ("»", '"'), ("「", '"'), ("」", '"')):
                n = n.replace(a, b)
            if n != l: stats["br"] += 1
            for _ in range(n.count("🔴")):
                red += 1
                if red > keep_red:
                    n = n.replace("🔴 ", "", 1) if "🔴 " in n else n.replace("🔴", "", 1)
                    stats["red"] += 1
            l = n
        out.append(l); i += 1
    io.open(path, "w", encoding="utf-8").write("\n".join(out))
    print(f"{path.split('/')[-1]:32} 언어지정 {stats['lang']:2} · 괄호 {stats['br']:2}줄 · 빨강제거 {stats['red']:2}")

for p in sys.argv[1:]:
    run(p)
