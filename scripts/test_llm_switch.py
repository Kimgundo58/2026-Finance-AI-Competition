# -*- coding: utf-8 -*-
"""SUDDOE_LLM 스위치가 기본값(미설정=vllm)에서 «지금과 바이트 단위로 같은» 경로인지
identity 로 증명한다. LLM 호출 없음(비용 0) — `llm_qwen.스위치_적용()` 이 세팅하는
객체가 패치 전 원본 그 자체(`is`)인지만 본다. 2026-09-07 ai-33 지시.
"""
import os
import sys

sys.path.insert(0, "scripts")


def test_기본값_vllm_바이트단위_동일():
    os.environ.pop("SUDDOE_LLM", None)
    import llm_qwen
    import normalize_run
    import orchestrate

    backend = llm_qwen.스위치_적용()
    assert backend == "vllm", f"기본값이 vllm 이 아니다: {backend!r}"
    assert normalize_run.llm_호출 is llm_qwen._원본_vllm_llm_호출, \
        "normalize_run.llm_호출 이 패치 전 원본과 다른 객체다 — ①이 갈렸다"
    assert orchestrate.llm_호출 is llm_qwen._원본_vllm_llm_호출, \
        "orchestrate.llm_호출 이 패치 전 원본과 다른 객체다 — ④가 갈렸다"
    print("✅ SUDDOE_LLM 미설정 → normalize_run·orchestrate 모두 원본 llm_호출 그대로(identity)")


def test_qwen_전환():
    os.environ["SUDDOE_LLM"] = "qwen"
    try:
        import llm_qwen
        import normalize_run
        import orchestrate

        backend = llm_qwen.스위치_적용()
        assert backend == "qwen"
        assert normalize_run.llm_호출 is llm_qwen.llm_호출, "①이 qwen 으로 안 갈렸다"
        assert orchestrate.llm_호출 is llm_qwen.llm_호출, "④가 qwen 으로 안 갈렸다"
        print("✅ SUDDOE_LLM=qwen → normalize_run·orchestrate 모두 llm_qwen.llm_호출로 전환")
    finally:
        os.environ.pop("SUDDOE_LLM", None)


def test_왕복_기본값으로_복귀():
    """qwen 으로 갔다가 다시 vllm(미설정)으로 — idempotent 확인."""
    os.environ["SUDDOE_LLM"] = "qwen"
    import llm_qwen
    llm_qwen.스위치_적용()
    os.environ.pop("SUDDOE_LLM", None)
    backend = llm_qwen.스위치_적용()
    import normalize_run
    import orchestrate
    assert backend == "vllm"
    assert normalize_run.llm_호출 is llm_qwen._원본_vllm_llm_호출
    assert orchestrate.llm_호출 is llm_qwen._원본_vllm_llm_호출
    print("✅ qwen -> vllm 왕복 후에도 원본으로 정확히 복귀(identity)")


if __name__ == "__main__":
    test_기본값_vllm_바이트단위_동일()
    test_qwen_전환()
    test_왕복_기본값으로_복귀()
    print("\n전부 통과")
