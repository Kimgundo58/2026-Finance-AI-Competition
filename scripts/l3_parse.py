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

━━ 2026-09-06(레인 W2) — VLM 분기 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
스캔 PDF·이미지 한글처럼 «형식은 맞는데 글자가 거의 없는» 파일은 지금까지 조 0건으로
바로 `fail` 이었다(다른 길이 없었다). ② 추출 단계(`extract()` 직후) 에 판독 시도를 끼운다.

  🔴 **`extract()` 가 예외를 던지는 경우(파일이 깨졌다·형식 위장 등)는 이 분기를 안 탄다.**
     "텍스트가 없다(스캔본)"과 "추출 자체가 죽었다"는 다른 사고다(요구사항 ④) — 후자는
     기존 `except` 경로 그대로 둔다. 이 분기는 **추출이 성공했는데 결과가 사실상 빈** 경우만이다
  🔴 임계값은 «새로 안 만든다» — `stage0_extract.빈_추출_글자수_임계치`(200자, 실측
     근거는 그 파일 §빈 추출 게이트 주석)를 그대로 가져다 쓴다. ai-47 이 다른 값을 쓰면
     **이 상수 하나로 맞춰야 한다** — 두 곳에 따로 적지 않는다
  🔴 `scripts/vlm_extract.py` 가 아직 없으면 **import 실패를 조용히 삼키지 않는다** —
     stderr 에 경고를 찍고 VLM 분기를 건너뛴다(기존 동작과 바이트 단위로 같다).
     생기면 코드 수정 없이 붙는다(있으면 부르고 없으면 건너뛰는 조건 하나뿐)
  🔴 판독에 성공해도 `파싱품질` 은 **'pass' 가 될 수 없다** — 항상 'warn'(판독은 틀릴
     수 있다). `extraction='vlm'` 을 여기서 기록한다 — `tenant.l3_documents.extraction`
     이 이 값을 가져야 (6-4_검증과_강등의) `VLM_DOWNGRADE` 강등이 걸린다. 안 적으면 강등이 안 돈다
  🔴 비용 가드 — 파일 **내용** 의 sha256 을 캐시 키로 쓴다(`doc_id` 는 매 업로드마다 새로
     발급돼 재업로드 탐지에 못 쓴다). `_vlm_캐시경로()` 아래 텍스트로 캐시하고, 있으면
     VLM 을 다시 안 부른다. 🔴 이건 «DB 스키마를 안 건드린다» 는 이 파일의 원칙 때문에
     고른 임시방편이다 — `tenant.l3_documents` 에 content_hash 컬럼이 생기면 그쪽으로
     옮기는 게 정석이다(제안만, 여기서 만들지 않는다)
  🔴 실제로 태워 본 결과(2026-09-06, DB 읽기전용) — «중앙이 말한 시험 재료가 실제로는
     그 시나리오가 아니었다»:
       경상국립대 안내문 2건(둘 다 파싱품질=warn) — 실측 글자수 27,123·549자, 둘 다
       200자 임계치 «위**다**» — 이 분기가 다루는 "텍스트 거의 0" 사례가 «아니다».
       그 문서들의 warn 원인은 Lane S 가 이미 짚은 것과 같다(조 구조가 없는 "단락"
       문서라 validate() 의 다른 flag 가 걸린 것) — VLM 로 고칠 사안이 아니다
       `_l3_업로드/` 에 있던 미등록 스캔 PDF 1건은 `extract()` 자체가
       `PdfReadError: startxref not found` 로 죽는다 — 이것도 «이 분기 대상이 아니다»
       (요구사항 ④의 "추출 자체가 죽었다" 쪽이다. 손상된 파일이지 스캔본 여부는 못 봤다)
     => 이 분기를 실제 "성공했지만 텍스트 0" 파일로 못 태웠다(그런 파일이 이 DB·이
        디렉터리에 없다). 아래 로직은 합성 입력(빈 문자열)으로 트리거 조건·임포트
        가드·캐시만 검증했다 — 실제 VLM 응답 경로는 `vlm_extract.py` 가 생겨야 검증된다
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

# ── VLM/Document AI 판독 — 레인 D(2026-09-06) «판독기 선택을 환경변수 한 개로».
#    `SUDDOE_L3_판독기 = vlm | docai` (기본 vlm). 계약이 같아(`extract(path) ->
#    (본문, 페이지오프셋)`) import 만 바꿔 끼운다 — 아래 호출 코드는 한 글자도 안 고친다.
#    둘 다 없거나 고른 쪽이 없으면 «조용히 스킵» 이 아니라 stderr 에 남기고 스킵한다.
_L3_판독기 = os.environ.get("SUDDOE_L3_판독기", "vlm")
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


def _파일해시(경로: Path) -> str:
    """VLM 재호출 방지 키. `doc_id` 는 매 업로드마다 새로 발급돼 같은 파일의
    재업로드를 못 알아본다 — «내용» 을 본다."""
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

    def _닫기(파싱품질: str, 조_건수: int = 0, dangling수: int = 0,
              extraction: str | None = None, **부가) -> dict:
        # 🔴 extraction 은 None(=바꾸지 않음)이 기본이다. VLM 분기를 안 탄 기존 경로는
        #    업로드 시점에 서버가 이미 적어 둔 값(파일 확장자 기준)을 그대로 둔다.
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
        # 🔴 여기서 죽는 건 "추출 자체가 죽었다"(파일 손상·형식 위장 등)다 — VLM 분기
        #    대상이 아니다(요구사항 ④). 스캔본이라 텍스트가 없는 것과는 다른 사고다.
        return _닫기("fail", 사유=f"추출 실패 {type(e).__name__}: {e}",
                    트레이스=traceback.format_exc()[-800:])

    vlm사용 = False
    if kind == "articles":
        arts, strategy, raw_text = payload, "xml_native", "\n".join(
            a["본문"] for a in payload)
    else:
        raw_text, page_offsets = payload
        # ── ②-1 🔴 VLM 분기 — 추출은 «성공했는데» 결과가 사실상 비었을 때만 ──────
        #    (레인 W2, 2026-09-06). 임계값은 stage0_extract 의 기존 상수를 그대로 쓴다.
        if len(raw_text) < 빈_추출_글자수_임계치:
            vlm결과 = _vlm_시도(경로)
            if vlm결과 is not None:
                판독본문, 판독오프셋, 출처 = vlm결과
                if len(판독본문) >= 빈_추출_글자수_임계치:
                    raw_text, page_offsets = 판독본문, 판독오프셋
                    vlm사용 = True
                # 판독도 짧으면 vlm사용=False 로 두고 아래 split_articles 가 그대로
                # 원래 raw_text(짧은 값)로 진행 -> 조 0건 -> ③에서 fail 로 닫힌다.
                # 사유에 "판독도 실패"임을 남기려면 여기서 짧게 이유를 적어 둔다.
            # vlm결과 가 None(모듈 없음·판독 자체 예외)이어도 여기서 죽지 않는다 —
            # 아래로 흘러 기존 split_articles 경로가 그대로 조 0건 -> fail 로 닫는다.
        try:
            arts, strategy = split_articles(raw_text, page_offsets)
        except Exception as e:
            return _닫기("fail", 사유=f"조문분해 실패 {type(e).__name__}: {e}",
                        트레이스=traceback.format_exc()[-800:])

    v = validate(arts, strategy)

    # ── ③ 🔴 조 0개는 성공이 아니다 — 게이트 두 겹 중 파싱 단 ───────────
    if not v["ok"]:
        # 🔴 요구사항 ④ — "텍스트가 없다(스캔본) + 판독도 실패" 와 "추출 자체가
        #    죽었다"(위 except 경로)는 다른 사고다. 여기 오는 건 전자 갈래이고,
        #    그 안에서도 VLM 을 시도했는지 여부를 사유에 갈라 남긴다.
        if kind != "articles" and len(raw_text) < 빈_추출_글자수_임계치:
            사유 = ("텍스트가 없다(스캔본으로 보임) — VLM 판독도 실패"
                   if _vlm추출 is not None else
                   "텍스트가 없다(스캔본으로 보임) — VLM 모듈 없음(scripts/vlm_extract.py 미구현)")
        else:
            사유 = f"조 0건 ({strategy})"
        return _닫기("fail", 사유=사유, strategy=strategy)

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
    if vlm사용:
        # 🔴 요구사항 ② — 판독은 틀릴 수 있다. 아무리 flags 가 깨끗해도 'pass' 로
        #    올리지 않는다. extraction='vlm' 을 여기서 «처음» 기록한다 —
        #    이게 없으면 VLM_DOWNGRADE 강등([[6_LLM/6-4_검증과_강등]])이 안 걸린다.
        파싱품질 = "warn"
        flags = list(flags) + ["VLM 판독분 — 사람 확인 권장"]
        return _닫기(파싱품질, 조_건수=len(arts), dangling수=dang,
                    strategy=strategy, flags=flags, extraction="vlm")
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
