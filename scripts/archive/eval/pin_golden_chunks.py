# -*- coding: utf-8 -*-
"""D3 — 정답셋 정답근거 ↔ chunk_id 고정 (`eval.golden_chunks`).

**왜 고정하는가.**
지금 `eval_retrieval.py` 는 평가할 때마다 `정답근거.원문` 앞 40자를 정규화해
`chunks.text` 에 부분일치로 되짚는다. 그래서

  · 청킹을 다시 돌리면 정답 집합이 조용히 달라지고 hit@5 가 흔들린다
  · 역추적 실패 문항이 매번 "평가 제외" 로 빠져 분모가 바뀐다 (계약 §7 의
    "평가 문항 수가 바뀌면 hit율을 직접 비교하면 안 된다" 에 정확히 걸린다)
  · 실패가 **매칭 버그**인지 **규정 모음 결손**인지 갈리지 않는다

한 번 박아두면 평가가 결정적이 되고, 실패는 사유와 함께 표에 남는다.

**매칭 사다리** (앞 단계가 잡으면 뒤는 안 본다)

    원문일치  doc_id + 정규화 원문 앞 40자 부분일치        ← 가장 정확
    조번호    doc_id + 조번호(제N조)                        ← 항호가 청크와 어긋날 때
    조제목    doc_id 안에서 조제목 재매칭 (구판 조번호 대응)  ← RAG.md `shifted`
    실패      chunk_id NULL + 실패사유                       ← 숨기지 않는다

🔴 추측으로 채우지 않는다. 못 찾으면 '실패' 로 남기고 사유를 적는다.
   틀린 정답 매핑은 없는 것보다 나쁘다 — 지표를 조용히 부풀린다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/pin_golden_chunks.py          # 미고정만
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/pin_golden_chunks.py --재고정  # 전건 다시
    PYTHONIOENCODING=utf-8 python scripts/archive/eval/pin_golden_chunks.py --dry     # 쓰지 않고 보기
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

# eval_retrieval.정규화() 와 **같은 규칙**이어야 한다. 다르면 고정 결과와 평가가 어긋난다.
_잡문자 = re.compile(r"[\s　·,.\"'()（）「」『』]")
_SQL정규화 = r"regexp_replace(text, '[\s·,.\"''()（）「」『』]', '', 'g')"


def 정규화(s: str) -> str:
    return _잡문자.sub("", s or "")


def 조만(조번호: str) -> str | None:
    """'제20조①' · '제20조(1)' → '제20조'. 항호는 청크 경계와 어긋나므로 떼어낸다."""
    m = re.match(r"(제\d+조(?:의\d+)?)", 조번호 or "")
    return m.group(1) if m else None


# 🔴 정답셋의 정답근거 좌표는 조문만이 아니다. 실측(2026-08-31) 결과 역추적 실패 12건이
#    전부 부속물이었다: '[붙임2] 외주용역비 유의사항' · '참고2' · '별표1' · '붙임2'.
#    chunks.조번호 는 이걸 '붙임2' · '참고2' · '별표1' 로 정규화해 들고 있다.
#    괄호·공백만 다르므로 표기를 맞춰주면 그대로 잡힌다.
_부속물 = re.compile(r"(붙임|별표|별지|참고|서식)\s*(\d+(?:의\d+)?)")


def 부속물만(조번호: str) -> str | None:
    """'[붙임2] 외주용역비 유의사항' → '붙임2'. 못 찾으면 None."""
    m = _부속물.search(조번호 or "")
    return f"{m.group(1)}{m.group(2)}" if m else None


def 매칭(cur, doc: str | None, 조번호: str | None, 원문: str | None):
    """→ (매칭방법, [chunk_id], 실패사유|None)"""
    조 = 조만(조번호)

    if not doc:
        return "실패", [], "정답근거에 doc 이 없다 (골든셋 결손)"

    # 🔴 2026-09-07(ai-33 실측 정정) — L3 는 «코퍼스 결손이 아니다».
    #    아래 분기가 "documents 에도 없다 — 코퍼스 결손" 이라고 단언하는 바람에
    #    L3 27건이 «규정이 없다» 로 오진됐다. 실제로는 tenant.l3_articles 에
    #    224조가 정상 적재돼 있고(파싱품질 warn·dangling 0), corpus.* 만 보는
    #    이 스크립트가 그 스키마를 안 볼 뿐이다. 하네스 결손이지 코퍼스 결손이 아니다.
    if (doc or "").startswith("L3_"):
        return "실패", [], (
            f"doc_id='{doc}' 는 L3 문서다 — tenant.l3_articles 에 있고 이 스크립트"
            "(corpus.* 전용)로는 못 잡는다. «코퍼스 결손이 아니다» — L3 는 pin 대상이"
            " 아니라 eval_store.평가대상() 이 별도 경로로 통과시킨다.")

    cur.execute("SELECT count(*) FROM corpus.chunks WHERE doc_id = %s", (doc,))
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT count(*) FROM corpus.documents WHERE doc_id = %s", (doc,))
        있음 = cur.fetchone()[0]
        return "실패", [], (
            f"doc_id='{doc}' 의 청크가 0건 "
            + ("(documents 에는 있다 — 청킹 결손)" if 있음 else "(documents 에도 없다 — 코퍼스 결손)")
        )

    # ① 원문일치
    핵심 = 정규화(원문)[:40]
    if 핵심:
        cur.execute(
            f"SELECT chunk_id FROM corpus.chunks "
            f"WHERE doc_id = %s AND {_SQL정규화} LIKE '%%' || %s || '%%' ORDER BY chunk_id",
            (doc, 핵심),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            return "원문일치", ids, None

    # ② 조번호 (조문 좌표 또는 부속물 좌표)
    부속 = 부속물만(조번호)  # 조문 좌표가 아니면 부속물 좌표일 수 있다
    for 키 in (조, 부속):
        if not 키:
            continue
        cur.execute(
            "SELECT chunk_id FROM corpus.chunks WHERE doc_id=%s AND 조번호=%s ORDER BY chunk_id",
            (doc, 키),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            return "조번호", ids, None

    # ③ 조제목 재매칭 — 구판 조번호가 밀렸을 때(RAG.md `shifted`).
    #    doc 안에서 조번호로 doc_articles 의 조제목을 얻고, 같은 조제목의 다른 조번호를 찾는다.
    if 조:
        cur.execute(
            "SELECT 조제목 FROM corpus.doc_articles "
            "WHERE doc_id=%s AND 조번호=%s AND 조제목 IS NOT NULL",
            (doc, 조),
        )
        r = cur.fetchone()
        제목 = r[0] if r else None
        if 제목:
            cur.execute(
                "SELECT c.chunk_id FROM corpus.chunks c "
                "JOIN corpus.doc_articles a ON a.article_id = c.article_id "
                "WHERE c.doc_id=%s AND a.조제목=%s ORDER BY c.chunk_id",
                (doc, 제목),
            )
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                return "조제목", ids, None

    사유 = f"doc_id='{doc}' 안에서 못 찾음"
    if 조:
        사유 += f" (조번호 '{조}' 도 chunks 에 없다 — 조문 재조립 결손 의심)"
    elif 부속:
        # 🔴 여기서 «규정 모음 결손» 이라고 단정하면 안 된다 (2026-09-01).
        #    이 스크립트가 아는 것은 «청크에 없다» 하나뿐이고, 그 원인은 최소 넷이다:
        #      ① 정말 미적재  ② 별표·박스표라 `표인가()` 가 설계대로 잘랐다
        #         (CLAUDE.md 「별표·한도표는 RAG 아닌 룰 테이블로」 — 넣으면 확정 원칙 위반)
        #      ③ 범위 밖 자료다 (모두의창업 별지③④ = 로컬트랙)
        #      ④ 좌표 표기가 다르다
        #    실제로 이 라벨을 그대로 믿고 적재를 지시한 3건이 전부 ②③ 이었다.
        #    **원인을 확정하지 않고 관측만 적는다.** 판정은 사람이 doc 을 열고 한다.
        cur.execute(
            "SELECT count(*) FROM corpus.chunks WHERE doc_id=%s AND 조번호 !~ '^제[0-9]'", (doc,)
        )
        n = cur.fetchone()[0]
        사유 += (
            f" (관측: 부속물 '{부속}' 이 chunks 에 없다 · 이 doc 의 부속물 청크 {n}건."
            " 원인 미확정 — 미적재 / 별표라 설계대로 컷 / 범위 밖 / 표기 불일치 중 하나."
            " 적재를 지시하기 전에 원문을 열어 갈라라)"
        )
    else:
        사유 += " (정답근거에 조/부속물 좌표가 없고 원문 부분일치도 실패 — 원문 표기 불일치 의심)"
    return "실패", [], 사유


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--재고정", action="store_true", help="기존 고정을 지우고 전건 다시")
    ap.add_argument("--dry", action="store_true", help="쓰지 않고 결과만 본다")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        if a.재고정 and not a.dry:
            cur.execute("DELETE FROM eval.golden_chunks")
            print(f"기존 고정 {cur.rowcount}행 삭제")

        cur.execute("SELECT gold_id FROM eval.golden_chunks")
        기고정 = {r[0] for r in cur.fetchall()}

        cur.execute(
            "SELECT gold_id, 세트, 사업명, 적용범위, 질문, 정답근거 "
            "FROM eval.golden_set ORDER BY gold_id"
        )
        rows = cur.fetchall()

        통계 = {"원문일치": 0, "조번호": 0, "조제목": 0, "실패": 0}
        문항실패: list[tuple] = []
        건너뜀 = 0
        쓴행 = 0

        for gid, 세트, 사업, 범위, 질문, 근거 in rows:
            if gid in 기고정 and not a.재고정:
                건너뜀 += 1
                continue
            근거 = 근거 or []
            if not 근거:
                # 정답근거 자체가 없는 문항. 실패로 1행 남긴다 — 분모에서 조용히 빠지지 않게.
                if not a.dry:
                    cur.execute(
                        "INSERT INTO eval.golden_chunks "
                        "(gold_id, 근거순번, 매칭방법, 실패사유) VALUES (%s,0,'실패',%s) "
                        "ON CONFLICT DO NOTHING",
                        (gid, "정답근거가 NULL/빈 배열 (골든셋 결손)"),
                    )
                    쓴행 += cur.rowcount
                통계["실패"] += 1
                문항실패.append((gid, 세트, 질문, "정답근거 없음"))
                continue

            문항_성공 = False
            for i, g in enumerate(근거):
                doc, 조, 원문 = g.get("doc"), g.get("조번호"), g.get("원문")
                방법, ids, 사유 = 매칭(cur, doc, 조, 원문)
                통계[방법] += 1
                if 방법 == "실패":
                    문항실패.append((gid, 세트, 질문, 사유))
                    if not a.dry:
                        cur.execute(
                            "INSERT INTO eval.golden_chunks "
                            "(gold_id, 근거순번, doc_id, 조번호, 매칭방법, 실패사유) "
                            "VALUES (%s,%s,%s,%s,'실패',%s) ON CONFLICT DO NOTHING",
                            (gid, i, doc, 조, 사유),
                        )
                        쓴행 += cur.rowcount
                    continue
                문항_성공 = True
                if a.dry:
                    continue
                for cid in ids:
                    cur.execute(
                        "INSERT INTO eval.golden_chunks "
                        "(gold_id, 근거순번, chunk_id, article_id, doc_id, 조번호, 매칭방법) "
                        "SELECT %s, %s, c.chunk_id, c.article_id, c.doc_id, %s, %s "
                        "  FROM corpus.chunks c WHERE c.chunk_id = %s "
                        "ON CONFLICT DO NOTHING",
                        (gid, i, 조, 방법, cid),
                    )
                    쓴행 += cur.rowcount
            del 문항_성공

        if not a.dry:
            conn.commit()

        print(f"\n골든셋 {len(rows)}문항 (기고정 건너뜀 {건너뜀}) · 삽입 {쓴행}행")
        print("근거 단위 매칭 결과")
        for k in ("원문일치", "조번호", "조제목", "실패"):
            print(f"  {k:8} {통계[k]:4}건")

        if 문항실패:
            print(f"\n🔴 역추적 실패 {len(문항실패)}건 — 추측으로 채우지 않고 사유와 함께 남겼다")
            for gid, 세트, 질문, 사유 in 문항실패:
                print(f"  gold_id={gid:3} [{세트}] {(질문 or '')[:40]}")
                print(f"        └ {사유}")

        # 문항 단위 요약 — 평가 분모가 몇 문항인지가 여기서 정해진다
        cur.execute("""
            SELECT count(DISTINCT gold_id) FILTER (WHERE chunk_id IS NOT NULL),
                   count(DISTINCT gold_id)
              FROM eval.golden_chunks""")
        유효, 전체 = cur.fetchone()
        print(f"\n고정 완료: 정답 청크를 가진 문항 {유효} / 고정 시도 {전체}")
        print("  → eval_retrieval / eval_e2e 는 이 표를 분모로 쓴다. 매 실행 재계산 없음.")


if __name__ == "__main__":
    main()
