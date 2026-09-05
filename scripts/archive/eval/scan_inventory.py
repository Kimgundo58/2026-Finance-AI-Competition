# -*- coding: utf-8 -*-
"""스캔 전용 PDF 전수 조사 → `_scan_inventory.json`.

텍스트 레이어가 없는 PDF 는 파서가 조용히 0자를 뱉는다. **어느 문서가 그런지 먼저 알아야**
판독 우선순위를 정할 수 있다.

판정 등급별로 처리 방침이 다르다:
    A등급 근거(지침·세부관리기준) → 판독본을 그대로 인용하면 안 된다.
        원칙 4(인용은 생성이 아니라 추출)상 손으로 옮긴 텍스트는 원문 일치 검증이 무의미하다.
        판독하더라도 `parse_quality='low'` + `extraction='vlm'` 로 태깅하고,
        판정 시 "이 근거는 판독본입니다" 경고를 강제한다.
    B등급 사례(사례집) → 판독본 사용 가능. 애초에 참고용이라 인용 정확성 요구가 낮다.

실행:
    python scripts/archive/eval/scan_inventory.py
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


import io
import json
import sys
from pathlib import Path

import pdfplumber

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
OUT = ROOT / "2026_Finance_DATA_FOR_RAG" / "_scan_inventory.json"

# 판정 근거로 쓰이는가 = 판독본 사용 시 경고가 필요한가
A등급_힌트 = ("지침", "세부관리기준", "운영요령", "관리규정", "법률", "고시", "훈령")


def 등급(name: str) -> str:
    if any(h in name for h in A등급_힌트):
        return "A등급(판정 근거) — 판독본 인용 시 경고 필수"
    if "사례" in name or "FAQ" in name or "질의응답" in name:
        return "B등급(참고) — 판독본 사용 가능"
    return "기타(매뉴얼·공고 등) — 인덱싱 대상 아님"


def main() -> None:
    rows = []
    bases = [ROOT / "2026_Finance_DATA_FOR_RAG", ROOT / "건국대학교 레퍼런스",
             ROOT / "_hwp변환"]
    files = sorted({f for b in bases if b.exists() for f in b.rglob("*.pdf")})
    print(f"점검 {len(files)}건")
    for i, f in enumerate(files, 1):
        try:
            with pdfplumber.open(f) as pdf:
                n = len(pdf.pages)
                # 앞·중간·뒤 3쪽만 표본
                idxs = sorted({0, n // 2, n - 1})
                chars = sum(len((pdf.pages[j].extract_text() or "").strip()) for j in idxs)
        except Exception as exc:                                   # noqa: BLE001
            print(f"  x {f.name[:50]} — {exc}")
            continue
        per_page = chars / max(1, len(idxs))
        if per_page < 60:                       # 사실상 텍스트 없음
            rows.append({
                "file": str(f.relative_to(ROOT)).replace("\\", "/"),
                "쪽수": n, "표본_평균문자": round(per_page, 1),
                "등급": 등급(f.name),
            })
        if i % 40 == 0:
            print(f"  … {i}/{len(files)}")

    rows.sort(key=lambda r: (not r["등급"].startswith("A"), -r["쪽수"]))
    doc = {
        "생성": "scripts/archive/eval/scan_inventory.py",
        "기준일": "2026-08-27",
        "판정기준": "앞·중간·뒤 3쪽 표본의 쪽당 평균 문자수 < 60 이면 스캔 전용으로 본다",
        "총건수": len(rows),
        "방침": {
            "A등급": "판독하되 parse_quality='low' + extraction='vlm' 태깅. 판정 시 경고 강제. "
                     "가능하면 텍스트 확보된 직전 판본과 diff 해서 변경분만 검증한다.",
            "B등급": "판독본 그대로 사용 가능 (사례집이 이미 이 경로로 처리됨)",
            "기타": "인덱싱 대상이 아니므로 판독 불필요",
        },
        "문서": rows,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n스캔 전용 {len(rows)}건 → {OUT.relative_to(ROOT)}")
    for r in rows[:25]:
        print(f"  {r['쪽수']:4d}쪽  [{r['등급'][:12]}]  {Path(r['file']).name[:58]}")


if __name__ == "__main__":
    main()
