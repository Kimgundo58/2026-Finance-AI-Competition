# -*- coding: utf-8 -*-
"""L3 업로드 파싱 — `server/routes_l3.py` 가 저장한 원본을 읽어 `tenant.l3_articles` 를 채운다.

`RAG.md` §4-1 · `Agent.md` §3-2 · CLAUDE.md 확정 원칙(끊긴 참조는 업로드 시점에 ·
조번호는 구판일 수 있다 — 조 제목으로 재매칭). 서버 배선은 BE 소관, 이 파일은
`scripts/` 쪽 파싱 함수만 — DB 스키마·`server/` 는 안 건드린다.

    l3_parse.파싱(cur, doc_id) -> {조_건수, dangling수, 파싱품질, strategy, flags}

새로 만들지 않은 것 (기존 걸 그대로 불렀다)
  · 텍스트 추출        stage0_extract.extract()          — 확장자 아니라 내용물로 가른다
  · 조문 분해          stage0_articles.split_articles()  — 5단 fallback
  · 품질 판정 재료     stage0_articles.validate()
  · 상위참조·dangling  l3_load.상위참조()                 — 오늘 shifted 재매칭을 붙였다

🔴 반드시 지킨 것 (이 프로젝트가 오늘 세 번 물린 자리)
  ① 조 0개 = 성공이 아니다. `파싱품질='fail'` 로 닫는다(추출기 쪽 게이트와 별개로 이중 방어)
  ② `파싱품질` 은 반드시 '대기' 를 벗어난다 — pass/warn/fail 중 하나로 반드시 갱신한다
  ③ dangling 은 업로드 시점(=파싱 시점)에 `l3_documents.dangling수` 에 채운다
  ④ 조번호 구판 재매칭은 `l3_load._shifted_재매칭()`(오늘 추가) 이 이미 한다 — 여기선 그냥 부른다

실행:
    PYTHONIOENCODING=utf-8 python scripts/l3_parse.py --doc-id <uuid>
    PYTHONIOENCODING=utf-8 python scripts/l3_parse.py --all-pending
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402

import l3_load  # noqa: E402
from stage0_extract import extract  # noqa: E402
from stage0_articles import split_articles, validate  # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# 🔴 `server/routes_l3.py::원본경로()` 와 **반드시 같은 규칙**이어야 한다 — 저장한 쪽과
#    읽는 쪽이 다른 파일을 보면 "파일이 없다" 실패가 매번 조용히 난다. 서버 코드를
#    임포트하지 않고 규칙만 복제한다(레인 분리 — server/ 는 안 건드리고 안 물고 간다).
_허용_확장자 = {"pdf", "hwpx", "hwp"}
L3_저장소 = Path(os.environ.get("SUDDOE_L3_DIR", str(ROOT / "_l3_업로드")))


def 원본경로(doc_id: str, 확장: str) -> Path:
    안전확장 = 확장 if 확장 in _허용_확장자 else "bin"
    return L3_저장소 / f"{doc_id}.{안전확장}"


def _확장자(원본파일명: str) -> str:
    return 원본파일명.rsplit(".", 1)[-1].lower() if "." in (원본파일명 or "") else ""


def 파싱(cur, doc_id: str) -> dict:
    """실제 파싱 + DB 반영. 성공이든 실패든 `파싱품질` 이 '대기' 를 벗어난 채로 끝난다.

    🔴 재파싱(재업로드 없이 다시 돌리는 경우)을 대비해 기존 `l3_articles` 를 doc_id
       범위로 지우고 다시 넣는다 — `seed_l3_fixture.py::적재()` 와 같은 관용구다.
    """
    row = cur.execute(
        "SELECT org_id, \"원본파일명\" FROM tenant.l3_documents WHERE doc_id=%s",
        (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "사유": f"l3_documents 에 doc_id={doc_id} 없음"}
    org_id, 원본파일명 = row
    확장 = _확장자(원본파일명)
    경로 = 원본경로(doc_id, 확장)

    def _닫기(파싱품질: str, 조_건수: int = 0, dangling수: int = 0, **부가) -> dict:
        cur.execute(
            "UPDATE tenant.l3_documents SET \"파싱품질\"=%s, \"dangling수\"=%s "
            " WHERE doc_id=%s",
            (파싱품질, dangling수, doc_id))
        return {"ok": 파싱품질 in ("pass", "warn"), "파싱품질": 파싱품질,
                "조_건수": 조_건수, "dangling수": dangling수, **부가}

    # ── ① 파일이 있어야 시작한다 ────────────────────────────────────────
    if not 경로.exists():
        return _닫기("fail", 사유=f"원본 파일 없음: {경로}")

    # ── ② 추출 — 확장자 아니라 내용물로 가른다(stage0_extract 가 이미 그렇게 짜여 있다) ──
    try:
        kind, payload = extract(경로)
    except Exception as e:
        return _닫기("fail", 사유=f"추출 실패 {type(e).__name__}: {e}",
                    트레이스=traceback.format_exc()[-800:])

    if kind == "articles":
        arts, strategy, raw_text = payload, "xml_native", "\n".join(
            a["본문"] for a in payload)
    else:
        raw_text, page_offsets = payload
        try:
            arts, strategy = split_articles(raw_text, page_offsets)
        except Exception as e:
            return _닫기("fail", 사유=f"조문분해 실패 {type(e).__name__}: {e}",
                        트레이스=traceback.format_exc()[-800:])

    v = validate(arts, strategy)

    # ── ③ 🔴 조 0개는 성공이 아니다 — 게이트 두 겹 중 파싱 단 ───────────
    if not v["ok"]:
        return _닫기("fail", 사유=f"조 0건 ({strategy})", strategy=strategy)

    # ── ④ tenant.l3_articles 에 넣는다 ──────────────────────────────────
    # 🔴 UNIQUE(doc_id, 조번호) 가 걸려 있다. `stage0_articles._build()` 는 titled/bare
    #    조 전략에서만 중복을 "[2]" 로 갈라 막는다 — jang·paragraph 전략은 이론상 안 막인다.
    #    막히면 이 함수가 예외로 죽어 「대기」에 영원히 남는 사고가 되므로 ON CONFLICT 로도
    #    막고(1차), 아래를 통째로 try 로도 감싼다(2차) — `stage0_ingest.py` 와 같은 관용구.
    cur.execute("DELETE FROM tenant.l3_articles WHERE doc_id=%s", (doc_id,))
    try:
        for a in arts:
            cur.execute(
                "INSERT INTO tenant.l3_articles "
                " (doc_id, org_id, \"조번호\", \"조제목\", \"조번호_int\", \"장\", \"본문\", \"페이지\") "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (doc_id, \"조번호\") DO NOTHING",
                (doc_id, org_id, a["조번호"], a.get("조제목"), a.get("조번호_int"),
                 a.get("장"), a["본문"], a.get("페이지")))
    except Exception as e:
        cur.connection.rollback()      # 부분 INSERT 가 'fail' 과 함께 커밋되면 안 된다
        return _닫기("fail", 사유=f"l3_articles 적재 실패 {type(e).__name__}: {e}",
                    트레이스=traceback.format_exc()[-800:])

    # ── dangling — 업로드(파싱) 시점에 센다. 사업비 관련 장만(l3_load 와 같은 기준) ──
    dang = 0
    for a in arts:
        if not l3_load.사업비관련장(a.get("장")):
            continue
        dang += sum(1 for r in l3_load.상위참조(cur, a["본문"]) if not r["해소"])

    파싱품질, flags = _품질판정(arts, v["flags"])
    return _닫기(파싱품질, 조_건수=len(arts), dangling수=dang,
                strategy=strategy, flags=flags)


def _품질판정(arts: list[dict], validate_flags: list[str]) -> tuple[str, list[str]]:
    """flags 있으면 warn, 없으면 pass. 조 0건은 호출부에서 이미 fail 로 닫혔다(안 들어옴).

    🔴 확인해본 결과 — ai-ae 가 제안한 "조 1건 + 본문 수천 자" 별도 게이트는
    **필요 없었다.** `stage0_articles.validate()` 의 V2(`조_개수_부족`, 5건 미만이면
    무조건 발동)가 조 1건인 모든 경우를 이미 잡는다 — 건국대 붙임1 이 고치기 전 조
    1건짜리 paragraph 로 떨어졌을 때도 V2 가 이미 걸려 있었다(실측: `flags=['V2:조_개수_부족(1)',
    '구조없음:단락분할']`). 별도 코드를 넣었다가 "V2 가 이미 채워놔서 내 조건이 항상
    거짓이 되는" 죽은 분기였다 — 넣어보고 확인한 뒤 도로 뺐다.
    """
    return ("warn" if validate_flags else "pass"), list(validate_flags)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doc-id")
    g.add_argument("--all-pending", action="store_true",
                   help="파싱품질='대기' 인 행 전부")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        if a.doc_id:
            대상 = [a.doc_id]
        else:
            대상 = [r[0] for r in cur.execute(
                "SELECT doc_id FROM tenant.l3_documents WHERE \"파싱품질\"='대기'"
            ).fetchall()]
            print(f"대기 {len(대상)}건")

        for doc_id in 대상:
            r = 파싱(cur, str(doc_id))
            conn.commit()
            print(f"  {doc_id}  ->  {r}")


if __name__ == "__main__":
    main()
