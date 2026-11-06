# -*- coding: utf-8 -*-
"""l3_doc_check 평가: 실제 문서로 코드 신호와 LLM 모델을 비교한다.

목표는 엉뚱한 문서 자동 적용 0건, 그다음 정상 문서를 사용자에게 묻는 일 최소화다.
문서는 DB 의 L3 조문과 corpus 조문으로 만든다(파일 원본을 다시 읽지 않는다). 판정이 실행마다 달라질 수 있어 반복한다.

실행: PYTHONIOENCODING=utf-8 SUDDOE_DSN=<소유자 DSN> python scripts/eval_l3_doc_check.py
      --models qwen/qwen3.8-flash,qwen/qwen3.7-plus --repeat 3
"""
from __future__ import annotations

import argparse
from collections import Counter

from _lib import db
from _lib.paths import ensure_on_path

ensure_on_path()

from l3_doc_check import 코드_통과, llm_판별   # noqa: E402
from llm_qwen import LLM실패                  # noqa: E402

경상국립대 = "경상국립대학교 창업중심대학사업단"
한국창업대 = "한국창업대학교 산학협력단"
대전과기원 = "대전과학기술원 창업지원단"

# (이름, 사용 기관, 출처 종류, 출처 값, 기대 결과, 분할 전략)
평가셋 = [
    ("사용안내문(자기 기관)", 경상국립대, "l3", "L3_2024창업중심대학_경상국립대_사업비사용안내문.hwp", "적용", "paragraph"),
    ("변경신청서 양식", 경상국립대, "l3", "L3_2024창업중심대학_경상국립대_사업비변경신청서양식.hwp", "확인", "paragraph"),
    ("테스트픽스처 한국창업대", 한국창업대, "l3", "_테스트픽스처_한국창업대학교_창업사업비_집행지침.hwpx", "적용", "article"),
    ("테스트픽스처 대전과기원", 대전과기원, "l3", "_테스트픽스처_대전과학기술원_사업비_운영세칙.hwpx", "적용", "article"),
    ("타기관 규정(한국창업대 문서를 대전과기원에)", 대전과기원, "l3", "_테스트픽스처_한국창업대학교_창업사업비_집행지침.hwpx", "확인", "article"),
    ("타기관 안내문(경상국립대 문서를 한국창업대에)", 한국창업대, "l3", "L3_2024창업중심대학_경상국립대_사업비사용안내문.hwp", "확인", "paragraph"),
    ("안내 책자(창진원)", 경상국립대, "corpus", "2차 모두의 창업 1라운드 진출자 지원 안내_20261015", "확인", "paragraph"),
    ("창진원 세부관리기준", 경상국립대, "corpus", "창업중심대학 세부관리기준2025년 개정", "확인", "article"),
    ("창진원 세부관리기준(초기창업)", 한국창업대, "corpus", "초기창업패키지 세부관리기준(2023년)", "확인", "article"),
    ("무관한 문서(특정금융 시행령)", 경상국립대, "corpus", "L1_특정금융거래정보의보고및이용등에관한법률시행령_20260820", "확인", "article"),
    ("무관한 문서(특정금융 감독규정)", 한국창업대, "corpus", "L1_특정금융거래정보보고및감독규정_20260820", "확인", "article"),
]


def _문서읽기(cur, 종류: str, 값: str) -> tuple[str, list[str], int]:
    if 종류 == "l3":
        행 = cur.execute(
            'SELECT a."조번호", a."조제목", a."본문" FROM tenant.l3_articles a '
            'JOIN tenant.l3_documents d ON d.doc_id = a.doc_id WHERE d."원본파일명" = %s '
            'ORDER BY a."조순번" NULLS LAST', (값,)).fetchall()
    else:
        행 = cur.execute(
            'SELECT "조번호", "조제목", "본문" FROM corpus.doc_articles WHERE doc_id = %s AND NOT "삭제" '
            'ORDER BY "조순번" NULLS LAST', (값,)).fetchall()
    본문 = "\n".join(f'{조번호 or ""} {조제목 or ""}\n{글 or ""}' for 조번호, 조제목, 글 in 행)
    표제 = [f'{조번호 or ""} {조제목 or ""}'.strip() for 조번호, 조제목, _ in 행[:60]]
    return 본문, 표제, len(행)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen/qwen3.8-flash,qwen/qwen3.7-plus")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()
    모델들 = a.models.split(",")
    합계 = {m: Counter() for m in 모델들}
    with db.connect() as conn:
        cur = conn.cursor()
        for 이름, 기관, 종류, 값, 기대, 전략 in 평가셋:
            본문, 표제, 조수 = _문서읽기(cur, 종류, 값)
            if not 조수:
                print(f"\n## {이름}: 문서 없음, 건너뜀")
                continue
            파일명 = 값 if 종류 == "l3" else f"{값}.pdf"
            코드 = 코드_통과(본문, 기관, 조수, 전략)
            print(f"\n## {이름} · 기대 {기대} · 조 {조수} · 글자 {len(본문)} · 코드통과 {코드}")
            if 코드 and 기대 == "확인":
                print("  [위험] 코드 신호가 엉뚱한 문서를 통과시켰다")
            for 모델 in 모델들:
                결과들, r = [], {}
                for _ in range(a.repeat):
                    try:
                        r = llm_판별(파일명, 기관, 본문, 표제, 모델=모델)
                    except LLM실패 as e:
                        합계[모델]["실패"] += 1
                        print(f"  {모델}: 실패 {str(e)[:100]}")
                        continue
                    결과들.append(r["결과"])
                    토큰 = r.get("토큰") or {}
                    합계[모델]["입력토큰"] += 토큰.get("prompt_tokens") or 0
                    합계[모델]["출력토큰"] += 토큰.get("completion_tokens") or 0
                    합계[모델]["호출"] += 1
                    if r["결과"] == "적용" and 기대 == "확인":
                        합계[모델]["엉뚱한 문서 적용"] += 1
                    if r["결과"] == "확인" and 기대 == "적용":
                        합계[모델]["정상 문서 물음"] += 1
                print(f"  {모델}: {Counter(결과들)} · {r.get('문서유형')} / 기관 {r.get('기관일치')} / 기준 {r.get('사업비기준포함')} · {r.get('이유')}")
    print("\n## 합계")
    for 모델, 값 in 합계.items():
        print(f"{모델}: {dict(값)}")


if __name__ == "__main__":
    main()
