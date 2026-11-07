# -*- coding: utf-8 -*-
"""개정 처리 점검(P3-4, docs/5_룰/5-8_저장과_조회.md "개정판"). 문서를 실제로 활성화하므로 던져 버릴 DB 에서만 돈다.

실제 판정이 쓰는 orchestrate.온톨로지_조회 와 corpus_ingest.활성화 로 전후를 본다(판정 LLM 없음):
모두의 창업 멘토링비 1인 1일 40만원 코드 불가 → 세부관리기준 새 판 활성화 → 그 판을 근거로 가진 규범 · 노드만 재검토 →
같은 입력이 코드 불가를 내지 않고 '가능' 을 조건부로 낮춘다 · 옛 판 조문은 문맥에서 빠진다 → 문서키만 바꿔 다시 적재해도 재검토 유지 →
원천 행을 고치면 풀려 코드 불가로 돌아온다.

전제: db/init 적용 + ontology_ci_seed.py + ontology_load.py 적재를 끝낸 DB. 접속: SUDDOE_INGEST_DSN(활성화 · 적재), SUDDOE_DSN(조회).
    python scripts/ontology_revision_check.py --버리는-DB
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db, paths  # noqa: E402

ROOT = paths.ensure_on_path()

import corpus_ingest  # noqa: E402
import ontology_gate  # noqa: E402
import ontology_load  # noqa: E402
import ontology_terms  # noqa: E402
import orchestrate  # noqa: E402

사업 = "모두의 창업 프로젝트"
문서키 = "모두의창업"
새_문서명 = "모두의 창업 프로젝트 세부관리기준(개정 점검)"
입력 = orchestrate.게이트_입력(400000, "포함", {"인원": 1, "일수": 1})
내부값 = re.compile(r"규범:|세목:|품목:|비목:창업|조:모두의창업|조:지침|조:안내책자|\[S\d+\]")
_임베딩 = "[" + ",".join(["0"] * 1024) + "]"


def _조회() -> dict:
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        return orchestrate.온톨로지_조회(cur, 사업, "지급수수료", "멘토링비", 입력, 확정=True, 품목="외부 전문가 멘토링")


def _근거_대상(cur, 옛: str, 종류: str) -> set[str]:
    return {r[0] for r in cur.execute("SELECT DISTINCT owner_id FROM corpus.provenance WHERE owner_kind = %s AND doc_id = %s",
                                      (종류, 옛)).fetchall()}


def _재검토(cur, 표: str, 키칸: str) -> set[str]:
    return {r[0] for r in cur.execute(f"SELECT {키칸} FROM corpus.{표} WHERE 검수단계 = '재검토'").fetchall()}


def _새판_활성화(dsn: str, 옛: str) -> dict:
    """옛 판 조문을 그대로 쓴 새 판을 staged 로 저장하고 활성화한다. 파싱 · 임베딩은 이 점검의 대상이 아니다."""
    메타 = {"layer": "L2", "발행기관": "창업진흥원", "문서명": 새_문서명, "사업명": 사업, "시행일": date(2027, 1, 1),
           "version": None, "적용대상": "공통"}
    with db.psycopg.connect(dsn, connect_timeout=10) as conn:
        cur = conn.cursor()
        조문 = [{"조번호": r[0], "조제목": r[1], "조순번": r[2], "본문": r[3], "페이지": r[4], "삭제": r[5], "적용대상": "공통"}
              for r in cur.execute("SELECT 조번호, 조제목, 조순번, 본문, 페이지, 삭제 FROM corpus.doc_articles "
                                   "WHERE doc_id = %s ORDER BY article_id", (옛,)).fetchall()]
        # L2 대체는 옛 판 청크로 사업을 확인한다. CI 입력에는 청크가 없어 한 건 만든다
        첫조 = next(i for i, a in enumerate(조문) if not a["삭제"])
        if not cur.execute("SELECT 1 FROM corpus.chunks WHERE doc_id = %s LIMIT 1", (옛,)).fetchone():
            cur.execute(
                """INSERT INTO corpus.chunks (doc_id, article_id, layer, version, status, 조번호, 사업명, 적용대상, text, embedding)
                   SELECT doc_id, article_id, 'L2', '점검', 'active', 조번호, %s, '공통', 본문, %s::extensions.vector(1024)
                     FROM corpus.doc_articles WHERE doc_id = %s AND 조번호 = %s""",
                ([사업], _임베딩, 옛, 조문[첫조]["조번호"]))
        conn.commit()
        doc_id = corpus_ingest.접수(conn, 메타, 원본파일명="개정점검.json", 형식="xml", 저장경로=Path("개정점검-원본없음"))
        작업 = corpus_ingest._작업_읽기(conn, doc_id)
        적재데이터 = {"조문": 조문, "전략": "복원본", "추출방식": "native", "품질": "high", "경고": [],
                  "청크": [{"조": 첫조, "항호": None, "text": 조문[첫조]["본문"], "retrieval_scope": "검색대상", "embedding": _임베딩}]}
        if not corpus_ingest._저장(conn, 작업, 적재데이터):
            raise RuntimeError("staged 저장 실패")
        return corpus_ingest.활성화(conn, doc_id, 대체_doc_id=옛)


def _원천_사본(폴더: Path, 문서키값: str) -> Path:
    """레포 원천은 건드리지 않는다. schema.json 문서키만 새 판으로 바꾼 사본(판이 바뀐 뒤 하는 일과 같다)."""
    원천 = ROOT / "ontology"
    for 이름 in ("schema.json", "공통_L1.json", f"{문서키}.json", ontology_terms.원천_파일):
        shutil.copy(원천 / 이름, 폴더 / 이름)
    for md in (원천 / "추출").rglob("입력_원문.md"):
        대상 = 폴더 / md.relative_to(원천)
        대상.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(md, 대상)
    schema = json.loads((폴더 / "schema.json").read_text(encoding="utf-8"))
    schema["문서키"][문서키] = 문서키값
    (폴더 / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    return 폴더


def _원천_행_고치기(폴더: Path, norm_id: str) -> None:
    """대조 후 원천 행을 고친 경우. 값은 그대로 두고 행 내용만 바꾼다."""
    for 이름 in ("공통_L1.json", f"{문서키}.json"):
        d = json.loads((폴더 / 이름).read_text(encoding="utf-8"))
        for n in d.get("규범") or []:
            if n.get("id") == norm_id:
                n["개정대조"] = "새 판과 대조"
                (폴더 / 이름).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                return
    raise LookupError(f"원천에 없는 규범: {norm_id}")


def 점검(ingest_dsn: str) -> int:
    t: list[tuple[str, bool, str]] = []

    def 기록(이름: str, ok: bool, 설명: str = "") -> None:
        t.append((이름, ok, 설명))
        print(("ok  " if ok else "FAIL"), 이름, f"[{설명}]" if 설명 else "", flush=True)

    옛 = json.loads((ROOT / "ontology" / "schema.json").read_text(encoding="utf-8"))["문서키"][문서키]

    # 활성화 전
    전 = _조회()
    g전 = 전["게이트"]
    발동 = (g전.get("발동규범") or {}).get("norm_id")
    기록("전: 1인 1일 40만원은 코드 불가", g전["종류"] == "한도초과" and ontology_gate.합치기(g전, None)[0] == "불가",
       f"종류 {g전['종류']} · 발동 {발동}")
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        규범들 = _근거_대상(cur, 옛, "규범")
        노드들 = _근거_대상(cur, 옛, "개념")
        용어들 = _근거_대상(cur, 옛, "용어")
        전풀이 = {x["용어"] for x in ontology_terms.풀이_목록(cur, 사업)}
        전검증 = {r[0] for r in cur.execute("SELECT norm_id FROM corpus.norms WHERE verified").fetchall()}
        전버전 = ontology_load._데이터버전(cur)
    기록("전: 재검토 대상이 있고 발동 규범이 그 안에 있다", bool(규범들) and 발동 in 규범들, f"규범 {len(규범들)} · 노드 {len(노드들)}")

    # 새 판 활성화
    활성 = _새판_활성화(ingest_dsn, 옛)
    새 = 활성["doc_id"]
    기록("활성화 응답 경고에 재검토 건수가 있다", any("재검토" in x and f"규범 {len(규범들)}건" in x for x in 활성["경고"]), " | ".join(활성["경고"]))
    with db.connect(autocommit=True) as conn, conn.cursor() as cur:
        재검토규범 = _재검토(cur, "norms", "norm_id")
        재검토노드 = _재검토(cur, "concepts", "concept_id")
        재검토용어 = _재검토(cur, "terminology", "term_id")
        후풀이 = {x["용어"] for x in ontology_terms.풀이_목록(cur, 사업)}
        옛판용어 = {r[0] for r in cur.execute("SELECT 용어 FROM corpus.terminology WHERE term_id = ANY(%s)", (list(용어들),)).fetchall()}
        후검증 = {r[0] for r in cur.execute("SELECT norm_id FROM corpus.norms WHERE verified").fetchall()}
        후버전 = ontology_load._데이터버전(cur)
    기록("옛 판을 가리키는 규범 · 노드가 전부, 그것만 재검토", 재검토규범 == 규범들 and 재검토노드 == 노드들,
       f"규범 {len(재검토규범)} · 노드 {len(재검토노드)}")
    기록("옛 판을 근거로 가진 용어 풀이가 전부, 그것만 재검토", bool(용어들) and 재검토용어 == 용어들, f"용어 {len(재검토용어)}")
    기록("재검토 풀이는 화면 · 질문 프롬프트 목록에서 빠진다", 옛판용어 and not (후풀이 & 옛판용어) and 후풀이 < 전풀이,
       f"{len(전풀이)} → {len(후풀이)}")
    기록("verified: 옛 판 규범만 false, 나머지는 그대로", 후검증 == 전검증 - 규범들 and not (후검증 & 규범들), f"{len(전검증)} → {len(후검증)}")
    기록("데이터 버전 해시가 바뀐다(게스트 캐시 무효)", 전버전 != 후버전)

    # 활성화 뒤 같은 입력
    후 = _조회()
    g후 = 후["게이트"]
    조건 = ontology_gate.화면_조건(g후)
    기록("후: 코드 불가가 나지 않는다", g후["종류"] is None, f"종류 {g후['종류']}")
    # 미검증 한도를 넘는 금액은 판단불가까지 낮춘다(수정 3a, 사용자 결정 대기 9b). 올리지 않는 것을 본다
    기록("후: LLM 이 '가능' 이라 해도 낮춘다", ontology_gate.합치기(g후, "가능")[0] == ("판단불가" if g후["미확정초과"] else "조건부"),
       f"상한 {g후['상한']} · 미확정초과 {g후['미확정초과']}")
    기록("후: 화면 조건이 있고 내부 값이 없다",
       bool(조건) and not any(내부값.search(c["문장"]) for c in 조건), f"{len(조건)}건 · 첫 줄 {조건[0]['문장'] if 조건 else '없음'}")
    남은 = [r for r in 후["근거행"] if r.get("doc_id") == 옛 and r.get("article_id") is not None]
    기록("후: 옛 판 조문은 판정 문맥에 들어가지 않는다", not 남은, f"{len(남은)}건")

    with tempfile.TemporaryDirectory() as 임시:
        폴더 = _원천_사본(Path(임시), 새)
        # 문서키만 바꿔 다시 적재
        기록("문서키만 바꾼 재적재가 통과한다", ontology_load.적재(폴더, None, False, ingest_dsn) == 0)
        with db.connect(autocommit=True) as conn, conn.cursor() as cur:
            유지규범 = _재검토(cur, "norms", "norm_id")
            유지용어 = _재검토(cur, "terminology", "term_id")
            새근거 = cur.execute("SELECT count(*) FROM corpus.provenance WHERE doc_id = %s", (새,)).fetchone()[0]
            옛근거 = cur.execute("SELECT count(*) FROM corpus.provenance WHERE doc_id = %s", (옛,)).fetchone()[0]
        기록("문서키만 바꿔 다시 적재해도 재검토가 유지된다", 유지규범 == 규범들, f"재검토 규범 {len(유지규범)}")
        기록("문서키만 바꿔 다시 적재해도 용어 풀이 재검토가 유지된다", 유지용어 == 용어들, f"재검토 용어 {len(유지용어)}")
        기록("재적재 뒤 근거가 새 판을 가리킨다", 새근거 > 0 and 옛근거 == 0, f"새 판 {새근거} · 옛 판 {옛근거}")
        기록("재적재 뒤에도 코드 불가가 나지 않는다", _조회()["게이트"]["종류"] is None)

        # 대조 후 원천 행을 고치면 그 행만 풀린다
        _원천_행_고치기(폴더, 발동)
        기록("원천 행을 고친 재적재가 통과한다", ontology_load.적재(폴더, None, False, ingest_dsn) == 0)
        with db.connect(autocommit=True) as conn, conn.cursor() as cur:
            풀린뒤 = _재검토(cur, "norms", "norm_id")
        기록("고친 행만 재검토에서 풀린다", 풀린뒤 == 규범들 - {발동}, f"재검토 규범 {len(풀린뒤)}")
        g끝 = _조회()["게이트"]
        기록("풀린 뒤 코드 불가로 돌아온다", g끝["종류"] == "한도초과", f"종류 {g끝['종류']}")

    실패 = [x for x, ok, _ in t if not ok]
    print(f"개정 처리 점검: {len(t) - len(실패)}/{len(t)} 통과")
    return 1 if 실패 else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="개정 처리 점검. 문서를 실제로 활성화한다")
    p.add_argument("--버리는-DB", dest="버리는", action="store_true", help="던져 버릴 DB 에서 돌린다는 확인. 없으면 돌지 않는다")
    a = p.parse_args(argv)
    if not a.버리는:
        p.error("문서를 실제로 활성화한다. 던져 버릴 DB 에서만 --버리는-DB 로 돌린다")
    return 점검(os.environ["SUDDOE_INGEST_DSN"])


if __name__ == "__main__":
    sys.exit(main())
