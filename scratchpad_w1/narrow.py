# -*- coding: utf-8 -*-
"""6열 파라미터 표를 좁힌다. 셀 값은 하나도 안 버린다.

절차:
1. 그 표 안에서 값이 하나뿐인 열은 뺀다. 뺀 값은 표 위에 한 줄로 적는다
2. 그래도 4열을 넘으면 뒤쪽 열을 한 칸에 모은다. 순서는 표 위에 적는다
"""
import io, sys

def cells(line):
    t = line.strip()
    if t.startswith("|"): t = t[1:]
    if t.endswith("|"): t = t[:-1]
    out, buf, i = [], "", 0
    while i < len(t):
        if t[i] == "\\" and i + 1 < len(t) and t[i + 1] == "|":
            buf += "\|"; i += 2; continue
        if t[i] == "|":
            out.append(buf.strip()); buf = ""; i += 1; continue
        buf += t[i]; i += 1
    out.append(buf.strip())
    return out

def render(hdr, align, rows):
    o = ["| " + " | ".join(hdr) + " |", "|" + "|".join(align) + "|"]
    for r in rows:
        o.append("| " + " | ".join(r) + " |")
    return o

def narrow(hdr, align, rows):
    note = []
    keep = list(range(len(hdr)))
    for i in range(len(hdr) - 1, 0, -1):          # 첫 열(키)은 유지
        vals = {r[i] for r in rows}
        if len(vals) == 1:
            v = vals.pop()
            note.append(f"{hdr[i]}는 전부 {v if v else '빈 값'}이다")
            keep.remove(i)
    hdr = [hdr[i] for i in keep]
    align = [align[i] for i in keep]
    rows = [[r[i] for i in keep] for r in rows]
    if len(hdr) > 4:                               # 남은 뒤쪽 열을 한 칸으로 모은다
        head, tail = hdr[:2], hdr[2:]
        note.append(f"{' · '.join(tail)} 순서로 한 칸에 적는다")
        hdr = head + [" · ".join(tail)]
        align = align[:2] + ["---"]
        rows = [r[:2] + [" · ".join(x if x else "—" for x in r[2:])] for r in rows]
    return hdr, align, rows, note

def run(path):
    src = io.open(path, encoding="utf-8").read().split("\n")
    out, i, fence, n = [], 0, False, 0
    while i < len(src):
        l = src[i]
        if l.startswith("```"):
            fence = not fence; out.append(l); i += 1; continue
        if fence or not l.strip().startswith("|"):
            out.append(l); i += 1; continue
        blk = []
        while i < len(src) and src[i].strip().startswith("|"):
            blk.append(src[i]); i += 1
        hdr = cells(blk[0])
        if len(hdr) <= 4 or len(blk) < 3:
            out.extend(blk); continue
        align = cells(blk[1])
        rows = [cells(x) for x in blk[2:]]
        if any(len(r) != len(hdr) for r in rows):
            out.extend(blk); continue
        nh, na, nr, note = narrow(hdr, align, rows)
        for t in note:
            out.append(t + ".")
        if note: out.append("")
        out.extend(render(nh, na, nr))
        n += 1
    io.open(path, "w", encoding="utf-8").write("\n".join(out))
    print(f"{path.split('/')[-1]}: 표 {n}개 좁힘")

for p in sys.argv[1:]:
    run(p)
