# -*- coding: utf-8 -*-
"""재도전성공패키지 2026년판(11차 개정) VLM 판독본 → corpus.doc_articles + 판본 역전 교정.

🔴 G 세션이 만들었지만 **G 는 실행하지 않는다.** `corpus.documents` / `corpus.doc_articles`
   는 0831 계약서 §3 소유권 표에 없다 = 전원 읽기 전용이다. A 가 승인하고 돌린다.

왜 필요한가
───────────
`documents` 에 2026년판 행은 있는데 `status='superseded'` 이고 `doc_articles` 는 0건이다.
2025년판이 `active` 를 차지하고 있어 **서비스가 작년 기준으로 판정한다.** 그냥 낡은 게
아니라 판정이 실제로 갈린다 —

  · 외주용역비 사전심의 임계 : 2025 "단건 2천만원" → 2026 "동일 업체 **누적** 2천만원"
    1,500만원 두 건이 구판은 통과, 신판은 심의 대상이다.
  · 창업기업 사업비 조문이 전부 **2조씩 당겨졌다** (2025 제18~23조 → 2026 제16~21조).
    구판 기준으로 룰을 만들면 인용 조번호가 전부 2씩 틀린다.

판독 정확도 근거 (`_재도전_2026_판독.json` §판독_정확도_근거)
──────────────────────────────────────────────────────────
`_scan_inventory.json` 의 A등급 방침 — "텍스트 확보된 직전 판본과 diff 해서 변경분만 검증".
2025년판(extraction=hancom · 35조)과 대조해 **변경 없는 조문이 문자 단위로 일치**함을 확인:
  · 2026 제20조(기계장치) == 2025 제22조(기계장치)
  · 2026 제17조①각호 1~5 == 2025 제19조①각호 1~5
= 판독이 원문을 재현한다는 실증.

그래도 `extraction='vlm'` 은 유지한다. CLAUDE.md 가 VLM 판독본의 **A등급 인용을 금지**하고
A 세션의 `VLM_DOWNGRADE` 강등코드가 이 태그를 보고 발화한다. 판독이 정확한 것과
등급을 올리는 것은 다른 문제다.

무엇을 하는가
─────────────
1. doc_articles 에 33조 적재 (2026년판, 기존 0건 → 33건)
2. documents 2026년판 : status superseded → active, 시행일 → 2026-04-23
3. documents 2025년판 : status active → superseded
4. chunks 는 **건드리지 않는다** (임베딩 20,525건 무영향). 검색 인덱스 재구축은 별건이다.

원문 결함 2건 — 판독본이 원문 그대로 옮긴 것이지 판독 오류가 아니다
───────────────────────────────────────────────────────────────────
  · 조번호 중복 : 9쪽에 제30조가 둘이다(재창업기업의 인수합병 / 해석). 제31조(제재)
    **다음에** 다시 제30조(해석)가 온다. 뒤쪽을 `제30조[2]` 로 적재한다
    (`조번호` 가 doc_id 안에서 유일해야 근거 JSONB 조인이 1행이 된다).
    2025년판에서는 제34조(해석)였다.
  · 별표 부재 : 제17조②가 「공무원 여비 규정」[별표 1] 을 참조하는데 이 PDF 9쪽 안에
    별표가 없다 → 끊긴 참조. 여비 한도를 NULL 로 두고 조건 문장에만 적었다(G1 룰).

알려진 전사 결손 1건 — 판정 룰과 무관
  · 제29조(창업기업등 최종평가) ③ 말미가 잘려 있다("…①과 ②번 항목 중 하나"). 최종평가
    조문이라 사업비 비목 룰이 인용하지 않는다. 룰 근거로 쓰지 말 것.

실행:  PYTHONIOENCODING=utf-8 python scripts/archive/work/_G_재도전2026_적재.py          # 미리보기
       PYTHONIOENCODING=utf-8 python scripts/archive/work/_G_재도전2026_적재.py --commit  # 실적재
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

import json, os, re, sys

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

여기 = os.path.dirname(os.path.abspath(__file__))
판독본 = os.path.join(여기, "_재도전_2026_판독.json")

신 = "2026년 재도전성공패키지 세부관리기준(11차 개정)"
구 = "재도전성공패키지 세부관리기준(2025년)"
시행일 = "2026-04-23"


def 조번호_int(조번호: str) -> int | None:
    """'제30조[2]' → 30. 정렬·범위조회용 보조키라 중복이 허용된다(원문이 중복이다)."""
    m = re.search(r"제\s*(\d+)\s*조", 조번호)
    return int(m.group(1)) if m else None


def 페이지_int(p) -> int | None:
    """'5-6' 같은 범위 표기는 시작 페이지를 쓴다."""
    if isinstance(p, int):
        return p
    m = re.match(r"\s*(\d+)", str(p or ""))
    return int(m.group(1)) if m else None


def main() -> int:
    commit = "--commit" in sys.argv

    with open(판독본, encoding="utf-8") as f:
        판독 = json.load(f)
    조문 = 판독["조문"]

    rows = [
        (신, a["조번호"], a["조제목"], 조번호_int(a["조번호"]),
         a["본문"], 페이지_int(a.get("페이지")), False)
        for a in 조문
    ]

    # 조번호가 doc_id 안에서 유일한가 — 아니면 근거 JSONB 조인이 여러 행을 문다
    키 = [r[1] for r in rows]
    if len(키) != len(set(키)):
        dup = sorted({k for k in 키 if 키.count(k) > 1})
        print(f"🔴 조번호 중복 {dup} — 적재 중단. 판독본에서 구분자를 붙여야 한다")
        return 1

    print(f"== 적재 대상 {len(rows)}조  (doc_id={신!r})")
    for r in rows[:3] + rows[-2:]:
        print(f"   {r[1]:<12} {r[2]:<20} p{r[5]}  {len(r[4]):>4}자")
    print("   ...")

    with psycopg.connect(DSN) as conn:
        기존 = conn.execute(
            "SELECT count(*) FROM corpus.doc_articles WHERE doc_id=%s", (신,)
        ).fetchone()[0]
        print(f"== 기존 doc_articles: {기존}건")
        if 기존:
            print("   🔴 이미 적재돼 있다. 중복 적재를 막기 위해 중단한다.")
            return 1

        if not commit:
            print("\n(미리보기다. 실제로 쓰려면 --commit)")
            for doc, st in ((신, "active"), (구, "superseded")):
                now = conn.execute(
                    "SELECT status FROM corpus.documents WHERE doc_id=%s", (doc,)
                ).fetchone()
                print(f"   documents {doc!r}: {now[0] if now else '없음'} -> {st}")
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO corpus.doc_articles "
                "(doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
            # 판본 역전 해소 — 신판을 올리기 전에 구판을 내린다. 순서가 뒤집히면
            # 중간 상태에서 active 가 둘이 된다.
            cur.execute("UPDATE corpus.documents SET status='superseded' WHERE doc_id=%s", (구,))
            cur.execute("UPDATE corpus.documents SET status='active', 시행일=%s WHERE doc_id=%s",
                        (시행일, 신))
        conn.commit()

        print("\n== 적재 후")
        for r in conn.execute(
                "SELECT doc_id, status, 시행일, extraction, "
                "(SELECT count(*) FROM corpus.doc_articles a WHERE a.doc_id=d.doc_id) "
                "FROM corpus.documents d WHERE doc_id LIKE '%재도전성공패키지 세부관리기준%' "
                "ORDER BY doc_id").fetchall():
            print("  ", r)
        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(f"== corpus.chunks: {n} (건드리지 않았다는 증거)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
