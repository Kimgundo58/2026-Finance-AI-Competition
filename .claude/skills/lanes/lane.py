# -*- coding: utf-8 -*-
"""레인 다이어그램 렌더러 — `||` 로 나뉜 N열 텍스트 도식을 정렬 보장하며 그린다.

    python .claude/skills/lanes/lane.py spec.json

왜 스크립트인가: 손으로 공백을 맞추면 반드시 틀어진다. 한글은 2칸, ASCII 는 1칸이라
눈으로는 맞아 보여도 다른 터미널에서 깨진다. 여기서 폭을 계산하고 **검증까지** 한다.

폭이 모호한 문자(이모지 · ① · -> 의 화살표 U+2192 · 가운뎃점 U+00B7)는
터미널마다 1칸/2칸이 갈리므로 **거부한다.** ASCII 와 한글만 쓴다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HANGUL = ((0xAC00, 0xD7A3), (0x3130, 0x318F), (0x1100, 0x11FF))


def is_hangul(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in HANGUL)


def width(s: str) -> int:
    return sum(2 if is_hangul(c) else 1 for c in s)


def bad_chars(s: str) -> list[str]:
    """폭이 모호해 터미널마다 갈리는 문자."""
    return [c for c in s if not (c.isascii() or is_hangul(c))]


class Renderer:
    def __init__(self, spec: dict):
        self.spec = spec
        self.rows = spec.get("rows", [])
        self.n = self._lane_count()
        self.w = self._width()
        self.errors: list[str] = []

    def _lane_count(self) -> int:
        for r in self.rows:
            if "cells" in r:
                return len(r["cells"])
        raise SystemExit("spec 에 cells 를 가진 row 가 하나도 없다")

    def _width(self) -> int:
        w = self.spec.get("width", "auto")
        if w != "auto":
            return int(w)
        longest = 0
        for r in self.rows:
            for cell in r.get("cells", []):
                for line in cell:
                    longest = max(longest, width(line))
        return longest + 1

    # ── 렌더 ─────────────────────────────────────────────────
    def pad(self, s: str) -> str:
        bad = bad_chars(s)
        if bad:
            self.errors.append(f"폭 모호 문자 {bad} — {s!r}")
        d = self.w - width(s)
        if d < 0:
            self.errors.append(f"폭 초과 {width(s)}/{self.w} — {s!r}")
        return s + " " * max(d, 0)

    def render(self) -> str:
        out: list[str] = []
        if self.spec.get("title"):
            out += [self.spec["title"], ""]
        for r in self.rows:
            if "sep" in r:
                ch = r["sep"] or "-"
                out.append((ch + "||" + ch).join(ch * self.w for _ in range(self.n)))
            elif "full" in r:
                out.append(r["full"])
            else:
                cells = [list(c) for c in r["cells"]]
                h = max(len(c) for c in cells)
                for c in cells:
                    c += [""] * (h - len(c))
                for line in zip(*cells):
                    out.append(" || ".join(self.pad(x) for x in line).rstrip())
        return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    r = Renderer(spec)
    text = r.render()
    if r.errors:
        sys.stderr.write("[!] 렌더 거부 — 아래를 고치고 다시 실행할 것\n")
        for e in r.errors:
            sys.stderr.write(f"    {e}\n")
        sys.stderr.write(
            f"\n    폭 {r.w}칸 / 레인 {r.n}개. 폭을 늘리려면 spec 의 width 를 올리고,\n"
            "    이모지·동그라미숫자·화살표(U+2192)·가운뎃점은 ASCII 로 바꿀 것.\n"
        )
        return 1
    print(text)
    print(f"\n[ok] 레인 {r.n} / 폭 {r.w} / {len(text.splitlines())}행 정렬 검증 통과",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
