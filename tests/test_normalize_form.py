# -*- coding: utf-8 -*-
"""정규화 폼 경로 회귀 테스트 — `프론트_API_계약_v1.0.md` §5 대조.

    pytest tests/test_normalize_form.py -q
    python tests/test_normalize_form.py       (pytest 없이도 돈다)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from server.main import app                # noqa: E402

client = TestClient(app)


def _sse_결과(r) -> dict:
    """SSE 응답 텍스트에서 `결과` 이벤트 data 를 뽑는다."""
    이름 = None
    for 줄 in r.text.splitlines():
        if 줄.startswith("event: "):
            이름 = 줄[7:]
        elif 줄.startswith("data: ") and 이름 == "결과":
            return json.loads(줄[6:])
    raise AssertionError(f"결과 이벤트가 없다: {r.text[:200]}")


def test_폼_경로_200_질문원문_합성():
    r = client.post("/api/normalize", json={
        "품목": "노트북", "금액": 1_200_000, "용도": "개발용",
        "사업명": "예비창업패키지",
        "f5": {"친족거래": False, "전직임직원업체": False},
    })
    assert r.status_code == 200
    결과 = _sse_결과(r)
    assert 결과["질문원문"] == "예비창업패키지에서 개발용 노트북 1,200,000원을 사도 되나요?"


def test_자연어_경로_200_질문원문_그대로():
    질문 = "디자이너 쓸 맥북 250만원 사도 되나요?"
    r = client.post("/api/normalize", json={
        "질문": 질문, "사업명": "예비창업패키지",
        "f5": {"친족거래": False, "전직임직원업체": False},
    })
    assert r.status_code == 200
    결과 = _sse_결과(r)
    assert 결과["질문원문"] == 질문


def test_질문과_폼_동시_422():
    r = client.post("/api/normalize", json={
        "질문": "이것도 되나요?", "품목": "노트북", "금액": 100, "용도": "개발용",
    })
    assert r.status_code == 422


def test_폼_필드_일부만_422():
    r = client.post("/api/normalize", json={"품목": "노트북"})   # 금액·용도 없음
    assert r.status_code == 422


def test_아무것도_없으면_422():
    r = client.post("/api/normalize", json={"사업명": "예비창업패키지"})
    assert r.status_code == 422


if __name__ == "__main__":
    for _이름, _fn in sorted(globals().items()):
        if _이름.startswith("test_"):
            _fn()
            print(f"  ok  {_이름}")
    print("전부 통과")
