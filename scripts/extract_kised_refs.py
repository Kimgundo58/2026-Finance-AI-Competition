# -*- coding: utf-8 -*-
"""창진원(L2) 문서에서 인용 법령·규범 추출 → 마스터 재도출용 원자료.

기존 창진원 마스터(`법령 연계 모음/문서_법령_링크모음.md`)는 **폐기한 구 PDF 6건**에서
뽑은 것이라 신규 데이터셋 기준으로 다시 뽑는다.

입력: `2026_Finance_DATA_FOR_RAG/창진원/` 42건
      - 원본 PDF 11건은 그대로
      - HWP 26건은 `_hwp변환/` 의 한컴 PDF
      - 배포용 HWP 8건은 `_hwp변환/` 의 hwp5txt TXT (표 손실 감수)

인용 표기가 「」 로만 오지 않는다. 실측: 예비창업 세부관리기준은
"창업사업화 지원사업 통합관리지침(이하 "지침"이라 한다)" 처럼 **괄호 없이** 쓴다.
그래서 (a) 「」 안쪽 (b) 법/법률/시행령/시행규칙/지침/요령/규정/기준 으로 끝나는
한글 어절 덩어리 두 갈래로 잡고, 뒤따르는 조항 표기를 함께 붙인다.

실행: python scripts/extract_kised_refs.py
"""
from __future__ import annotations

import json
import re
import sys
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "2026_Finance_DATA_FOR_RAG" / "창진원"
CONV = ROOT / "_hwp변환"
OUT = ROOT / "법령 PDF" / "_kised_refs_raw.json"

SUFFIX = r"(?:법률|법|시행령|시행규칙|통합관리지침|관리지침|지침|운영요령|요령|관리기준|기준|규정|규칙|고시|훈령|예규)"
# 「」 인용
RE_BRACKET = re.compile(r"[「『]([^」』\n]{2,60})[」』]")
# 괄호 없는 규범명 — 한글/기타 어절이 이어지다 접미사로 끝나는 덩어리
RE_BARE = re.compile(r"([가-힣A-Za-z0-9ㆍ·\u00b7]{2,}(?:\s[가-힣A-Za-z0-9ㆍ·\u00b7]{1,}){0,5}\s?" + SUFFIX + r")(?![가-힣])")
# 뒤따르는 조항
RE_JO = re.compile(r"제\s*\d+\s*조(?:의\s*\d+)?(?:\s*제\s*\d+\s*[항호목])*")

NOISE = re.compile(r"^(이 |본 |해당 |같은 |위 |아래 |다음 |각 |동 |상기 |별도 |관련 |기타 |주요 |세부|국가법|현행법|관계법|타법|제정법)")
DROP = {"법", "법률", "지침", "기준", "규정", "요령", "고시", "훈령", "규칙",
        "관리기준", "관리지침", "세부관리기준", "운영요령", "통합관리지침",
        "시행령", "시행규칙", "본 관리기준", "이 기준", "본 지침", "이 지침"}


def text_of(p: Path) -> str:
    if p.suffix.lower() == ".txt":
        return p.read_text(encoding="utf-8", errors="replace")
    import pdfplumber
    with pdfplumber.open(p) as pdf:
        return "\n".join((pg.extract_text() or "") for pg in pdf.pages)


def sources() -> list[tuple[str, Path]]:
    """(원본 표시경로, 실제 읽을 파일)"""
    out = []
    for f in sorted(SRC.rglob("*")):
        if not f.is_file() or "_정리보류" in f.parts:
            continue
        ext = f.suffix.lower()
        rel = f.relative_to(ROOT)
        if ext == ".pdf":
            out.append((rel.as_posix(), f))
        elif ext in (".hwp", ".hwpx"):
            base = CONV / rel.parent / f.stem
            for cand in (base.with_suffix(".pdf"), base.with_suffix(".txt")):
                if cand.exists():
                    out.append((rel.as_posix(), cand)); break
            else:
                out.append((rel.as_posix(), None))
    return out


def clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" ·ㆍ")


def main() -> None:
    hits: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "docs": set(), "jo": set(), "forms": set()})
    missing = []
    files = sources()
    print("창진원 문서 %d건\n" % len(files))

    for shown, real in files:
        if real is None:
            missing.append(shown)
            print("  ! 변환본 없음:", Path(shown).name[:60])
            continue
        try:
            txt = text_of(real)
        except Exception as e:                                   # noqa: BLE001
            missing.append(shown + " (읽기 실패: %s)" % str(e)[:40])
            continue
        found = 0
        for m in list(RE_BRACKET.finditer(txt)) + list(RE_BARE.finditer(txt)):
            nm = clean(m.group(1))
            if nm in DROP or len(nm) < 4 or NOISE.match(nm):
                continue
            tail = txt[m.end():m.end() + 60]
            e = hits[nm]
            e["count"] += 1
            e["docs"].add(shown)
            e["forms"].add(m.group(0)[:1])
            for j in RE_JO.findall(tail)[:2]:
                e["jo"].add(re.sub(r"\s+", "", j))
            found += 1
        print("  %-64s %5d자 인용 %d" % (Path(shown).name[:64], len(txt), found))

    rows = [{"name": k, "count": v["count"], "docs": sorted(v["docs"]),
             "jo": sorted(v["jo"]), "bracketed": "「" in v["forms"]}
            for k, v in sorted(hits.items(), key=lambda x: -x[1]["count"])]
    OUT.write_text(json.dumps({"refs": rows, "unreadable": missing},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n고유 규범명 후보 %d개 → %s" % (len(rows), OUT.name))
    print("읽지 못한 문서 %d건" % len(missing))
    for r in rows[:25]:
        print("  %-52s %4d회  문서%d  %s" % (r["name"][:52], r["count"],
                                            len(r["docs"]), ",".join(r["jo"][:3])))


if __name__ == "__main__":
    main()
