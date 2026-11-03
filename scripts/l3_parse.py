# -*- coding: utf-8 -*-
"""L3 업로드 파싱: 저장된 원본에서 조문을 뽑아 tenant.l3_articles 에 넣는다.

server/routes_l3.백그라운드_파싱 이 업로드 응답 뒤 백그라운드로 부른다. 공개 함수는
파싱(cur, doc_id) -> {ok, 파싱품질, 조_건수, 미해결참조수, strategy, flags, 사유} 와 원본경로/L3_저장소.
tenant.l3_documents 의 파싱품질·미해결참조수·실패사유를 갱신한다. 호출자는 GUC app.org_id 를 설정한
트랜잭션 안에서 불러야 한다(RLS). 항상 지키는 조건: 조 0건은 실패이고, 파싱품질은 'pending' 에서 항상
pass/warn/fail 로 바뀐다. 텍스트 레이어가 없는 스캔본은 텍스트 인식(OCR)을 하지 않고 실패로 기록한다.
기관당 판정에 쓰는 L3 는 한 문서다. 파싱이 성공하면 같은 기관의 이전 문서를 superseded 로 바꾸고,
실패하면 이전 문서를 그대로 둔다(`적용문서_정리`).

실행: PYTHONIOENCODING=utf-8 python scripts/l3_parse.py --doc-id <uuid> | --all-pending [--owner-dsn <DSN>]
"""
from __future__ import annotations

import argparse
import os
import traceback
import uuid
from pathlib import Path

import l3_load
from _lib import db
from article_split import split_articles, validate
from text_extract import extract, 빈_추출_글자수_임계치

ROOT = Path(__file__).resolve().parent.parent

# 업로드 원본 저장 경로 규칙. server/routes_l3 가 원본경로() 로 저장하고 파싱() 이 같은 함수로 읽는다.
# 두 쪽이 다른 경로를 보면 '원본 파일 없음' 으로 실패하므로 규칙은 여기 한 곳에만 둔다.
_허용_확장자 = {"pdf", "hwpx", "hwp"}
L3_저장소 = Path(os.environ.get("SUDDOE_L3_DIR", str(ROOT / "_l3_업로드")))

_스캔본_사유 = "텍스트가 없다(스캔본으로 보임) — 스캔본 텍스트 인식은 지원하지 않는다"
# 'pending' 으로 이만큼 넘게 남은 문서는 파싱이 끊긴 것으로 본다. server/routes_l3.py `_중단_판정_분` 과 같은 값
중단_판정_분 = 10


def 원본경로(doc_id: str, 확장: str) -> Path:
    """L3_저장소/<doc_id>.<확장>. doc_id 는 UUID 표준 표기로 바꿔 쓴다(UUID 가 아니면 ValueError).

    파일 이름이 doc_id 에서 나오므로, UUID 가 아닌 값이 들어와 저장소 밖 경로가 되는 일을 막는다.
    """
    안전확장 = 확장 if 확장 in _허용_확장자 else "bin"
    return L3_저장소 / f"{uuid.UUID(str(doc_id))}.{안전확장}"


def _확장자(원본파일명: str) -> str:
    return 원본파일명.rsplit(".", 1)[-1].lower() if "." in (원본파일명 or "") else ""


def _본문_추출(경로: Path) -> tuple[str, dict[int, int]] | dict:
    """(본문, 페이지오프셋). 실패하면 결과에 덧붙일 {사유, 실패사유, 트레이스?} dict 를 돌려준다.

    형식은 확장자가 아니라 파일 내용으로 판정한다(text_extract.extract).
    `실패사유` 는 화면 안내용 분류 코드다(server/routes_l3.py `_실패_안내`).
    """
    if not 경로.exists():
        return {"사유": f"원본 파일 없음: {경로}", "실패사유": "원본없음"}
    try:
        kind, payload = extract(경로)
    except Exception as e:
        # 파일 손상·형식 위장처럼 추출 자체가 실패한 경우다. 스캔본(텍스트 없음)과는 다르다.
        보호 = type(e).__name__ == "HwpProtectedError"      # 배포용·암호화 HWP
        return {"사유": f"추출 실패 {type(e).__name__}: {e}",
                "실패사유": "보호문서" if 보호 else "추출실패",
                "트레이스": traceback.format_exc()[-800:]}
    if kind != "text":
        # 업로드는 pdf/hwpx/hwp 만 받으므로 조문 구조(법령 XML) 경로는 오지 않는다
        return {"사유": f"지원하지 않는 형식({kind})", "실패사유": "형식"}
    return payload


def _조문_저장(cur, doc_id: str, org_id, arts: list[dict]) -> None:
    """doc_id 의 기존 조문을 지우고 다시 넣는다. 실패하면 예외를 올린다.

    세이브포인트 안에서 실행한다. 실패하면 이 블록만 롤백되므로 부분 INSERT 가 남지 않고,
    호출자가 설정한 트랜잭션 범위 GUC app.org_id 는 유지되어 이어지는 fail UPDATE 가
    RLS 정책을 통과한다(커넥션 전체 rollback 은 GUC 까지 지운다).
    """
    # UNIQUE(doc_id, 조번호) 가 있고 chapter·paragraph 전략은 중복 조번호를 낼 수 있어 ON CONFLICT 로 거른다
    with cur.connection.transaction():
        cur.execute("DELETE FROM tenant.l3_articles WHERE doc_id=%s", (doc_id,))
        for a in arts:
            cur.execute(
                "INSERT INTO tenant.l3_articles "
                " (doc_id, org_id, \"조번호\", \"조제목\", \"조순번\", \"장\", \"본문\", \"페이지\") "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (doc_id, \"조번호\") DO NOTHING",
                (doc_id, org_id, a["조번호"], a.get("조제목"), a.get("조순번"),
                 a.get("장"), a["본문"], a.get("페이지")))


def _미해결참조_수(cur, arts: list[dict]) -> int:
    """해결되지 않은 상위 참조 수. l3_load 와 같은 기준으로 사업비 관련 장만 센다."""
    return sum(1
               for a in arts if l3_load.사업비관련장(a.get("장"))
               for r in l3_load.상위참조(cur, a["본문"]) if not r["해결"])


def _상태_기록(cur, doc_id: str, 파싱품질: str, 미해결참조수: int,
             실패사유: str | None = None) -> None:
    cur.execute(
        "UPDATE tenant.l3_documents SET \"파싱품질\"=%s, \"미해결참조수\"=%s, \"실패사유\"=%s "
        " WHERE doc_id=%s",
        (파싱품질, 미해결참조수, 실패사유, doc_id))


def _기관_정리_잠금(cur, org_id) -> None:
    """같은 기관의 결과 기록·적용 문서 정리를 한 번에 하나씩 하게 하는 트랜잭션 범위 advisory lock.

    두 파싱이 동시에 끝나도 뒤에 잠금을 얻은 쪽이 앞 트랜잭션의 커밋 결과를 보고 판단한다.
    교착을 피하려면 이 트랜잭션이 l3_documents 행을 처음 UPDATE 하기 전에(`_상태_기록` 전에) 잡아야 한다.
    같은 트랜잭션에서 다시 불러도 된다(advisory lock 은 중첩된다).
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('l3_적용문서'), hashtext(%s))", (str(org_id),))


def 적용문서_정리(cur, doc_id: str, org_id, 성공: bool) -> None:
    """기관당 판정에 쓰는 L3 를 한 문서로 맞춘다. 파싱 결과를 기록한 같은 트랜잭션에서 부른다.

    규칙은 '나중에 올린(created_at) 문서가 이긴다' 이다.
    성공(pass/warn): 이 문서보다 나중에 올린 문서가 이미 성공해 있으면(파싱 완료 순서가 뒤바뀐 경우) 이 문서를
      superseded 로 바꾼다. 아니면 이 문서를 active 로 두고 — 파싱이 `중단_판정_분` 보다 오래 걸려 다른 업로드의
      실패 정리에서 superseded 로 바뀌었어도 되돌린다 — 이 문서보다 먼저 올린 active 문서(이전 규정, 이전 실패·대기)를
      superseded 로 바꾼다. 되돌리지 않으면 이 문서가 이전 규정만 내리고 자신은 빠져 적용 문서가 0건이 된다.
    실패: 판정에 쓰는 이전 규정은 그대로 둔다. 이 문서보다 나중에 올린 문서가 이미 결과(성공·실패)를 냈으면 이 문서를
      superseded 로 바꾼다. 아니면 이 문서보다 먼저 실패한 문서와 파싱이 끊겨 `중단_판정_분` 넘게 'pending' 으로
      남은 문서만 superseded 로 바꿔 최근 실패 1건만 남긴다(아직 파싱 중인 이전 업로드는 건드리지 않는다).
    판정(l3_load._조문들)은 status='active' 이고 pass/warn 인 문서만 읽으므로 이 함수가 곧 '어느 규정으로
    판정하는가' 를 정한다.
    """
    _기관_정리_잠금(cur, org_id)
    이후_결과 = ("pass", "warn") if 성공 else ("pass", "warn", "fail")
    이후_있음 = cur.execute(
        "SELECT 1 FROM tenant.l3_documents n WHERE n.org_id=%s AND n.doc_id<>%s "
        "   AND n.status='active' AND n.\"파싱품질\" = ANY(%s) "
        "   AND n.created_at > (SELECT created_at FROM tenant.l3_documents WHERE doc_id=%s) LIMIT 1",
        (org_id, doc_id, list(이후_결과), doc_id)).fetchone()
    if 이후_있음:
        cur.execute("UPDATE tenant.l3_documents SET status='superseded' "
                    "WHERE doc_id=%s AND status<>'superseded'", (doc_id,))
        return
    조건 = ("org_id=%s AND doc_id<>%s AND status='active' "
          "AND created_at <= (SELECT created_at FROM tenant.l3_documents WHERE doc_id=%s)")
    if not 성공:
        cur.execute("UPDATE tenant.l3_documents SET status='superseded' "
                    f"WHERE {조건} AND (\"파싱품질\"='fail' OR (\"파싱품질\"='pending' "
                    f"AND created_at < now() - interval '{중단_판정_분} minutes'))",
                    (org_id, doc_id, doc_id))
        return
    cur.execute("UPDATE tenant.l3_documents SET status='active' "
                "WHERE doc_id=%s AND status<>'active'", (doc_id,))
    cur.execute(f"UPDATE tenant.l3_documents SET status='superseded' WHERE {조건}",
                (org_id, doc_id, doc_id))


def 실패_기록(cur, doc_id: str, org_id, 실패사유: str) -> None:
    """파싱 실패를 기록하고 적용 문서를 정리한다. 파서 밖 예외 경로(server/routes_l3)도 이 함수를 쓴다."""
    _기관_정리_잠금(cur, org_id)
    _상태_기록(cur, doc_id, "fail", 0, 실패사유)
    적용문서_정리(cur, doc_id, org_id, 성공=False)


def 파싱(cur, doc_id: str) -> dict:
    """원본을 파싱해 l3_articles 와 l3_documents 상태를 갱신하고 결과 dict 를 돌려준다.

    실패도 예외 없이 {ok: False, 파싱품질: 'fail', 사유} 로 돌려준다. doc_id 행이 없을 때만
    상태를 쓰지 않고 {ok: False, 사유} 를 돌려준다. 재파싱에 대비해 기존 조문을 지우고 다시 넣는다.
    """
    row = cur.execute(
        "SELECT org_id, \"원본파일명\" FROM tenant.l3_documents WHERE doc_id=%s",
        (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "사유": f"l3_documents 에 doc_id={doc_id} 없음"}
    org_id, 원본파일명 = row

    def _결과_기록(파싱품질: str, 조_건수: int = 0, 미해결참조수: int = 0, **부가) -> dict:
        성공 = 파싱품질 in ("pass", "warn")
        _기관_정리_잠금(cur, org_id)          # `_상태_기록` 의 행 잠금보다 먼저 잡는다(교착 방지)
        _상태_기록(cur, doc_id, 파싱품질, 미해결참조수, None if 성공 else 부가.get("실패사유"))
        적용문서_정리(cur, doc_id, org_id, 성공)
        return {"ok": 성공, "파싱품질": 파싱품질,
                "조_건수": 조_건수, "미해결참조수": 미해결참조수, **부가}

    추출 = _본문_추출(원본경로(doc_id, _확장자(원본파일명)))
    if isinstance(추출, dict):
        return _결과_기록("fail", **추출)
    raw_text, page_offsets = 추출

    try:
        arts, strategy = split_articles(raw_text, page_offsets)
    except Exception as e:
        return _결과_기록("fail", 사유=f"조문 분할 실패 {type(e).__name__}: {e}",
                      실패사유="분할실패", 트레이스=traceback.format_exc()[-800:])

    v = validate(arts, strategy)
    if not v["ok"]:                 # 조 0건은 성공이 아니다
        스캔본 = len(raw_text) < 빈_추출_글자수_임계치
        return _결과_기록("fail", 사유=_스캔본_사유 if 스캔본 else f"조 0건 ({strategy})",
                      실패사유="스캔본" if 스캔본 else "조문없음", strategy=strategy)

    try:
        _조문_저장(cur, doc_id, org_id, arts)
    except Exception as e:
        return _결과_기록("fail", 사유=f"l3_articles 적재 실패 {type(e).__name__}: {e}",
                      실패사유="저장실패", 트레이스=traceback.format_exc()[-800:])

    # 미해결 참조는 업로드(파싱) 시점에 세어 l3_documents.미해결참조수 에 둔다
    flags = list(v["flags"])
    return _결과_기록("warn" if flags else "pass", 조_건수=len(arts),
                  미해결참조수=_미해결참조_수(cur, arts), strategy=strategy, flags=flags)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="L3 문서를 다시 파싱한다. 문서 목록과 org_id 는 --owner-dsn(RLS 를 "
                    "우회하는 테이블 소유자 롤)으로 읽고, 파싱은 SUDDOE_DSN 커넥션에서 "
                    "문서마다 GUC app.org_id 를 설정한 트랜잭션으로 실행한다.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doc-id")
    g.add_argument("--all-pending", action="store_true",
                   help="파싱품질='pending' 인 행 전부")
    ap.add_argument("--owner-dsn", default=db.DSN,
                    help="문서 목록·org_id 조회용 DSN. 테이블 소유자 롤이어야 한다 "
                         "(기본: SUDDOE_DSN)")
    a = ap.parse_args()

    with db.connect(a.owner_dsn) as owner:
        if a.doc_id:
            대상 = owner.execute(
                "SELECT doc_id, org_id FROM tenant.l3_documents WHERE doc_id=%s",
                (a.doc_id,)).fetchall()
        else:
            # superseded 된 pending(이후 업로드로 이미 교체된 문서)은 다시 파싱하지 않는다
            대상 = owner.execute(
                "SELECT doc_id, org_id FROM tenant.l3_documents "
                " WHERE \"파싱품질\"='pending' AND status='active'"
            ).fetchall()
            print(f"파싱 대기(pending) {len(대상)}건")
    if a.doc_id and not 대상:
        print(f"  {a.doc_id}  ->  l3_documents 에 없음")

    with db.connect() as conn:
        cur = conn.cursor()
        for doc_id, org_id in 대상:
            # RLS 정책이 읽는 GUC 를 문서마다 같은 트랜잭션 안에서 설정한다
            with conn.transaction():
                cur.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
                r = 파싱(cur, str(doc_id))
            print(f"  {doc_id}  ->  {r}")


if __name__ == "__main__":
    main()
