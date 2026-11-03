# -*- coding: utf-8 -*-
"""평가 결과를 `eval.runs` / `eval.run_items` 에 저장한다.

`eval_retrieval.py`, 판정 파이프라인 골든셋 실행(`orchestrate.py --eval-log`)이 호출한다.
공개 함수: `기록(run, items, *, conn)`, `코퍼스버전(cur)`, `평가용_사업명(사업명, 적용범위)`.
`지표`·`원출력` 은 jsonb 로 그대로 저장하고, `코퍼스버전`·`git커밋` 을 주지 않으면 여기서 계산해 채운다.
채점은 전부 결정론적이며 LLM-as-judge 는 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db                                                   # noqa: E402

_종류 = ("e2e", "retrieval", "judge")


def 코퍼스버전(cur) -> str:
    """코퍼스 전체 상태를 한 문자열로 압축한다 — (청크수, 임베딩수, refs수, doc수, 최대 chunk_id) 해시."""
    cur.execute("""
        SELECT (SELECT count(*) FROM corpus.chunks),
               (SELECT count(*) FROM corpus.chunks WHERE embedding IS NOT NULL),
               (SELECT count(*) FROM corpus.refs),
               (SELECT count(*) FROM corpus.documents),
               (SELECT coalesce(max(chunk_id),0) FROM corpus.chunks)""")
    n = cur.fetchone()
    h = hashlib.sha1("|".join(map(str, n)).encode()).hexdigest()[:8]
    return f"c{n[0]}-e{n[1]}-r{n[2]}-d{n[3]}-{h}"


def _git커밋() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        변경있음 = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        if out.returncode:
            return None
        # 커밋 안 된 변경이 있으면 커밋 해시만으로는 재현이 안 되므로 +dirty 를 붙인다.
        return out.stdout.strip() + ("+dirty" if 변경있음.stdout.strip() else "")
    except Exception:
        return None


def 기록(run: dict, items: list[dict] | None = None, *, conn=None) -> int:
    """평가 1회를 저장하고 run_id 를 반환한다. items 는 없어도 된다(집계만 남기는 실행).

    `run["종류"]` 가 허용 값이 아니면 ValueError. `conn` 을 주지 않으면 새 커넥션을 열고 닫는다.
    끝에 commit 한다.
    """
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


def 평가용_사업명(사업명: str | None, 적용범위: str | None = None) -> str | None:
    """골든셋 문항의 사업명을 검색 사업 필터에 넣을 값으로 바꾼다. 사업을 특정하지 않는 문항이면 None.

    None 이 되는 경우: `적용범위` 가 있다, 사업명이 비었다, 사업명이 '공통…' 이다. 뒤의 둘은 사업 이름이 아니라
    적용범위 표시라 그대로 사업 필터에 넣으면 존재하지 않는 사업으로 걸러 후보가 0건이 된다.
    eval_retrieval 과 eval_e2e 가 같은 규칙을 쓴다.
    """
    if 적용범위 or not 사업명 or 사업명.startswith("공통"):
        return None
    return 사업명
