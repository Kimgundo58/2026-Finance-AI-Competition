# -*- coding: utf-8 -*-
"""누락 규범 지정 수집 — `_missing_norms.json` 에 적힌 규범을 이름으로 찾아 `법령 PDF/L1_법령/` 에 받는다.
해소기는 `Law_Crawling.resolve_law` / `resolve_admrul` 을 그대로 쓴다.

실행:
    $env:LAW_GO_KR_OC = "<신청ID>"
    python scripts/archive/crawling/fetch_missing_norms.py            # 수집
    python scripts/archive/crawling/fetch_missing_norms.py --dry-run  # 해결만 하고 저장 안 함
"""
from __future__ import annotations

# scripts/_lib 을 찾을 때까지 위로 올라가 scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 건다.
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
# archive 하위 폴더끼리 서로 import 하므로 scripts/archive/* 도 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())
sys.path.insert(0, str(ROOT / "scripts"))

import Law_Crawling as LC  # noqa: E402

SRC = ROOT / "법령 PDF" / "_missing_norms.json"
OUT = ROOT / "법령 PDF" / "_missing_norms_report.json"

# 인용 표기 -> law.go.kr 현행 제명 (세부관리기준의 인용 표기가 현행 제명과 다른 경우)
정식제명 = {
    "근로자직업능력개발법": "국민 평생 직업능력 개발법",
    "대·중소기업 상생협력 촉진에 관한 법률": "대ㆍ중소기업 상생협력 촉진에 관한 법률",
    # 가운뎃점이 U+00B7(·) 이 아니라 U+318D(ㆍ) 다
    "중소기업기술개발 지원사업 관리지침": "중소기업기술개발 지원사업 운영요령",
}

# law.go.kr 미등재 — 수집 불가 사유
미등재 = {
    "신사업창업사관학교 운영지침":
        "검색 0건. 중기부 내부 지침으로 국가법령정보센터 미등재. "
        "모두의창업 로컬트랙이 준용하므로 중기부 배포본을 따로 구해야 한다",
    "창업지원사업 제3자 부당개입 근절을 위한 업무처리 지침":
        "검색 0건(공정위 '부당한 공동행위 심사지침' 은 별개 규범). 중기부 내부 지침",
    "예산 및 기금운용계획 집행지침":
        "기재부가 매년 발간하는 지침. 법령이 아니라 예산 문서라 API 대상이 아니다",
}

# 종류 -> law.go.kr target
TARGET = {"법률": "law", "시행령": "law", "시행규칙": "law", "행정규칙": "admrul"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--priority", default="", help="높음 / 중간 / 낮음 만 (쉼표 구분)")
    args = ap.parse_args()

    LC.require_oc()
    api = LC.Api()
    spec = json.loads(SRC.read_text(encoding="utf-8"))
    items = spec["수집_대상"]
    if args.priority:
        want = {x.strip() for x in args.priority.split(",")}
        items = [x for x in items if x["우선순위"] in want]
    print(f"수집 대상 {len(items)}종"
          f" ({'해결만' if args.dry_run else '저장'})\n")

    results = []
    for i, it in enumerate(items, 1):
        name, kind = it["규범명"], it["종류"]
        target = TARGET.get(kind, "law")
        정식 = 정식제명.get(name)
        rec = {"규범명": name, "종류": kind, "우선순위": it["우선순위"],
               "판정_접점": it["판정_접점"], "target": target,
               "상태": "미해결", "file": None, "flags": []}
        if name in 미등재:
            rec.update(상태="미등재", flags=[미등재[name]])
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] 미등재 {name[:40]:<42} {미등재[name][:44]}")
            continue
        if 정식:
            rec["flags"].append(f"정식 제명: {정식}")
        try:
            질의 = 정식 or name
            hit = (LC.resolve_law(api, 질의, name) if target == "law"
                   else LC.resolve_admrul(api, 질의))
        except Exception as e:                                  # noqa: BLE001
            rec.update(상태="오류", flags=[f"{type(e).__name__}: {e}"[:120]])
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] !! {name[:40]:<42} {type(e).__name__}")
            continue

        if not hit:
            results.append(rec)
            print(f"[{i:>2}/{len(items)}] -- {name[:40]:<42} 미해결")
            continue

        if target == "law":
             제명, eff, key = hit.get("법령명한글"), hit.get("시행일자"), {"MST": hit["법령일련번호"]}
        else:
            제명, eff, key = hit.get("행정규칙명"), hit.get("시행일자"), {"ID": hit["행정규칙일련번호"]}
        rec.update(제명=제명, 시행일자=eff, mst=list(key.values())[0], 상태="해결")
        if LC.norm(제명) != LC.norm(name) and not 정식:
            rec["flags"].append(f"제명 다름: {제명}")

        if not args.dry_run:
            body = api.body_xml(target, **key)
            # OC 오타·미승인이면 HTTP 200 에 빈 결과가 온다
            if not body.lstrip().startswith("<") or len(body) < 500:
                rec.update(상태="본문없음", flags=rec["flags"] + [f"len={len(body)}"])
                results.append(rec)
                print(f"[{i:>2}/{len(items)}] !! {name[:40]:<42} 본문 비어 있음")
                continue
            fname = f"L1_{LC.safe_name(제명)}_{eff}.xml"
            LC.save_xml(LC.OUT_DIR / fname, body)
            rec.update(상태="수집", file=f"법령 PDF/L1_법령/{fname}", bytes=len(body))

        results.append(rec)
        mark = rec["상태"]
        print(f"[{i:>2}/{len(items)}] {mark:<5} {name[:40]:<42} {제명 or ''}"
              f" ({eff}){'  ' + ' / '.join(rec['flags']) if rec['flags'] else ''}")

    from collections import Counter
    c = Counter(r["상태"] for r in results)
    doc = {"생성": "scripts/archive/crawling/fetch_missing_norms.py", "원본": str(SRC.relative_to(ROOT)),
           "요약": dict(c), "results": results}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{dict(c)}")
    print(f"-> {OUT.relative_to(ROOT)}")
    미해결 = [r["규범명"] for r in results if r["상태"] not in ("수집", "해결")]
    if 미해결:
        print("\n미해결 — 제명이 다르거나 law.go.kr 미등재일 수 있다:")
        for n in 미해결:
            print(f"   {n}")


if __name__ == "__main__":
    main()
