# -*- coding: utf-8 -*-
"""L1 통합관리지침의 조 안 표(표-9·표-10 등)를 `_tables.json` 에 «추가» 한다 (중앙 ai-4a).

왜 별도 스크립트인가 — `scripts/archive/indexing/extract_tables.py` 를 그냥 쓸 수 없는 이유 3개
  ① 대상이 `대상수집("L2")` 로 고정이다. L1 은 «한 번도 훑은 적이 없다»
  ② `run()` 이 `_tables.json` 을 통째로 덮어쓴다 — `--doc` 으로 좁혀 돌리면
     나머지 21문서 469표가 «날아간다». 그래서 병합만 한다
  ③ 섹션 라벨을 `[참고N]`/`[붙임N]` 헤더로만 잡는다. L1 의 표는 `제36조` 본문 «안» 에
     `< 표-10 … >` 로 들어 있어 그 정규식에 안 걸린다 -> 여기서는 «감싸는 조» 로 태깅한다
  ④ 이 PDF 는 문자중복 레이어다(`extraction='dedupe'`). 셀을 그냥 뽑으면 "비비목목" 이
     나온다 -> `page.dedupe_chars()` 를 태우고 뽑는다 (pdftext.py 와 같은 처방)

실행:  PYTHONIOENCODING=utf-8 python scratchpad/0단계_L1표추출.py           # dry-run
       PYTHONIOENCODING=utf-8 python scratchpad/0단계_L1표추출.py --write   # _tables.json 병합
"""
from __future__ import annotations

import argparse, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

import pdfplumber  # noqa: E402

문서 = "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"
PDF = ROOT / "2026_Finance_DATA_FOR_RAG" / "중기부" / f"{문서}.pdf"

TABLE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                  "intersection_tolerance": 5}
RE_조 = re.compile(r"^제(\d+)조(?:의(\d+))?\s*\(")


def _cl(c) -> str:
    return re.sub(r"\s+", " ", str(c or "")).strip()


def _meaningful(rows) -> bool:
    if len(rows) < 2:
        return False
    if max(len(r) for r in rows) < 2:
        return False
    return sum(1 for r in rows for c in r if c) >= 4


def 조헤더들(page) -> list[tuple[float, str]]:
    """(y, '제N조') — 줄머리에 있는 조 헤더만. 본문 속 인용은 줄머리가 아니다."""
    words = page.extract_words(keep_blank_chars=False)
    lines: dict[int, list[dict]] = {}
    for w in words:
        lines.setdefault(round(w["top"] / 3.0), []).append(w)
    out = []
    for k in sorted(lines):
        ws = sorted(lines[k], key=lambda w: w["x0"])
        head = "".join(w["text"] for w in ws[:4])
        m = RE_조.match(head)
        if m:
            out.append((ws[0]["top"], f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    새표: list[dict] = []
    cur_sec = None
    with pdfplumber.open(PDF) as pdf:
        for pno, raw in enumerate(pdf.pages):
            pg = raw.dedupe_chars()
            heads = 조헤더들(pg)
            found = pg.find_tables(TABLE_SETTINGS)
            for t in found:
                rows = [[_cl(c) for c in r] for r in t.extract()]
                rows = [r for r in rows if any(r)]
                if not _meaningful(rows):
                    continue
                top = t.bbox[1]
                above = [h for h in heads if h[0] <= top]
                started = bool(above)
                if above:
                    cur_sec = above[-1][1]
                새표.append({"doc_id": 문서, "섹션": cur_sec, "섹션시작": started,
                            "페이지": pno, "페이지_끝": pno,
                            "열": max(len(r) for r in rows), "행수": len(rows), "행": rows})
            if heads:
                cur_sec = heads[-1][1]

    # 이어짐 병합 — extract_tables._merge_continuation 과 같은 조건
    merged: list[dict] = []
    for t in 새표:
        if merged:
            p = merged[-1]
            if (p["페이지_끝"] + 1 == t["페이지"] and p["열"] == t["열"]
                    and p["섹션"] == t["섹션"] and not t["섹션시작"]):
                p["행"].extend(t["행"]); p["페이지_끝"] = t["페이지"]; continue
        merged.append(t)

    import collections
    c = collections.Counter(t["섹션"] for t in merged)
    for k, v in sorted(c.items(), key=lambda x: str(x[0])):
        rows = sum(t["행수"] for t in merged if t["섹션"] == k)
        print(f"  {str(k):<12} 표 {v:2d}  행 {rows:3d}")
    print(f"총 {len(merged)}표")

    if not a.write:
        print("dry-run — _tables.json 을 쓰지 않았다.")
        return 0

    doc = json.loads(TABLES.read_text(encoding="utf-8"))
    before = len(doc["tables"])
    doc["tables"] = [t for t in doc["tables"] if t.get("doc_id") != 문서] + merged
    doc.setdefault("요약", {})["문서별"] = doc.get("요약", {}).get("문서별", {})
    doc["요약"]["문서별"][문서] = len(merged)
    doc["주의"] = (doc.get("주의", "") +
                   " · 2026-09-06 scratchpad/0단계_L1표추출.py 로 L1 통합관리지침을 «추가» 병합"
                   " (섹션 라벨은 감싸는 조번호. dedupe_chars 적용).")
    TABLES.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    print(f"_tables.json  {before} -> {len(doc['tables'])} 표")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
