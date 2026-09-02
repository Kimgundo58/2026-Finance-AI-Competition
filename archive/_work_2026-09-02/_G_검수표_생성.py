# -*- coding: utf-8 -*-
"""`_G_룰검수요청.md` 의 **행 ↔ 근거 조문 원문 대조표**를 생성한다.

왜 스크립트로 뽑는가 — 조문 원문을 손으로 옮겨 적으면 그 자체가 오탈자 위험이고,
검수의 대조 기준이 흔들린다. `doc_articles` 에서 직접 읽어 붙인다.
재도전 2026년판은 아직 doc_articles 에 없으므로(A 가 적재 예정) `_재도전_2026_판독.json`
에서 읽고 **미적재 표시**를 단다.

실행:  PYTHONIOENCODING=utf-8 python scripts/_work/_G_검수표_생성.py > /dev/null
       (실제로는 아래 OUT 경로에 쓴다)
"""
from __future__ import annotations
import json, os, sys

여기 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(여기))
import psycopg
import seed_rules as SR

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
OUT = os.path.join(여기, "_G_룰검수요청_대조표.md")


def 판독본_조문() -> dict[str, dict]:
    with open(os.path.join(여기, "_재도전_2026_판독.json"), encoding="utf-8") as f:
        d = json.load(f)
    return {a["조번호"]: a for a in d["조문"]}


def main() -> int:
    판독 = 판독본_조문()
    # 🔴 autocommit — 8세션 병렬 중 읽기 트랜잭션을 붙들면 남의 DDL 과 교착난다.
    #    2026-09-01 실측: B 의 idle-in-transaction 886초가 D 의 04_agent.sql 을 막고,
    #    그 DDL 이 corpus.rules 에 AccessExclusiveLock 을 쥔 채 대기해 **아무도 rules 를
    #    읽지 못하는** 상태가 됐다. 조회용 커넥션은 트랜잭션을 열지 않는다.
    with psycopg.connect(DSN, autocommit=True) as conn:
        db = {}
        for doc, 조, 제목, 본문 in conn.execute(
                "SELECT doc_id, 조번호, 조제목, 본문 FROM corpus.doc_articles").fetchall():
            db[(doc, 조)] = (제목, 본문)

    행들 = list(SR.rows())
    out = []
    w = out.append

    w("# 룰 ↔ 근거 조문 원문 대조표 (자동 생성)")
    w("")
    w("`scripts/_work/_G_검수표_생성.py` 산출. **손으로 고치지 마라** — 다시 생성하면 덮인다.")
    w("고칠 것이 있으면 `scripts/seed_rules.py` 를 고치고 이 스크립트를 다시 돌린다.")
    w("")
    w("각 룰 행 아래에 그 행이 인용한 `(doc_id, 조번호)` 의 **원문을 그대로** 붙였다.")
    w("검수는 «룰의 값이 원문에서 실제로 읽히는가» 한 가지만 보면 된다.")
    w("")

    # G 가 이번에 새로 만든 것만 — 기존 54행은 이미 오너 검수를 통과했다
    대상 = [r for r in 행들 if r[0] == "L1" or r[2] == "재도전성공패키지"]
    w(f"대상 **{len(대상)}행** — G 가 2026-08-31 에 새로 만든 행만 담았다.")
    w("기존 54행(예비·초기·초격차·대학·도약·모두의창업)은 오너 검수를 이미 통과했고 값을 바꾸지 않았다.")
    w("")

    for layer, 기관, 사업, 비목, 허용, 승인, 조건, 유형, 값, 단위, 증빙, 금지, 허용예, 근거, 도메인, v, 검수자, 검수일 in 대상:
        제목 = f"{layer} · {사업 or '(전 사업 공통)'} · {비목}"
        w(f"## {제목}")
        w("")
        w(f"- **허용**: `{허용}`  ·  **verified**: `{v}`  ·  **검수자**: {검수자}")
        if 유형:
            w(f"- **한도**: {유형} `{값}` {단위}")
        else:
            w("- **한도**: (없음)")
        w(f"- **사전승인**: `{승인}`")
        if 조건:
            w(f"  - 조건: {조건}")
        if 금지:
            w(f"- **금지예시 {len(금지)}건**:")
            for x in 금지:
                w(f"  - {x}")
        if 허용예:
            w(f"- **허용예시 {len(허용예)}건**:")
            for x in 허용예:
                w(f"  - {x}")
        w(f"- **증빙 {len(증빙)}건**: {', '.join(증빙)}")
        w("")
        w("### 근거 조문 원문")
        w("")
        for g in json.loads(근거):
            doc, 조 = g["doc_id"], g["조번호"]
            if (doc, 조) in db:
                제목2, 본문 = db[(doc, 조)]
                w(f"**{doc} · {조}({제목2})**")
                w("")
                w("```")
                w(본문.strip())
                w("```")
            elif 조 in 판독 and "재도전" in doc:
                a = 판독[조]
                w(f"**{doc} · {조}({a['조제목']})**  ⚠️ *아직 `doc_articles` 미적재 — "
                  f"`_재도전_2026_판독.json` 에서 인용. A 가 `_G_재도전2026_적재.py` 를 "
                  f"돌리면 실재가 된다*")
                w("")
                w("```")
                w(a["본문"].strip())
                w("```")
            else:
                w(f"**{doc} · {조}** — 🔴 **원문을 찾지 못했다. 이 행의 인용은 검증되지 않았다.**")
            w("")
        w("---")
        w("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"wrote {OUT}  ({len(대상)}행 · {len(out)}줄)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
