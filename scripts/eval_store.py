# -*- coding: utf-8 -*-
"""평가 결과 적재 (`eval.runs` · `eval.run_items`).

`지표`·`원출력` 은 jsonb 로 통째로 받는다. `코퍼스버전`·`git커밋` 은 안 주면 여기서 실측해 채운다.
채점은 전부 결정론적이며 LLM-as-judge 는 붙이지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db                                                   # noqa: E402

DSN = db.DSN

_종류 = ("e2e", "retrieval", "judge")


def 코퍼스버전(cur) -> str:
    """판정 인덱스 상태를 한 문자열로 압축한다 — (청크수, 임베딩수, refs수, doc수, 최대 chunk_id) 해시."""
    cur.execute("""
        SELECT (SELECT count(*) FROM corpus.chunks),
               (SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL),
               (SELECT count(*) FROM corpus.refs),
               (SELECT count(*) FROM corpus.documents),
               (SELECT coalesce(max(chunk_id),0) FROM corpus.chunks)""")
    n = cur.fetchone()
    h = hashlib.sha1("|".join(map(str, n)).encode()).hexdigest()[:8]
    return f"c{n[0]}-e{n[1]}-r{n[2]}-d{n[3]}-{h}"


def 골든고정(cur) -> str:
    """정답지(`eval.golden_chunks`·`eval.golden_set`) 상태를 한 문자열로 압축한다.

    행 수·매칭방법 분포·정답판정 분포를 함께 해시한다. 이 지문이 다른 run 끼리는 hit@k 를 비교하지 않는다.
    """
    cur.execute("""
        SELECT (SELECT count(*) FROM eval.golden_chunks),
               (SELECT count(*) FROM eval.golden_chunks WHERE 매칭방법='실패'),
               (SELECT count(DISTINCT gold_id) FROM eval.golden_chunks),
               (SELECT coalesce(max(gc_id),0) FROM eval.golden_chunks)""")
    n = cur.fetchone()
    cur.execute("SELECT 매칭방법, count(*) FROM eval.golden_chunks GROUP BY 1 ORDER BY 1")
    분포 = cur.fetchall()

    # golden_set 의 정답 라벨이 바뀌어도 지문이 움직이도록 씨앗에 더한다.
    cur.execute("SELECT count(*), count(*) FILTER (WHERE verified) FROM eval.golden_set")
    총, 검증됨 = cur.fetchone()
    cur.execute("SELECT 정답판정, count(*) FROM eval.golden_set GROUP BY 1 ORDER BY 1")
    판정분포 = cur.fetchall()

    씨앗 = ("|".join(map(str, n)) + "|" + ",".join(f"{k}:{v}" for k, v in 분포)
            + f"|s{총}v{검증됨}|" + ",".join(f"{k}:{v}" for k, v in 판정분포))
    h = hashlib.sha1(씨앗.encode()).hexdigest()[:8]
    return f"g{n[0]}-f{n[1]}-q{n[2]}-s{총}v{검증됨}-{h}"


def _git커밋() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        더러움 = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        if out.returncode:
            return None
        # 워킹트리가 더러우면 커밋 해시만으로는 재현이 안 되므로 +dirty 를 붙인다.
        return out.stdout.strip() + ("+dirty" if 더러움.stdout.strip() else "")
    except Exception:
        return None


def 기록(run: dict, items: list[dict] | None = None, *, conn=None) -> int:
    """평가 1회를 남기고 run_id 를 돌려준다. items 는 없어도 된다(집계만 남기는 실행)."""
    종류 = run.get("종류")
    if 종류 not in _종류:
        raise ValueError(f"종류는 {_종류} 중 하나여야 한다: {종류!r}")

    with db.borrow(conn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO eval.runs
                 (종류, 코퍼스버전, git커밋, 설정, 문항수, 지표, 라벨, 비고, 종료)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
               RETURNING run_id""",
            (종류,
             run.get("코퍼스버전") or 코퍼스버전(cur),
             run.get("git커밋") or _git커밋(),
             json.dumps(run.get("설정") or {}, ensure_ascii=False),
             run.get("문항수") if run.get("문항수") is not None
                 else (len(items) if items is not None else None),
             json.dumps(run.get("지표") or {}, ensure_ascii=False),
             run.get("라벨"),
             run.get("비고")),
        )
        run_id = cur.fetchone()[0]

        for it in items or []:
            cur.execute(
                """INSERT INTO eval.run_items (run_id, gold_id, 예측, 정답, 적중, 원출력)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, gold_id) DO UPDATE SET
                     예측=EXCLUDED.예측, 정답=EXCLUDED.정답,
                     적중=EXCLUDED.적중, 원출력=EXCLUDED.원출력""",
                (run_id, it.get("gold_id"), it.get("예측"), it.get("정답"),
                 it.get("적중"),
                 json.dumps(it.get("원출력") or {}, ensure_ascii=False, default=str)),
            )
        conn.commit()
        return run_id


def 정답청크(cur, gold_id: int) -> set[int]:
    """고정된 정답 청크(`eval.golden_chunks`). 원문 재매칭을 하지 않는다 — 청킹이 바뀌어도 정답 집합이 안 흔들린다."""
    cur.execute(
        "SELECT chunk_id FROM eval.golden_chunks WHERE gold_id=%s AND chunk_id IS NOT NULL",
        (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 평가대상(cur, *, 세트: str | None = None, 범위밖포함: bool = False) -> list[dict]:
    """평가 분모가 되는 문항 목록.

    정답 청크가 고정된 문항에 더해 `정답판정='판단불가'` 와 `세트='L3'` 는 골든청크 없이도 든다
    (둘 다 인용적중은 `eval_e2e` 가 채점에서 뺀다). `평가범위='범위밖…'` 은 `범위밖포함=True` 가 아니면 뺀다.
    """
    cur.execute(
        """SELECT g.gold_id, g.세트, g.사업명, g.적용범위, g.질문, g.정답판정, g.비목,
                  g.대상, g.평가범위, g.채점모드, g.해야할일
             FROM eval.golden_set g
            WHERE (g.정답판정 = '판단불가'
                   OR g.세트 = 'L3'
                   OR EXISTS (SELECT 1 FROM eval.golden_chunks gc
                               WHERE gc.gold_id = g.gold_id AND gc.chunk_id IS NOT NULL))
              AND (%s::text IS NULL OR g.세트 = %s::text)
              AND (%s OR g.평가범위 IS NULL OR g.평가범위 NOT LIKE '범위밖%%')
            ORDER BY g.gold_id""", (세트, 세트, 범위밖포함))
    cols =("gold_id", "세트", "사업명", "적용범위", "질문", "정답판정", "비목",
            "대상", "평가범위", "채점모드", "해야할일")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def 사업키(사업명: str | None) -> str | None:
    """공통 문항('공통…')은 사업명 NULL 로 접는다 — 존재하지 않는 사업으로 필터링되는 것을 막는다."""
    if not 사업명 or 사업명.startswith("공통"):
        return None
    return 사업명


def 요약(run_id: int, *, conn=None) -> dict:
    """적재된 실행 하나를 사람이 읽는 형태로 되읽는다 (검증용)."""
    with db.borrow(conn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 종류, 시작, 코퍼스버전, git커밋, 문항수, 지표, 설정, 라벨 "
                    "FROM eval.runs WHERE run_id=%s", (run_id,))
        r = cur.fetchone()
        if not r:
            raise LookupError(f"run_id={run_id} 없음")
        cur.execute("SELECT count(*), count(*) FILTER (WHERE 적중) "
                    "FROM eval.run_items WHERE run_id=%s", (run_id,))
        n, hit = cur.fetchone()
        return {"run_id": run_id, "종류": r[0], "시작": r[1], "코퍼스버전": r[2],
                "git커밋": r[3], "문항수": r[4], "지표": r[5], "설정": r[6], "라벨": r[7],
                "items": n, "적중": hit}


if __name__ == "__main__":
    # 스모크 — 실제 표에 1행 넣고 되읽는다.
    rid = 기록({"종류": "judge", "설정": {"smoke": True}, "지표": {},
               "라벨": "eval_store 스모크", "비고": "D4 자체 점검"}, [])
    print("run_id =", rid)
    for k, v in 요약(rid).items():
        print(f"  {k:10} {v}")
