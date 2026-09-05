# -*- coding: utf-8 -*-
"""`corpus.documents` 의 `version` · `시행일` · `doc_type` 을 doc_id 에서 뽑아 채운다.

## 왜 필요한가

283행 전부 NULL 이다. 원인은 두 겹이다:
  · `_stage0_articles.json` 문서 레코드에 해당 필드가 **애초에 없다**
    (키: doc_id·layer·strategy·조·삭제조·최장·quality·flags·path·규범)
  · `load_db.py:89` 가 그 자리에 하드코딩 `None` 을 넣는다

🔴 **`LLM.md` §3-4 [2겹] 이 판정 응답의 `버전스탬프` 를 `documents.version` 에서 만든다** —
   "제14차, 2025.12.23 기준". 지금은 그 문장을 만들 수 없다.

## 무엇을 하나

**재파싱하지 않는다.** doc_id 문자열에 이미 들어 있다:

    L1_개인정보보호법_20251002                       -> 시행일 2025-10-02
    L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223  -> 제14차개정, 2025-12-23
    예비창업패키지 세부관리기준(2025년)                 -> 2025년

🔴 **추측으로 채우지 않는다.** 확실한 패턴만 잡고 나머지는 NULL 로 남긴다 —
   틀린 버전스탬프("제13차 기준입니다")는 없는 것보다 나쁘다. 인용의 신뢰를 무너뜨린다.
   못 채운 문서는 목록으로 보고한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/indexing/backfill_doc_meta.py          # dry-run
    PYTHONIOENCODING=utf-8 python scripts/archive/indexing/backfill_doc_meta.py --apply
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


import argparse
import os
import re
import sys

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ── doc_type — 이름에 든 규범 종류. 긴 것부터 본다 (시행규칙 이 규칙 보다 먼저) ──
#    법령 제명 규칙상 이 접미사는 뒤에 붙으므로 오탐이 거의 없다.
DOC_TYPE = [
    ("시행규칙", "시행규칙"), ("시행령", "시행령"),
    ("세부관리기준", "세부관리기준"), ("관리기준", "관리기준"),
    ("통합관리지침", "지침"), ("운영지침", "지침"), ("지침", "지침"),
    ("운영요령", "요령"), ("요령", "요령"),
    ("고시", "고시"), ("훈령", "훈령"), ("예규", "예규"), ("규정", "규정"),
    ("모집공고", "공고"), ("통합공고문", "공고"), ("공고", "공고"),
    ("사례집", "사례집"), ("가이드라인", "가이드라인"),
    ("법률", "법률"), ("법", "법률"),          # 「…법」 은 맨 뒤에 — 부분일치가 넓다
]


def doc_type_of(doc_id: str) -> str | None:
    for 패턴, 값 in DOC_TYPE:
        if 패턴 in doc_id:
            return 값
    return None


def 시행일_version(doc_id: str) -> tuple[str | None, str | None]:
    """(시행일 'YYYY-MM-DD', version 표시문자열). 확실한 것만 돌려준다."""
    시행일 = None
    version_조각: list[str] = []

    # ① 끝의 _YYYYMMDD — 법령 XML 계열 228/235 가 이 형태다
    m = re.search(r"_(\d{4})(\d{2})(\d{2})$", doc_id)
    if not m:
        # ② (YYYYMMDD) — "보조금 관리에 관한 법률(법률)(제21751호)(20260602)"
        m = re.search(r"\((\d{4})(\d{2})(\d{2})\)", doc_id)
    if m:
        y, mo, d = m.groups()
        if 1990 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            시행일 = f"{y}-{mo}-{d}"
            version_조각.append(f"{y}.{int(mo)}.{int(d)}")

    # ③ 개정 차수 — "제14차개정", "제10차", "11차 개정", "3차 개정안"
    #    🔴 "차" 앞의 숫자를 무조건 개정 차수로 보면 안 된다. 실측 오탐:
    #       "「모두의 창업 프로젝트」 통합 모집공고 2차"  ->  version="제2차" (X)
    #       이건 공고 회차이지 규범의 개정 차수가 아니다.
    #       **뒤에 '개정' 이 붙거나 '제N차' 형태일 때만** 채택한다.
    #       틀린 버전스탬프("제2차 기준입니다")는 없는 것보다 나쁘다.
    c = (re.search(r"(\d+)\s*차\s*개정", doc_id)     # "11차 개정", "제14차개정", "3차 개정안"
         or re.search(r"제\s*(\d+)\s*차(?!\s*(?:모집|공고))", doc_id))  # "제10차"
    if c:
        version_조각.insert(0, f"제{c.group(1)}차")

    # ④ 연도만 있는 것 — "세부관리기준(2025년)", "2024년 창업중심대학…"
    if 시행일 is None:
        y = re.search(r"\((\d{4})년\)", doc_id) or re.search(r"^(\d{4})년", doc_id)
        if y and 1990 <= int(y.group(1)) <= 2100:
            version_조각.append(f"{y.group(1)}년")
            # 🔴 시행일은 채우지 않는다. 연도만으로 날짜를 지어내면 안 된다

    # ⑤ 호수 — "제2025-648호", "제21751호"
    h = re.search(r"제\s*(\d{4}-\d+|\d+)\s*호", doc_id)
    if h and not version_조각:
        version_조각.append(f"제{h.group(1)}호")

    return 시행일, (", ".join(version_조각) or None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT doc_id, layer FROM corpus.documents ORDER BY doc_id").fetchall()

        upd, 미해결 = [], []
        통계 = {"시행일": 0, "version": 0, "doc_type": 0}
        for doc_id, layer in rows:
            시행일, ver = 시행일_version(doc_id)
            dt = doc_type_of(doc_id)
            if 시행일: 통계["시행일"] += 1
            if ver:   통계["version"] += 1
            if dt:    통계["doc_type"] += 1
            if not (시행일 or ver or dt):
                미해결.append((doc_id, layer))
            upd.append((시행일, ver, dt, doc_id))

        n = len(rows)
        print(f"문서 {n}행")
        for k, v in 통계.items():
            print(f"  {k:9} 채움 {v:3} / {n}  ({v/n*100:.1f}%)  · 남는 NULL {n-v}")
        print(f"\n셋 다 못 채운 문서 {len(미해결)}건" + (":" if 미해결 else " (없음)"))
        for d, l in 미해결[:12]:
            print(f"    [{l}] {d[:66]}")

        # 표본을 눈으로 확인한다 — 규칙이 엉뚱한 값을 만들지 않았는지
        print("\n표본 (layer 별 2건씩):")
        seen: dict[str, int] = {}
        for 시행일, ver, dt, doc_id in upd:
            l = dict(rows)[doc_id]
            if seen.get(l, 0) >= 2:
                continue
            seen[l] = seen.get(l, 0) + 1
            print(f"    [{l}] {doc_id[:52]:54} 시행일={시행일} · version={ver} · type={dt}")

        if not a.apply:
            print("\n(dry-run. --apply 를 주면 UPDATE 한다)")
            return

        with conn.cursor() as cur:
            cur.executemany("""UPDATE corpus.documents
                                  SET 시행일 = %s::date, version = %s, doc_type = %s
                                WHERE doc_id = %s""", upd)
            갱신 = cur.rowcount
        # chunks 에도 version 사본이 있다 — documents 에서 통째로 동기화한다
        with conn.cursor() as cur:
            cur.execute("""UPDATE corpus.chunks c SET version = d.version
                             FROM corpus.documents d WHERE d.doc_id = c.doc_id""")
            청크 = cur.rowcount
        conn.commit()
        print(f"\nUPDATE documents {갱신}행 · chunks.version 동기화 {청크:,}행")

        남 = conn.execute("""SELECT count(*) FILTER (WHERE version IS NULL),
                                    count(*) FILTER (WHERE 시행일 IS NULL),
                                    count(*) FILTER (WHERE doc_type IS NULL)
                               FROM corpus.documents""").fetchone()
        print(f"남은 NULL — version {남[0]} · 시행일 {남[1]} · doc_type {남[2]}")


if __name__ == "__main__":
    main()
