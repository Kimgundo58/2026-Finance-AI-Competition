# -*- coding: utf-8 -*-
"""역참조 인덱스 — 조문 입장에서 '누가 나를 인용했는지'.

`법령 연계 모음/문서_법령_링크모음.md` §3(문서별 참조 조항)을 파싱해
    조문  →  [ {문서, 판/연도, 위치, 원문표기, 사업명}, ... ]
를 만든다. 정참조(문서→조문)를 뒤집은 것이다.

왜 필요한가
  수집한 조문 11,676개 중 문서가 실제 인용한 건 82개(0.7%)뿐이다.
  나머지 99.3%는 검색 노이즈다. 역참조가 있으면 인용된 조문을 상위로
  올리고, chunks.사업명(TEXT[] + GIN 인덱스)을 채워 사업별 필터를 걸 수 있다.

산출물
  법령 PDF/_law_citations.json   ref_id → 조 → [인용 레코드]

실행:
    python scripts/build_citations.py            생성 + 요약
    python scripts/build_citations.py --show L10 특정 규범의 역참조 확인
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
MD_PATH = ROOT / "법령 연계 모음" / "문서_법령_링크모음.md"
OUT_PATH = ROOT / "법령 PDF" / "_law_citations.json"
REPORT_PATH = ROOT / "법령 PDF" / "_law_report.json"

# §3 절 제목 → 사업명 (chunks.사업명 에 들어갈 값)
SECTION_BIZ = {
    "3-1": ["초기창업패키지"],
    "3-2": ["혁신분야 창업패키지(신산업)", "초격차 스타트업 1000+"],
    "3-3": ["예비창업패키지"],
    "3-4": ["재도전성공패키지"],
    "3-5": ["모두의 창업 프로젝트"],
    "3-6": ["창업도약패키지", "창업중심대학"],
    "3-7": ["민관공동 창업자 발굴·육성사업(TIPS)"],
}

RE_REF = re.compile(r"\b([LR]\d{2})\b")
RE_SEC = re.compile(r"^###\s+(3-\d)\.\s*(.+?)\s*(?:\(원본:|$)")
RE_SUB = re.compile(r"^####\s+(.+?)\s*$")
RE_YEAR = re.compile(r"(20\d{2})")


def parse_articles(s: str) -> list[str]:
    """조항 문자열 → 조 식별자. Law_Crawling.parse_articles 와 같은 규칙."""
    from Law_Crawling import parse_articles as _pa   # 단일 소스 유지
    return _pa(s)[0]


def _biz_for(section: str, sub: str, row_cols: dict) -> list[str]:
    """이 행이 어느 사업에 속하는지."""
    biz = list(SECTION_BIZ.get(section, []))
    if section == "3-2":
        # 넓은 표: ○ 가 찍힌 열만 해당 사업
        hit = []
        if row_cols.get("신산업23"):
            hit.append("혁신분야 창업패키지(신산업)")
        if any(row_cols.get(k) for k in ("초격차24", "초격차25", "제10차")):
            hit.append("초격차 스타트업 1000+")
        return hit or biz
    if section == "3-6" and sub:
        if "창업도약" in sub:
            return ["창업도약패키지"]
        if "창업중심대학" in sub:
            return ["창업중심대학"]
    return biz


def _pair(refs: list[str], 조항_raw: str) -> list[tuple[str, str]]:
    """'L33 ... / L34 동 시행령' + '제22조 / 제49조' 를 짝지어 준다.

    개수가 맞으면 1:1, 아니면 모든 규범에 전체 조항을 적용한다.
    """
    parts = [p.strip() for p in 조항_raw.split("/")]
    if len(refs) > 1 and len(parts) == len(refs):
        return list(zip(refs, parts))
    return [(r, 조항_raw) for r in refs]


def build(md_path: Path = MD_PATH) -> dict:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    section, sub, header = None, "", []
    sec_title = ""
    in_s3 = False
    cites: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    unresolved: list[dict] = []

    for line in lines:
        s = line.strip()
        if s.startswith("## §3"):
            in_s3 = True
            continue
        if s.startswith("## §") and not s.startswith("## §3"):
            in_s3 = False
        if not in_s3:
            continue

        m = RE_SEC.match(s)
        if m:
            section, sub, header = m.group(1), "", []
            sec_title = m.group(2)
            continue
        m = RE_SUB.match(s)
        if m:
            sub, header = m.group(1), []
            continue
        if not s.startswith("|"):
            continue

        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):        # 구분선
            continue
        if not header:                                # 첫 행 = 헤더
            header = cells
            continue

        col = dict(zip(header, cells))
        규범 = col.get("규범", "")
        refs = RE_REF.findall(규범)
        if not refs:
            continue

        조항_raw = col.get("조항", "")
        위치 = (col.get("위치") or col.get("관리기준 위치")
                or col.get("운영지침 위치") or "")
        연도 = col.get("연도", "") or sub
        edition = 연도 or sub or ""
        if section == "3-2":
            # 넓은 표는 ○ 가 찍힌 열이 곧 판(版)이다
            marks = [k for k in ("신산업23", "초격차24", "초격차25", "제10차")
                     if col.get(k)]
            edition = ", ".join(marks) or sec_title
        doc_name = sub or sec_title or section
        biz = _biz_for(section, sub, col)

        for ref, 조항s in _pair(refs, 조항_raw):
            jos = parse_articles(조항s)
            rec = {
                "doc": doc_name,
                "edition": edition,
                "section": section,
                "loc": 위치,
                "as_written": 조항s,
                "사업명": biz,
            }
            if not jos:
                unresolved.append({**rec, "ref_id": ref, "reason": "조 미특정"})
                cites[ref]["_미특정"].append(rec)
                continue
            for jo in jos:
                cites[ref][jo].append(rec)

    return {"citations": {k: dict(v) for k, v in cites.items()},
            "unresolved": unresolved}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", default="", help="특정 ref_id 의 역참조 출력")
    args = ap.parse_args()

    data = build()
    cites = data["citations"]

    if args.show:
        r = cites.get(args.show)
        if not r:
            print(f"{args.show} 에 대한 역참조가 없습니다.")
            return
        print(f"=== {args.show} 역참조 ===")
        for jo, recs in sorted(r.items(), key=lambda x: (x[0] == "_미특정", x[0])):
            label = "조 미특정" if jo == "_미특정" else (
                f"별표 {jo[2:]}" if jo.startswith("별표") else
                f"제{jo.replace('-', '조의')}조" if "-" in jo else f"제{jo}조")
            print(f"  {label}  ← {len(recs)}건")
            for x in recs:
                loc = f" / {x['loc']}" if x["loc"] else ""
                print(f"      {x['edition']}{loc}   [{', '.join(x['사업명'])}]")
        return

    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    n_ref = len(cites)
    n_jo = sum(len(v) for v in cites.values())
    n_rec = sum(len(r) for v in cites.values() for r in v.values())
    미특정 = sum(1 for v in cites.values() if "_미특정" in v)

    print(f"역참조 인덱스 생성: {OUT_PATH}")
    print(f"  규범 {n_ref}개 / 조문 {n_jo}개 / 인용 레코드 {n_rec}건")
    print(f"  조 미특정 인용을 가진 규범 {미특정}개")

    print("\n── 인용이 많은 조문 상위 ──")
    flat = [(ref, jo, len(recs)) for ref, v in cites.items()
            for jo, recs in v.items() if jo != "_미특정"]
    for ref, jo, n in sorted(flat, key=lambda x: -x[2])[:12]:
        label = f"별표{jo[2:]}" if jo.startswith("별표") else f"제{jo}조"
        print(f"  {ref} {label:<10} {n}회")

    # 수집 결과와 대조 — 역참조가 있는데 수집 안 된 규범 확인
    if REPORT_PATH.exists():
        rep = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        got = {r["ref_id"] for r in rep if r.get("status") == "수집"}
        missing = sorted(set(cites) - got)
        if missing:
            print(f"\n── ⚠ 역참조는 있으나 미수집 ({len(missing)}건) ──")
            for m in missing:
                print(f"  {m}: 조문 {len(cites[m])}개 인용됨")


if __name__ == "__main__":
    main()
