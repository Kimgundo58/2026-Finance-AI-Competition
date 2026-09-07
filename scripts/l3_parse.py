# -*- coding: utf-8 -*-
"""L3 업로드 파싱 — 저장된 원본을 읽어 `tenant.l3_articles` 를 채운다.

    l3_parse.파싱(cur, doc_id) -> {조_건수, dangling수, 파싱품질, strategy, flags}

텍스트 추출·조문 분해·품질 판정은 기존 `stage0_extract`·`stage0_articles` 를
그대로 부른다. 스캔본처럼 추출은 됐지만 글자가 거의 없는 파일은 VLM/Document AI
판독을 시도하고, 판독 후에도 짧으면 조 0건으로 실패 처리한다.

지키는 것
  · 조 0개는 성공이 아니다 — `파싱품질='fail'` 로 닫는다
  · `파싱품질` 은 반드시 '대기' 를 벗어난다 — pass/warn/fail 중 하나로 갱신한다
  · dangling 은 업로드 시점(=파싱 시점)에 `l3_documents.dangling수` 에 채운다
  · 조번호 구판 재매칭은 `l3_load._shifted_재매칭()` 이 한다 — 여기선 그냥 부른다

실행:
    PYTHONIOENCODING=utf-8 python scripts/l3_parse.py --doc-id <uuid>
    PYTHONIOENCODING=utf-8 python scripts/l3_parse.py --all-pending
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402

import l3_load  # noqa: E402
from stage0_extract import extract, 빈_추출_글자수_임계치  # noqa: E402
from stage0_articles import split_articles, validate  # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# 판독기 선택 — `SUDDOE_L3_판독기 = vlm | docai` (기본 vlm). 계약이 같아
# (`extract(path) -> (본문, 페이지오프셋)`) import 만 바꿔 끼운다. 모듈이 없으면
# 조용히 넘어가지 않고 stderr 에 남기고 스킵한다.
_L3_판독기 = (os.environ.get("SUDDOE_L3_READER") or os.environ.get("SUDDOE_L3_판독기", "vlm"))
try:
    if _L3_판독기 == "docai":
        from docai_extract import extract as _vlm추출  # noqa: E402
    else:
        from vlm_extract import extract as _vlm추출  # noqa: E402
    #  계약: _vlm추출(path: Path) -> tuple[str, dict[int,int]]
    #  — extract_pdf()·extract_hwp() 와 같은 모양(본문, 페이지오프셋)으로 맞췄다.
except ImportError as _e:
    _vlm추출 = None
    print(f"⚠️ scripts/{_L3_판독기}_extract.py 를 못 찾았다({_e}) — 판독 분기는 항상 스킵된다. "
          "생기면 코드 수정 없이 붙는다.", file=sys.stderr)

_VLM_캐시_디렉터리 = Path(os.environ.get("SUDDOE_VLM_CACHE_DIR",
                                     str(ROOT / "_l3_업로드" / "_vlm_캐시")))

# `server/routes_l3.py::원본경로()` 와 반드시 같은 규칙이어야 한다 — 저장한 쪽과
# 읽는 쪽이 다른 파일을 보면 "파일이 없다" 실패가 조용히 난다. 서버 코드를 임포트하지
# 않고 규칙만 복제한다.
_허용_확장자 = {"pdf", "hwpx", "hwp"}
L3_저장소 = Path(os.environ.get("SUDDOE_L3_DIR", str(ROOT / "_l3_업로드")))


def 원본경로(doc_id: str, 확장: str) -> Path:
    안전확장 = 확장 if 확장 in _허용_확장자 else "bin"
    return L3_저장소 / f"{doc_id}.{안전확장}"


def _확장자(원본파일명: str) -> str:
    return 원본파일명.rsplit(".", 1)[-1].lower() if "." in (원본파일명 or "") else ""


def _파일해시(경로: Path) -> str:
    """VLM 재호출 방지 키. doc_id 는 업로드마다 새로 발급돼 재업로드 탐지에 못 쓴다 —
    파일 내용의 해시를 쓴다."""
    return hashlib.sha256(경로.read_bytes()).hexdigest()


def _vlm_캐시_경로(파일해시: str) -> Path:
    return _VLM_캐시_디렉터리 / f"{파일해시}.txt"


def _vlm_시도(경로: Path) -> tuple[str, dict[int, int], str] | None:
    """VLM 판독. 성공하면 (본문, 페이지오프셋, 출처) — 출처는 'cache'|'live'.
    실패(모듈 없음·판독 자체 실패)하면 None — 호출부가 기존 fail 경로로 닫는다.
    """
    if _vlm추출 is None:
        return None
    해시 = _파일해시(경로)
    캐시경로 = _vlm_캐시_경로(해시)
    if 캐시경로.exists():
        return 캐시경로.read_text(encoding="utf-8"), {}, "cache"
    try:
        본문, 오프셋 = _vlm추출(경로)
    except Exception as e:
        print(f"⚠️ VLM 판독 실패 {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if 본문:
        try:
            _VLM_캐시_디렉터리.mkdir(parents=True, exist_ok=True)
            캐시경로.write_text(본문, encoding="utf-8")
        except OSError as e:
            print(f"⚠️ VLM 캐시 저장 실패(계속 진행) — {e}", file=sys.stderr)
    return 본문, 오프셋, "live"


def 파싱(cur, doc_id: str) -> dict:
    """실제 파싱 + DB 반영. 성공이든 실패든 `파싱품질` 이 '대기' 를 벗어난 채로 끝난다.

    재파싱을 대비해 기존 `l3_articles` 를 doc_id 범위로 지우고 다시 넣는다.
    """
    row = cur.execute(
        "SELECT org_id, \"원본파일명\" FROM tenant.l3_documents WHERE doc_id=%s",
        (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "사유": f"l3_documents 에 doc_id={doc_id} 없음"}
    org_id, 원본파일명 = row
    확장 = _확장자(원본파일명)
    경로 = 원본경로(doc_id, 확장)

    def _닫기(파싱품질: str, 조_건수: int = 0, dangling수: int = 0,
              extraction: str | None = None, **부가) -> dict:
        # extraction 은 None(=바꾸지 않음)이 기본이다. VLM 분기를 안 탄 경로는 업로드
        # 시점에 서버가 적어 둔 값을 그대로 둔다.
        if extraction is not None:
            cur.execute(
                "UPDATE tenant.l3_documents SET \"파싱품질\"=%s, \"dangling수\"=%s, "
                " extraction=%s WHERE doc_id=%s",
                (파싱품질, dangling수, extraction, doc_id))
        else:
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
        # 여기서 죽는 건 "추출 자체가 죽었다"(파일 손상·형식 위장 등)다 — VLM 분기
        # 대상이 아니다. 스캔본이라 텍스트가 없는 것과는 다른 사고다.
        return _닫기("fail", 사유=f"추출 실패 {type(e).__name__}: {e}",
                    트레이스=traceback.format_exc()[-800:])

    vlm사용 = False
    if kind == "articles":
        arts, strategy, raw_text = payload, "xml_native", "\n".join(
            a["본문"] for a in payload)
    else:
        raw_text, page_offsets = payload
        # VLM 분기 — 추출은 성공했는데 결과가 사실상 비었을 때만 판독을 시도한다.
        # 임계값은 stage0_extract 의 기존 상수를 그대로 쓴다.
        if len(raw_text) < 빈_추출_글자수_임계치:
            vlm결과 = _vlm_시도(경로)
            if vlm결과 is not None:
                판독본문, 판독오프셋, 출처 = vlm결과
                if len(판독본문) >= 빈_추출_글자수_임계치:
                    raw_text, page_offsets = 판독본문, 판독오프셋
                    vlm사용 = True
                # 판독도 짧으면 아래 split_articles 가 원래 raw_text 로 진행해
                # 조 0건 -> fail 로 닫힌다.
            # vlm결과 가 None(모듈 없음·판독 실패)이어도 여기서 죽지 않고 같은 경로로 닫힌다.
        try:
            arts, strategy = split_articles(raw_text, page_offsets)
        except Exception as e:
            return _닫기("fail", 사유=f"조문분해 실패 {type(e).__name__}: {e}",
                        트레이스=traceback.format_exc()[-800:])

    v = validate(arts, strategy)

    # 조 0개는 성공이 아니다 — 게이트 두 겹 중 파싱 단.
    if not v["ok"]:
        # "텍스트가 없다(스캔본) + 판독도 실패" 와 "추출 자체가 죽었다"(위 except
        # 경로)는 다른 사고다. 여기는 전자 갈래이고, VLM 시도 여부를 사유에 남긴다.
        if kind != "articles" and len(raw_text) < 빈_추출_글자수_임계치:
            사유 = ("텍스트가 없다(스캔본으로 보임) — VLM 판독도 실패"
                   if _vlm추출 is not None else
                   "텍스트가 없다(스캔본으로 보임) — VLM 모듈 없음(scripts/vlm_extract.py 미구현)")
        else:
            사유 = f"조 0건 ({strategy})"
        return _닫기("fail", 사유=사유, strategy=strategy)

    # tenant.l3_articles 에 넣는다. UNIQUE(doc_id, 조번호) 가 걸려 있고
    # jang·paragraph 전략은 중복을 못 막을 수 있어, ON CONFLICT 와 try 양쪽으로 막는다.
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
    if vlm사용:
        # 판독은 틀릴 수 있다 — flags 가 깨끗해도 'pass' 로 올리지 않는다.
        # extraction='vlm' 을 여기서 기록해야 VLM_DOWNGRADE 강등이 걸린다.
        파싱품질 = "warn"
        flags = list(flags) + ["VLM 판독분 — 사람 확인 권장"]
        return _닫기(파싱품질, 조_건수=len(arts), dangling수=dang,
                    strategy=strategy, flags=flags, extraction="vlm")
    return _닫기(파싱품질, 조_건수=len(arts), dangling수=dang,
                strategy=strategy, flags=flags)


def _품질판정(arts: list[dict], validate_flags: list[str]) -> tuple[str, list[str]]:
    """flags 있으면 warn, 없으면 pass. 조 0건은 호출부에서 이미 fail 로 닫혔다(안 들어옴)."""
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
