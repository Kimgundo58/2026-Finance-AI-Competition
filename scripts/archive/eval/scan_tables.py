# -*- coding: utf-8 -*-
"""원본 파일에 표가 몇 개 있는지 전수 스캔한다.

목적: "표가 있는데 파싱에서 씹힌 파일"을 사람이 수작업으로 찾지 않게 한다.
  PDF  → pdfplumber find_tables() 로 페이지별 표 개수
  HWP  → HWPTAG_TABLE(76) 레코드 개수
  HWPML→ <TABLE 태그 개수

판정:
  표가 있는데 현재 추출 텍스트에 셀 경계 흔적이 없으면 '깨짐 의심'.

실행:  python scripts/archive/eval/scan_tables.py
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)

import io, json, sys, zlib
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT))

HWPTAG_TABLE = 16 + 60  # 76


def pdf_tables(path: Path):
    import pdfplumber
    n, pages, chars = 0, 0, 0
    with pdfplumber.open(str(path)) as pdf:
        pages = len(pdf.pages)
        for pg in pdf.pages:
            try:
                n += len(pg.find_tables())
                chars += len(pg.extract_text() or "")
            except Exception:
                pass
    return n, pages, chars


def hwp_tables(path: Path):
    head = path.open("rb").read(16)
    if head.lstrip()[:5] == b"<?xml":
        t = path.read_text(encoding="utf-16", errors="replace")
        if "<TABLE" not in t.upper():
            t = path.read_text(encoding="utf-8", errors="replace")
        return t.upper().count("<TABLE"), 0, len(t)

    import olefile
    from hwp_extract import is_compressed, get_sections, parse_records
    ole = olefile.OleFileIO(str(path))
    comp = is_compressed(ole)
    n, chars = 0, 0
    for sec in get_sections(ole):
        raw = ole.openstream(sec).read()
        data = zlib.decompress(raw, -15) if comp else raw
        chars += len(data)
        for tag, _payload in parse_records(data):
            if tag == HWPTAG_TABLE:
                n += 1
    ole.close()
    return n, 0, chars


def main():
    targets = []
    for pat in ("규정문서/*.hwp", "규정문서/*.pdf", "법령 PDF/**/*.pdf", "법령 PDF/**/*.hwp"):
        targets += sorted(ROOT.glob(pat))
    targets = [t for t in targets if not t.name.startswith("~$")]

    rows = []
    for p in targets:
        rel = p.relative_to(ROOT).as_posix()
        try:
            if p.suffix.lower() == ".pdf":
                n, pages, chars = pdf_tables(p)
                kind = "PDF"
            else:
                n, pages, chars = hwp_tables(p)
                kind = "HWP"
        except Exception as e:
            rows.append(dict(file=rel, kind=p.suffix, tables=-1, pages=0,
                             chars=0, err=str(e)[:80]))
            print(f"  ✗ {rel}  ({e.__class__.__name__})", flush=True)
            continue
        rows.append(dict(file=rel, kind=kind, tables=n, pages=pages, chars=chars, err=""))
        print(f"  {kind}  표 {n:>4}  {rel}", flush=True)

    out = ROOT / "scripts" / "_table_scan.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n스캔 {len(rows)}개 → {out.name}")
    print(f"표 있는 파일: {sum(1 for r in rows if r['tables'] > 0)}개")
    print(f"총 표 개수  : {sum(max(0, r['tables']) for r in rows)}")


if __name__ == "__main__":
    main()
