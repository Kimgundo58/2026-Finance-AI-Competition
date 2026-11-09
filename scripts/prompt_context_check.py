# -*- coding: utf-8 -*-
"""원문·인용 보존과 B4의 중복 발췌 축약을 합성 자료로 확인한다."""
from __future__ import annotations

import assemble_context as ac
import sys


def main():
    원문 = "① 제품 개발을 위한 검색 API 사용료는 사업 수행에 필요한 경우 집행할 수 있다."
    예외 = "단, 별도 증빙을 갖추어야 하고 승인 여부는 기관 기준을 확인해야 한다."
    중복 = "여러 조항에 동일하게 반복되는 충분히 긴 문장은 발췌를 그대로 보존한다."
    문맥 = {"블록": [("B2. 원문", [("article", 1, "규정 제1조", 원문 + " " + 예외, "①"),
                                 ("article", 2, "규정 제2조", 중복, "①"),
                                 ("l3", 3, "기관 제3조", 중복, "①")])]}
    요약 = f"- 허용: 「{원문}」\n- 금지: 「{중복}」 예외가 있다: 「{예외}」.\n- 확인 필요: 조건을 확인한다."
    기존 = ac._중복발췌_참조
    try:
        ac._중복발췌_참조 = lambda text, sources: text
        이전, 이전맵, _ = ac.프롬프트_구성(None, "검색 API 비용", {}, 룰결과=요약, 문맥=문맥)
    finally:
        ac._중복발췌_참조 = 기존
    이후, 이후맵, _ = ac.프롬프트_구성(None, "검색 API 비용", {}, 룰결과=요약, 문맥=문맥)
    assert 이전맵 == 이후맵 == {"S01": ("article", 1, "①"), "S02": ("article", 2, "①"), "S03": ("l3", 3, "①")}
    assert 원문 in 이후 and 예외 in 이후 and f"- 허용: 「[S01]의 원문」" in 이후
    assert f"- 금지: 「{중복}」 예외가 있다: 「{예외}」" in 이후
    assert "- 확인 필요: 조건을 확인한다." in 이후 and len(이후) < len(이전)
    # 짧은 발췌, 동일 문장의 동일 항 내 중복, 원문 없는 발췌는 바꾸지 않는다.
    짧음 = "- 허용: 「검색 API」"
    assert 기존(짧음, {"S01": "검색 API"}) == 짧음
    중복행 = f"- 요건: 「{중복}」"
    assert 기존(중복행, {"S01": 중복 + 중복}) == 중복행
    assert 기존(중복행, {}) == 중복행
    바꿈 = 원문.replace("검색 API", "검색\n  API")
    assert 기존(f"- 허용: 「{바꿈}」", {"S01": 원문}) == "- 허용: 「[S01]의 원문」"
    assert 기존(f"- 허용: 「{바꿈}」", {"S01": 원문.replace("API", "포털")}) != "- 허용: 「[S01]의 원문」"
    print(f"synthetic prompt chars before={len(이전)} after={len(이후)}; SID and evidence preserved")


def db_check():
    import ontology_fund
    import ontology_lookup
    import orchestrate
    from _lib import db
    with db.connect(connect_timeout=5, autocommit=True) as conn:
        assert ontology_lookup.새경로_대상(conn.cursor(), ontology_fund._사업), "새 경로 비활성"
    정규화 = {"품목": "APICK API검색크레딧", "용도": "제품개발용AI검색", "금액": 55000, "비목후보": []}
    인자 = {"사업명": ontology_fund._사업, "확정비목": "창업활동비", "확정세목": "지급수수료",
            "정규화결과": 정규화, "부가세구분": "포함", "dry": True, "기록": False}
    원래 = ac._중복발췌_참조
    원래구성 = orchestrate.프롬프트_구성
    프롬프트들 = []
    문장맵들 = []
    def 수집(*args, **kwargs):
        반환 = 원래구성(*args, **kwargs)
        프롬프트들.append(반환[0])
        문장맵들.append(반환[1])
        return 반환
    orchestrate.프롬프트_구성 = 수집
    결과 = []
    try:
        for 적용 in (False, True):
            ac._중복발췌_참조 = 원래 if 적용 else lambda text, sources: text
            결과.append(orchestrate.판정("APICK API 검색 크레딧 제품 개발 55000원", **인자))
    finally:
        ac._중복발췌_참조 = 원래
        orchestrate.프롬프트_구성 = 원래구성
    for label, row in zip(("before", "after"), 결과):
        print(label, "prompt_chars", row.get("프롬프트길이"), "verdict", row.get("판정"),
              "reason", row.get("검증사유"))
    if not all(isinstance(r.get("프롬프트길이"), int) for r in 결과):
        raise SystemExit(1)
    assert all(r.get("경로") == "온톨로지" for r in 결과), "새 경로 결과 아님"
    assert len(프롬프트들) == 2
    assert 문장맵들[0] == 문장맵들[1]
    assert 프롬프트들[0].split("## B4.")[0] == 프롬프트들[1].split("## B4.")[0]


if __name__ == "__main__":
    main()
    if "--db" in sys.argv:
        db_check()
