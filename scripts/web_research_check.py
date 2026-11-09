# -*- coding: utf-8 -*-
"""외부 네트워크·API 키 없이 웹 조사 안전 경로를 검사한다."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from apick_mcp import ApickClient, ApickError, _key, parse_response, public_url
from research_runtime import Budget as RuntimeBudget, activate, current_budget, final_call, is_final_call
from web_research import _links, _확인질문, research


class Budget:
    def __init__(self, tools=6, evidence=True):
        self.tools = tools
        self.evidence = evidence

    def reserve_tool(self):
        if self.tools <= 0:
            return False
        self.tools -= 1
        return True

    def remaining_seconds(self):
        return 100.0

    def add_evidence(self, text):
        return self.evidence


class Client:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def call(self, name, args, timeout):
        self.calls.append((name, args))
        if name == "google_search":
            return {"content": [{"type": "text", "text": '{"link":"https://example.com/product"}'}]}
        return {"content": [{"type": "text", "text": self.body}]}


def run(body, excerpt, budget=None, name="Example"):
    plans = iter([
        {"행동": "검색", "유형": "제품성격", "검색어": "Example official product", "부족정보": [], "URL": "", "후보": []},
        {"행동": "본문", "유형": "제품성격", "검색어": "", "부족정보": [], "URL": "https://example.com/product", "후보": []},
        {"행동": "완료", "유형": "제품성격", "검색어": "", "부족정보": [], "URL": "", "후보": [{"유형": "제품성격", "내용": "임의 해석", "발췌": excerpt, "URL": "https://example.com/product"}]},
    ])
    client = Client(body)
    events = []
    result = research(name, budget=budget or Budget(), client=client,
                      llm_call=lambda p, s, t: next(plans), emit=events.append)
    return result, client.calls, events


def main():
    assert public_url("https://example.com/product")
    for url in ("https://example.com/article?id=123&page=2&lang=ko#section-3",
                "https://example.com/view?article_id=42", "https://example.com/info#top"):
        assert public_url(url), url
    for url in ("http://127.0.0.1/a", "http://169.254.169.254/a", "http://localhost/a", "https://user:pass@example.com", "https://example.com/?token=secret", "https://example.com/login", "http://0x7f000001/a",
                "https://example.com/?id=01012345678", "https://example.com/?email=user%40example.com",
                "https://example.com/?id=abc123", "https://example.com/?id=1&token=secret",
                "https://example.com/?%74oken=secret", "https://example.com/?next=https%3A%2F%2Fprivate.local",
                "https://example.com/?id=1#session-secret", "https://example.com/?id=1;token=secret"):
        assert not public_url(url), url
    safe = "https://example.com/article?id=123&page=2&lang=ko#section-3"
    fixture = {"content": [{"type": "text", "text": json.dumps({"success": True, "data": {"results": [
        {"title": "문서", "link": safe}, {"link": "https://example.com/?token=secret"},
        {"url": "http://127.0.0.1/private"}]}})}]}
    assert _links(fixture) == [safe]
    assert _links({"structuredContent": {"results": [{"url": safe}]}}) == [safe]
    class QueryClient(Client):
        def call(self, name, args, timeout):
            self.calls.append((name, args))
            return fixture if name == "google_search" else {"content": [{"type": "text", "text": self.body}]}
    query_body = "Example official documentation. This is a search API for developers. Terms apply to all accounts."
    query_client = QueryClient(query_body)
    query_plans = iter([
        {"행동": "검색", "유형": "제품성격", "검색어": "Example official product", "후보": []},
        {"행동": "본문", "유형": "제품성격", "URL": safe, "후보": []},
        {"행동": "완료", "후보": [{"유형": "제품성격", "내용": "확인", "발췌": "This is a search API for developers.", "URL": safe}]},
    ])
    query_result = research("Example", budget=Budget(), client=query_client, llm_call=lambda p, s, t: next(query_plans))
    assert query_client.calls[1][1]["url"] == safe and query_result["후보"][0]["출처"][0]["url"] == safe
    body = "Example official documentation. This is a search API for developers. Terms apply to all accounts."
    result, calls, events = run(body, "This is a search API for developers.")
    assert len(calls) == 2 and len(events) == 4
    assert result["후보"][0]["내용"] == "This is a search API for developers."
    assert result["후보"][0]["상태"] == "확인필요"
    assert not run(body, "This is a search API. Terms apply")[0]["후보"]
    # 30000자 상한으로 잘린 본문도 쓰되 발췌는 본문과 글자 그대로 일치해야 한다.
    assert run(body + "...", "This is a search API for developers.")[0]["후보"]
    assert run(body * 400, "This is a search API for developers.")[0]["후보"]
    assert not run(body, "This is a search API for developers.", Budget(evidence=False))[0]["후보"]
    assert not run(body, "This is a search API for developers.", Budget(tools=0))[1]
    assert not run(body, "This is a search API for developers.", name="홍길동 전화 01012345678")[1]
    ok = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    assert parse_response(json.dumps(ok).encode(), "application/json") == {"content": []}
    assert parse_response(("event: message\ndata: " + json.dumps(ok) + "\n\n").encode(), "text/event-stream") == {"content": []}
    for bad in ({"jsonrpc": "2.0", "id": 1, "error": {"message": "fake-secret"}},
                {"jsonrpc": "2.0", "id": 1, "result": {"isError": True}},
                {"jsonrpc": "2.0", "id": 2, "result": {}}):
        try:
            parse_response(json.dumps(bad).encode(), "application/json")
            raise AssertionError("잘못된 MCP 응답 허용")
        except ApickError as exc:
            assert "fake-secret" not in str(exc)
    ip_error = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True,
                "content": [{"type": "text", "text": "[401] 허용되지 않은 IP주소 입니다. fake-secret"}]}}
    try:
        parse_response(json.dumps(ip_error, ensure_ascii=False).encode(), "application/json")
        raise AssertionError("IP 오류 허용")
    except ApickError as exc:
        assert exc.code == "APICK_IP_DENIED" and "fake-secret" not in str(exc)
    class DeniedClient:
        def call(self, name, args, timeout):
            raise ApickError("안전한 오류", code="APICK_IP_DENIED")
    with patch("web_research._log.warning") as warning:
        denied = research("Example", budget=Budget(), client=DeniedClient(),
                          llm_call=lambda p, s, t: {"행동": "검색", "유형": "제품성격", "검색어": "Example official product", "부족정보": [], "URL": "", "후보": []})
    assert denied == {"상태": "실패", "사유": "검색 서비스 연결 설정을 확인해야 합니다.", "후보": [], "출처": []}
    warning.assert_called_once_with("APICK_IP_DENIED")
    clock = [0.0]
    deadlines = []
    def late_plan(prompt, schema, timeout):
        deadlines.append(timeout)
        clock[0] = 61
        return {"행동": "검색", "유형": "제품성격", "검색어": "Example official product", "부족정보": [], "URL": "", "후보": []}
    fake_client = Client(body)
    with patch("web_research.time.monotonic", side_effect=lambda: clock[0]):
        assert research("Example", budget=Budget(), client=fake_client, llm_call=late_plan)["상태"] == "미확인"
    assert deadlines == [60] and not fake_client.calls
    fake_client = Client(body)
    bad_query = lambda p, s, t: {"행동": "검색", "유형": "판매자", "검색어": "Example secret-contact official company", "부족정보": [], "URL": "", "후보": []}
    research("Example", requirements=[{"내용": "secret-contact"}], context={"거래처": "secret-contact"}, budget=Budget(), client=fake_client, llm_call=bad_query)
    # 허용 밖 낱말은 보내지 않고 공개 검색어만으로 검색한다.
    assert fake_client.calls[0] == ("google_search", {"keyword": "Example"})
    assert "secret-contact" not in json.dumps(fake_client.calls, ensure_ascii=False)
    no_tool = Client(body)
    research("Example 2026 official", budget=Budget(tools=0), client=no_tool, llm_call=bad_query)
    assert not no_tool.calls
    # 첫 계획이 사용자확인이어도 부족한 공개 정보가 있으면 먼저 검색·본문을 확인한다(운영 2026-11-10 5/5 재현).
    first_confirm = Client(body)
    confirm_plans = iter([
        {"행동": "사용자확인", "유형": "공개등록", "검색어": "Example 공식 결제 세금계산서", "부족정보": ["가격"], "확인항목": ["실제결제"], "URL": "", "후보": []},
        {"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": ["가격"], "확인항목": ["실제사용"], "URL": "", "후보": []},
        {"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": [], "확인항목": [], "URL": "", "후보": [{"유형": "가격", "내용": "x", "발췌": "This is a search API for developers.", "URL": "https://example.com/product"}]},
    ])
    confirmed = research("Example", budget=Budget(), client=first_confirm, llm_call=lambda p, s, t: next(confirm_plans))
    assert [c[0] for c in first_confirm.calls] == ["google_search", "url_to_markdown"], first_confirm.calls
    assert first_confirm.calls[0][1] == {"keyword": "Example 요금 가격"}
    assert len(confirmed["후보"]) == 1 and confirmed["상태"] == "확인필요"
    assert [x["항목"] for x in confirmed["사용자확인"]] == ["실제결제", "실제사용"]
    # 두 주소를 동시에 읽고 한 페이지가 실패해도 다른 본문으로 다음 계획이 발췌를 고른다.
    class TwoLinks(Client):
        def call(self, name, args, timeout):
            self.calls.append((name, args, timeout))
            if name == "google_search":
                return {"content": [{"type": "text", "text": '[{"link":"https://a.example.com/x"},{"link":"https://b.example.com/y"}]'}]}
            if "a.example" in args["url"]:
                raise ApickError("MCP 도구 오류")
            return {"content": [{"type": "text", "text": self.body}]}
    two = TwoLinks(body)
    two_plans = iter([
        {"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": ["가격"], "확인항목": [], "URL": "", "후보": []},
        {"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": ["가격"], "확인항목": [], "URL": "", "후보": []},
        {"행동": "완료", "유형": "가격", "검색어": "", "부족정보": [], "확인항목": [], "URL": "", "후보": [{"유형": "가격", "내용": "x", "발췌": "This is a search API for developers.", "URL": "https://b.example.com/y"}]},
    ])
    got = research("Example", budget=Budget(), client=two, llm_call=lambda p, s, t: next(two_plans))
    assert sorted(c[1].get("url", "") for c in two.calls[1:]) == ["https://a.example.com/x", "https://b.example.com/y"], two.calls
    assert len(got["후보"]) == 1 and got["출처"] == ["https://b.example.com/y"]
    # 도구 호출별 상한: 검색 20초, 본문 15초
    assert two.calls[0][2] <= 20 and all(c[2] <= 15 for c in two.calls[1:])
    # 동시에 읽은 묶음에 연결 설정 오류가 있으면 성공한 본문도 쓰지 않고 실패로 끝낸다.
    class MixedDenied(TwoLinks):
        def call(self, name, args, timeout):
            if name == "url_to_markdown" and "a.example" in args["url"]:
                raise ApickError("x", code="APICK_IP_DENIED")
            return TwoLinks.call(self, name, args, timeout)
    mixed_plans = iter([{"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": ["가격"], "확인항목": [], "URL": "", "후보": []}] * 3)
    ev = []
    with patch("web_research._log.warning"):
        mixed = research("Example", budget=Budget(), client=MixedDenied(body), llm_call=lambda p, s, t: next(mixed_plans), emit=ev.append)
    assert mixed["상태"] == "실패" and mixed["출처"] == [], mixed
    열림 = {e["id"] for e in ev if e["상태"] == "진행중"}
    assert 열림 and 열림 <= {e["id"] for e in ev if e["상태"] in ("완료", "실패")}
    # 검색 결과 주소의 추적 query 제거와 사이트별 공개 식별자
    from apick_mcp import strip_tracking
    naver = "https://m.blog.naver.com/PostView.naver?blogId=ikso2000&logNo=223456789012&categoryNo=3&proxyReferer=&noTrackingCode=true"
    assert public_url(strip_tracking(naver)) and "proxyReferer" not in strip_tracking(naver)
    assert not public_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")  # 영상 페이지는 본문이 없다
    assert public_url(strip_tracking("https://www.reddit.com/r/x/comments/1j/abc/?tl=ko"))
    # 검색이 막혀도 부족한 공개 정보가 있으면 확인필요, 같은 검색어는 한 번만 보낸다.
    blocked_plan = lambda p, s, t: {"행동": "사용자확인", "유형": "가격", "검색어": "", "부족정보": ["가격"], "확인항목": [], "URL": "", "후보": []}
    assert research("Example", budget=Budget(tools=0), client=Client(body), llm_call=blocked_plan)["상태"] == "확인필요"
    repeat = Client(body)
    kinds = iter(["판매자", "가격", "통화"])
    research("Example", budget=Budget(), client=repeat, llm_call=lambda p, s, t: {"행동": "검색", "유형": next(kinds), "검색어": "Example secret", "부족정보": [], "URL": "", "후보": []})
    assert [c[1] for c in repeat.calls if c[0] == "google_search"] == [{"keyword": "Example"}], repeat.calls
    for url in ("https://example.com/?logNo=01012345678", "https://m.blog.naver.com/PostView.naver?blogId=a&logNo=01012345678", "https://example.com/?v=abcdefghijk",
                "https://m.blog.naver.com/PostView.naver?blogId=a&logNo=1&token=x"):
        assert not public_url(strip_tracking(url)), url
    pages = Client(body)
    prompts = []
    plans = iter([
        {"행동": "검색", "유형": "판매자", "검색어": "Example official seller company", "부족정보": ["판매자"], "확인항목": [], "URL": "", "후보": []},
        {"행동": "본문", "유형": "판매자", "검색어": "", "부족정보": ["판매자"], "확인항목": [], "URL": "https://example.com/product", "후보": []},
        {"행동": "사용자확인", "유형": "판매자", "검색어": "", "부족정보": [], "확인항목": ["결제수단"], "URL": "", "후보": [{"유형": "판매자", "내용": "판매자", "발췌": "This is a search API for developers.", "URL": "https://example.com/product"}]},
    ])
    def planner(prompt, schema, timeout):
        prompts.append(prompt)
        return next(plans)
    researched = research("Example", requirements=[{"요건": "비공개 요건"}], context={"문맥": "비공개 문맥"}, budget=Budget(), client=pages, llm_call=planner)
    assert "비공개 요건" in prompts[0] and "비공개 문맥" in prompts[0]
    assert pages.calls[0][1] == {"keyword": "Example official seller company"}
    assert "비공개" not in json.dumps(pages.calls, ensure_ascii=False)
    assert len(pages.calls) == 2 and len(researched["후보"]) == 1
    assert researched["사용자확인"][0]["항목"] == "결제수단" and researched["상태"] == "확인필요"
    try:
        ApickClient("fake-secret").call("url_html", {}, 1)
        raise AssertionError("허용되지 않은 도구")
    except ApickError as exc:
        assert "fake-secret" not in str(exc)
    with tempfile.TemporaryDirectory() as directory:
        keyfile = Path(directory) / "synthetic.key"
        with patch.dict(os.environ, {"SUDDOE_APICK_API_KEY_FILE": str(keyfile), "SUDDOE_APICK_API_KEY": "other-synthetic"}):
            keyfile.write_text("synthetic-value\n", encoding="utf-8")
            assert _key() == "synthetic-value"
            keyfile.write_text("SUDDOE_APICK_API_KEY=synthetic-value\n", encoding="utf-8")
            assert _key() == "synthetic-value"
            keyfile.write_text("synthetic-value\nother", encoding="utf-8")
            try:
                _key()
                raise AssertionError("여러 줄 키 허용")
            except ApickError as exc:
                assert "synthetic-value" not in str(exc)
    clock = [0.0]
    with patch("research_runtime.time.monotonic", side_effect=lambda: clock[0]):
        b = RuntimeBudget(seconds=10, tools=1, llm=3, evidence_chars=5)
        with activate(b):
            assert current_budget() is b
            assert b.reserve_tool() and not b.reserve_tool()
            assert b.reserve_llm() and b.reserve_llm() and not b.reserve_llm()
            with final_call():
                assert is_final_call() and b.reserve_llm(final=True)
            assert not is_final_call() and not b.reserve_llm(final=True)
            assert b.add_evidence("12345") and not b.add_evidence("6")
            clock[0] = 3
        assert current_budget() is None
        clock[0] = 103
        assert b.remaining_seconds() == 7
        with activate(b):
            clock[0] = 111
            assert b.remaining_seconds() == 0 and not b.reserve_tool()
        b.cancel()
        assert b.remaining_seconds() == 0
    # 실제 SDK와 네트워크를 사용하지 않는다.
    with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=object)}):
        import llm_qwen
    sent = []
    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs)
            if len(sent) == 1:
                raise RuntimeError("synthetic failure")
            msg = SimpleNamespace(content='{"ok": true}', model_extra={})
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    b = RuntimeBudget(llm=3)
    with patch.object(llm_qwen, "_client_얻기", return_value=fake_client), activate(b):
        result, _ = llm_qwen.llm_호출("prompt", {"type": "object"}, 모델="qwen3.7-plus", 재시도=1)
        assert result == {"ok": True} and len(sent) == 2 and b.llm_used == 2
        try:
            llm_qwen.llm_호출("prompt", None, 모델="qwen3.7-plus", 재시도=0)
            raise AssertionError("최종 판정 몫 사용")
        except llm_qwen.LLM실패:
            assert len(sent) == 2
        with final_call():
            llm_qwen.llm_호출("prompt", None, 모델="qwen3.7-plus", 재시도=0)
        assert len(sent) == 3
        b.cancel()
        try:
            with final_call():
                llm_qwen.llm_호출("prompt", None, 모델="qwen3.7-plus", 재시도=0)
            raise AssertionError("취소 후 호출")
        except llm_qwen.LLM실패:
            assert len(sent) == 3
    # 확인 질문: 발췌에 있는 숫자·통화만 쓴 문장은 그대로, 아니면 기본 문장. 내용은 언제나 발췌다.
    발췌 = "### 플러스\n\n₩14,000\n\n멤버 1인당 / 1개월"
    assert _확인질문("노션 플러스 요금은 멤버 1인당 월 14,000원이에요. 맞나요?", 발췌, "가격") == ("노션 플러스 요금은 멤버 1인당 월 14,000원이에요. 맞나요?", "문장")
    assert _확인질문("플러스 요금은 월 15,000원이에요. 맞나요?", 발췌, "가격") == ("가격 정보가 맞나요?", "기본")
    assert _확인질문("플러스 요금은 월 14,000달러예요. 맞나요?", 발췌, "가격")[1] == "기본"
    assert _확인질문("### 플러스 요금은 14,000원", 발췌, "가격")[1] == "기본"
    assert _확인질문("자세한 내용은 https://example.com 을 보세요", 발췌, "가격")[1] == "기본"
    assert _확인질문("가" * 121, 발췌, "가격")[1] == "기본" and _확인질문(None, 발췌, "결제안내") == ("결제 방법 정보가 맞나요?", "기본")
    # URL만 받은 조사: 검색 없이 그 주소 본문 하나만 읽고, 발췌는 본문 그대로일 때만 후보가 된다.
    page = "https://example.com/product"
    body = "Example Pro is a hosted design service billed monthly in KRW for business teams."
    plans = iter([{"행동": "완료", "유형": "제품성격", "검색어": "", "부족정보": [], "확인항목": [], "URL": "",
                   "후보": [{"유형": "제품성격", "내용": "x", "발췌": "hosted design service billed monthly", "URL": page}]}])
    client = Client(body)
    prompts = []
    result = research("", public_url=page, budget=Budget(), client=client,
                      llm_call=lambda p, s, t: (prompts.append(p), next(plans))[1])
    assert [c[0] for c in client.calls] == ["url_to_markdown"], client.calls
    assert client.calls[0][1]["url"] == page
    assert result["상태"] == "확인필요" and len(result["후보"]) == 1 and len(prompts) == 1, result
    후보 = result["후보"][0]
    assert 후보["내용"] == "hosted design service billed monthly" == 후보["출처"][0]["발췌"], 후보
    assert (후보["질문"], 후보["질문종류"]) == ("제품·서비스 성격 정보가 맞나요?", "기본"), 후보   # 「x」는 너무 짧다
    # 계획이 검색을 골라도 URL만 받은 조사는 검색하지 않는다.
    plans = iter([{"행동": "검색", "유형": "가격", "검색어": "Example pricing", "부족정보": ["가격"], "확인항목": [], "URL": "", "후보": []}] * 3)
    client = Client(body)
    research(None, public_url=page, budget=Budget(), client=client, llm_call=lambda p, s, t: next(plans))
    assert all(c[0] != "google_search" for c in client.calls), client.calls
    # 검색어도 URL도 없거나, URL이 공개 주소가 아니면 아무 호출도 하지 않는다.
    for query, url in (("", None), (None, None), ("", "http://127.0.0.1/a"), ("", "https://example.com/?token=secret")):
        client = Client(body)
        out = research(query, public_url=url, budget=Budget(), client=client,
                       llm_call=lambda p, s, t: (_ for _ in ()).throw(AssertionError("LLM 호출")))
        assert client.calls == [] and out["후보"] == [], (query, url, client.calls)
    print("웹 조사 모의 검사 통과")


if __name__ == "__main__":
    main()
