# -*- coding: utf-8 -*-
"""D4 — 평가 결과 적재 (`eval.runs` · `eval.run_items`).

계약 §4 동결 인터페이스:

    eval_store.기록(run: dict, items: list[dict]) -> run_id
      run   {"종류":"e2e|retrieval|judge", "코퍼스버전":str, "설정":{...},
             "문항수":int, "지표":{...}}
      items [{"gold_id":int, "예측":str, "정답":str, "적중":bool, "원출력":{...}}]

**설계 이유 세 가지.**

1. `지표` 와 `원출력` 은 jsonb 통째로 받고 스키마 검증을 하지 않는다.
   종류마다 지표가 다르고(hit@5 vs 일치율 vs 판단불가율) A 가 넣는 원출력 키도
   늘어난다. 컬럼으로 박으면 새 지표마다 DDL 이 필요해지고 DDL 은 D 만 칠 수 있다 —
   밤새 돌리는 스윕이 스키마 변경을 기다리게 되는 건 말이 안 된다.

2. 🔴 `설정` 은 반드시 채운다. 재현의 유일한 근거다.
   내일 아침 "이 숫자가 무엇 때문에 나왔나"를 답할 수 있는 건 이 필드뿐이다.

3. `코퍼스버전` · `git커밋` 은 안 주면 여기서 실측해 채운다.
   사람이 안 적어서 비는 걸 막는다 — 코퍼스가 바뀌면 지표는 비교 불가가 된다.

**금지** — 계약 §10: LLM-as-judge 평가(RAGAS 등)를 여기에 붙이지 않는다.
심판이 LLM 이면 같은 산출물에 다른 점수가 나온다. 채점은 전부 결정론적이다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

import psycopg

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

_종류 = ("e2e", "retrieval", "judge")


def 코퍼스버전(cur) -> str:
    """판정 인덱스의 상태를 한 문자열로 압축한다.

    청크 수만으로는 부족하다 — 같은 수라도 내용이 바뀔 수 있다. 그래서
    (청크수, 임베딩수, refs수, doc수, 최대 chunk_id) 를 해시한다. 싸고 충분하다.
    """
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
        더러움 = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        if out.returncode:
            return None
        # 🔴 워킹트리가 더러우면 커밋 해시만으로는 재현이 안 된다. 그 사실을 남긴다.
        return out.stdout.strip() + ("+dirty" if 더러움.stdout.strip() else "")
    except Exception:
        return None


def 기록(run: dict, items: list[dict] | None = None, *, conn=None) -> int:
    """평가 1회를 남기고 run_id 를 돌려준다. items 는 없어도 된다(집계만 남기는 실행)."""
    종류 = run.get("종류")
    if 종류 not in _종류:
        raise ValueError(f"종류는 {_종류} 중 하나여야 한다: {종류!r}")

    닫기 = conn is None
    conn = conn or psycopg.connect(DSN)
    try:
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
    finally:
        if 닫기:
            conn.close()


def 정답청크(cur, gold_id: int) -> set[int]:
    """고정된 정답 청크 (D3 `eval.golden_chunks`). 원문 재매칭을 하지 않는다.

    🔴 여기가 평가의 결정성을 담보하는 지점이다. 매 실행 원문 부분일치로 되짚으면
       청킹이 바뀔 때마다 정답 집합이 조용히 달라진다.
    """
    cur.execute(
        "SELECT chunk_id FROM eval.golden_chunks WHERE gold_id=%s AND chunk_id IS NOT NULL",
        (gold_id,))
    return {r[0] for r in cur.fetchall()}


def 평가대상(cur, *, 세트: str | None = None) -> list[dict]:
    """정답 청크가 고정된 문항만. 분모가 매 실행 흔들리지 않게 여기서 한 번에 정한다."""
    cur.execute(
        """SELECT g.gold_id, g.세트, g.사업명, g.적용범위, g.질문, g.정답판정, g.비목
             FROM eval.golden_set g
            WHERE EXISTS (SELECT 1 FROM eval.golden_chunks gc
                           WHERE gc.gold_id = g.gold_id AND gc.chunk_id IS NOT NULL)
              AND (%s::text IS NULL OR g.세트 = %s::text)
            ORDER BY g.gold_id""", (세트, 세트))
    cols = ("gold_id", "세트", "사업명", "적용범위", "질문", "정답판정", "비목")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def 사업키(사업명: str | None) -> str | None:
    """🔴 D2 이후의 규약. 공통 문항은 사업명이 NULL 이고 적용범위에 원표기가 있다.

    D2 이전 코드는 '공통(지침 제14차)' 를 사업명으로 넘겨 존재하지 않는 사업으로
    필터링하고 있었다. 방어적으로 여기서 한 번 더 접는다.
    """
    if not 사업명 or 사업명.startswith("공통"):
        return None
    return 사업명


def 요약(run_id: int, *, conn=None) -> dict:
    """적재된 실행 하나를 사람이 읽는 형태로 되읽는다 (검증용)."""
    닫기 = conn is None
    conn = conn or psycopg.connect(DSN)
    try:
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
    finally:
        if 닫기:
            conn.close()


if __name__ == "__main__":
    # 스모크 — 실제 표에 1행 넣고 되읽는다. 지표는 빈 dict 라 지표 판독에 섞이지 않는다.
    rid = 기록({"종류": "judge", "설정": {"smoke": True}, "지표": {},
               "라벨": "eval_store 스모크", "비고": "D4 자체 점검"}, [])
    print("run_id =", rid)
    for k, v in 요약(rid).items():
        print(f"  {k:10} {v}")
