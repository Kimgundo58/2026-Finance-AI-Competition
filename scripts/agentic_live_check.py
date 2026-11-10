# -*- coding: utf-8 -*-
"""로컬 읽기 전용 DB와 실제 LLM으로 합성 공개 사실의 경로만 확인한다.

외부 웹 검색을 하지 않는다. 본문·질문·응답·키는 출력하거나 저장하지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import llm_qwen
import orchestrate
import question_generate
import research_runtime
import ontology_fund
import ontology_lookup
from _lib import db

logging.disable(logging.CRITICAL)
MAX_HTTP = 12
traces = []
stage = ""
case_name = ""
original = llm_qwen._호출_1회


def safe(value):
    if not isinstance(value, str):
        return value
    for name, secret in os.environ.items():
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)bearer\s+\S+", "[REDACTED]", value)[:1500]


def measured(client, model, prompt, schema, temperature, tokens, timeout, thinking):
    if len(traces) >= MAX_HTTP:
        raise llm_qwen.LLM실패("호출 한도")
    row = {"case": case_name, "stage": stage, "model": model,
           "prompt_chars": len(prompt), "schema_chars": len(json.dumps(schema, ensure_ascii=False)) if schema else 0,
           "success": False}
    traces.append(row)
    t = time.monotonic()
    try:
        value, meta = original(client, model, prompt, schema, temperature, tokens, timeout, thinking)
        row.update(success=True, usage=meta.get("토큰"))
        return value, meta
    finally:
        row["elapsed_ms"] = round((time.monotonic() - t) * 1000)


def main():
    global case_name, stage
    assert os.environ.get("SUDDOE_QWEN_MODEL") == "qwen/qwen3.7-plus"
    assert os.environ.get("SUDDOE_QWEN_FALLBACK_MODEL") == "qwen/qwen3.8-flash"
    assert "default_transaction_read_only=on" in os.environ.get("PGOPTIONS", "")
    with db.connect(connect_timeout=5, autocommit=True) as conn:
        assert ontology_lookup.새경로_대상(conn.cursor(), ontology_fund._사업), "새 경로 비활성"
    llm_qwen._호출_1회 = measured
    입력 = {"사업명": ontology_fund._사업, "비목": "창업활동비", "확정세목": "지급수수료",
          "품목": "APICK API검색크레딧", "용도": "제품개발용AI검색", "금액": 55000,
          "부가세구분": "포함", "질문": "제품 개발용 AI 검색 API 크레딧 55000원"}
    확인 = {"확인한정보": [{"유형": "제품성격", "내용": "합성 사례: 개발용 검색 API 호출에 쓰는 크레딧이다.", "답": "맞아요"}]}
    결과 = []
    for case_name, 조사 in (("no_fixture", None), ("synthetic_fixture_confirmed", 확인)):
        budget = research_runtime.Budget()
        with research_runtime.activate(budget):
            stage = "questions"
            질문 = question_generate.질문생성({**입력, **({"_조사확인": 조사} if 조사 else {})},
                                             org_id=None, account_id=None, 모델="qwen/qwen3.7-plus")
            stage = "judgment"
            판정 = orchestrate.판정(입력["질문"], 사업명=입력["사업명"], 확정비목=입력["비목"],
                확정세목=입력["확정세목"], 정규화결과={"품목": 입력["품목"], "용도": 입력["용도"],
                "금액": 입력["금액"], "비목후보": []}, 부가세구분=입력["부가세구분"],
                org_id=None, account_id=None, 노드결정=질문.get("노드결정"),
                상황노드=질문.get("상황노드"), l3숫자라벨=질문.get("L3숫자라벨"),
                조사=조사, 기록=False)
        결과.append({"case": case_name, "question_status": 질문.get("상태"),
                   "question_count": len(질문.get("질문") or []),
                   "node_selected": bool(질문.get("노드결정")), "verdict": 판정.get("판정"),
                   "path": 판정.get("경로"), "summary": safe(판정.get("요약")),
                   "validation_reasons": [safe(x) for x in 판정.get("검증사유") or [] if isinstance(x, str)],
                   "failure_stage": bool(판정.get("실패단계") or 판정.get("실패경로")),
                   "citation_count": len(판정.get("인용목록") or []),
                   "tool_calls": budget.tools_used, "llm_http_calls": budget.llm_used})
    print(json.dumps({"scope": "synthetic_fixture_local_readonly", "results": 결과, "calls": traces}, ensure_ascii=True))
    assert all(x["path"] == "온톨로지" for x in 결과), "새 경로 결과 아님"


if __name__ == "__main__":
    main()
