# -*- coding: utf-8 -*-
"""`llm_schema.py` · `llm_validate.py` 테스트. **DB 없이 돈다.**

강등 규칙 하나당 케이스 하나. "규칙을 적었다" 와 "규칙이 실제로 걸린다" 는 다르다 —
이 파일이 그 차이를 막는다.

메타·f경로·룰들을 스텁으로 주입해 DB 를 타지 않는다(§계약 주석 참조).
스키마 자체 검증은 `jsonschema` 가 있으면 하고, 없으면 건너뛴다(선택 의존).

실행:  PYTHONIOENCODING=utf-8 python scripts/_work/_test_llm_schema.py
"""
from __future__ import annotations

import io
import json
import os
import sys

if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ))
import llm_schema as S            # noqa: E402
from llm_validate import 검증     # noqa: E402

# ── 고정 픽스처 ──────────────────────────────────────────────────────────────
S맵 = {
    "S01": ("chunk", 12345, "①"),
    "S07": ("article", 6789, None),
    "S12": ("l3", 4321, "②"),
    "S20": ("chunk", 55555, "③"),      # vlm 문서에서 온 청크 (아래 메타 참조)
}
메타 = {   # s번호_메타() 가 돌려주는 모양 그대로
    "S01": dict(doc_id="L1_지침", 조번호="제38조", 조제목="외주용역비",
                원문="③ 용역금액이 2천만원을 초과하는 경우에는 사전 심의를 거쳐야 한다.",
                원문범위="항", version="제14차, 2025.12.23", extraction="native", 항호_DB=None),
    "S07": dict(doc_id="예비창업패키지 세부관리기준(2025년)", 조번호="제22조", 조제목="사업비 비목",
                원문="붙임2 비목 해설표", 원문범위="청크", version="2025",
                extraction="native", 항호_DB="①"),
    "S12": dict(doc_id=None, 조번호="제5조", 조제목="사업비", 원문="L3 원문",
                원문범위="조전체", version=None, extraction="native", 항호_DB=None),
    "S20": dict(doc_id="스캔본", 조번호="제3조", 조제목="적용범위", 원문="스캔 판독본",
                원문범위="청크", version="2024", extraction="vlm", 항호_DB=None),
}
F경로 = {"F1.정부지원.현금", "F1.협약종료일", "F3.비목", "F3.금액", "F4.타사업참여율"}

정상 = {
    "판정": "조건부",
    "요약": "2천만원을 초과하므로 사업운영위원회 사전심의를 받아야 집행할 수 있습니다.",
    "해야할일": [{"항목": "사전심의 신청", "설명": "견적서 2개 이상 첨부"}],
    "인용": ["S01", "S07"],
    "전제": [{"사실": "계약금액이 2천만원을 초과한다", "근거조항": "S01",
             "매핑": ["F3.금액"], "미충족시": "가능"}],
}

결과: list[tuple[bool, str, str]] = []


def 확인(이름: str, 조건: bool, 상세: str = "") -> None:
    결과.append((조건, 이름, 상세))


def V(출력, **kw):
    kw.setdefault("메타", 메타)
    kw.setdefault("f경로", F경로)
    return 검증(출력, S맵, **kw)


# ════════════════════════════════════════════════════════════════════════════
print("── 스키마 ──")
판정s = S.판정_스키마()
확인("판정 enum 4-way 폐쇄", 판정s["properties"]["판정"]["enum"] == ["가능", "조건부", "불가", "판단불가"])
확인("additionalProperties=False", 판정s["additionalProperties"] is False)
확인("인용 items 가 S번호 패턴", 판정s["properties"]["인용"]["items"].get("pattern") == S.S번호_PATTERN)
닫힌s = S.판정_스키마(["S01", "S07"])
확인("s번호 집합 주면 인용이 enum 으로 닫힌다",
     닫힌s["properties"]["인용"]["items"].get("enum") == ["S01", "S07"]
     and 닫힌s["properties"]["전제"]["items"]["properties"]["근거조항"].get("enum") == ["S01", "S07"])

어휘집 = json.load(open(S.어휘집_경로, encoding="utf-8"))["guided_json_enum"]
정규화s = S.정규화_스키마()
확인("비목 enum 이 어휘집에서 실행시점에 온다 (하드코딩 아님)",
     [x for x in 정규화s["properties"]["비목"]["enum"] if x] == 어휘집,
     f"{len(어휘집)}종")
확인("비목 enum 이 소스에 박혀 있지 않다",
     not any(b in open(S.__file__, encoding="utf-8").read()
             for b in ("특허권등무형자산취득비", "창업활동비")))

try:
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(판정s)
    jsonschema.Draft202012Validator.check_schema(정규화s)
    jsonschema.validate(정상, 판정s)
    확인("JSON Schema 문법 유효 + 정상 샘플 통과", True)
    for 나쁨, 왜 in [({**정상, "판정": "아마가능"}, "enum 밖"),
                   ({**정상, "인용": ["Q1"]}, "S번호 패턴 위반"),
                   ({k: v for k, v in 정상.items() if k != "전제"}, "required 누락")]:
        try:
            jsonschema.validate(나쁨, 판정s); 확인(f"스키마가 거부해야 함({왜})", False)
        except jsonschema.ValidationError:
            확인(f"스키마가 거부함({왜})", True)
except ImportError:
    print("   (jsonschema 미설치 — 스키마 문법 검사 건너뜀)")

# ════════════════════════════════════════════════════════════════════════════
print("── 검증·강등 ──")

# 0) 정상
out, why = V(정상)
확인("정상: 판정 유지", out["판정"] == "조건부", out["판정"])
확인("정상: 강등사유 0", why == [], str(why))
확인("정상: 신뢰등급 A", out["신뢰등급"] == "A")
확인("정상: 버전스탬프를 코드가 채움", bool(out["버전스탬프"]), out["버전스탬프"] or "")

# 1) S번호 환각
out, why = V({**정상, "인용": ["S01", "S99"]})
확인("위반1 S번호 환각: S99 폐기", [c["s번호"] for c in out["인용목록"]] == ["S01"])
확인("위반1 사유 기록", any("S99" in w and "폐기" in w for w in why), str(why))

# 2) enum 밖 판정
out, why = V({**정상, "판정": "아마 가능"})
확인("위반2 enum 밖 → 판단불가", out["판정"] == "판단불가", out["판정"])

# 3) 근거조항 없는 전제
out, why = V({**정상, "전제": [{"사실": "근거 없는 주장", "근거조항": None,
                             "매핑": [], "미충족시": "불가"}]})
확인("위반3 근거조항 없는 전제 폐기", out["전제목록"] == [], str(out["전제목록"]))
확인("위반3 사유 기록", any("근거조항 없음" in w for w in why), str(why))

# 3-b) 근거조항이 s맵 밖 (환각)
out, why = V({**정상, "전제": [{"사실": "환각 근거", "근거조항": "S77",
                             "매핑": [], "미충족시": "불가"}]})
확인("위반3-b 전제 근거조항 환각 → 폐기", out["전제목록"] == [])

# 4) F 스키마에 없는 매핑
out, why = V({**정상, "전제": [{"사실": "타사업 참여율 합계", "근거조항": "S01",
                             "매핑": ["F4.타사업참여율", "F9.없는필드"], "미충족시": "불가"}]})
확인("위반4 전제는 살린다", len(out["전제목록"]) == 1)
확인("위반4 미매핑 표시", out["전제목록"][0]["미매핑"] is True)
확인("위반4 unmapped_premise 대상 반환", out["미매핑전제"] == ["F9.없는필드"], str(out["미매핑전제"]))
확인("위반4 DB 쓰기 안 함 (반환만)", "미매핑전제" in out)

# 5) vlm 인용 → A등급 금지
out, why = V({**정상, "인용": ["S01", "S20"]})
확인("위반5 vlm 인용 → B등급", out["신뢰등급"] == "B", str(out["신뢰등급"]))
확인("위반5 사유 기록", any("vlm" in w for w in why), str(why))

# 6) verified=false 룰만으로 '가능'
out, why = V({**정상, "판정": "가능"}, 룰들=[{"verified": False}])
확인("위반6 미검수 룰만으로 가능 → 조건부", out["판정"] == "조건부", out["판정"])
out2, _ = V({**정상, "판정": "가능"}, 룰들=[{"verified": True}])
확인("위반6 대조: 검수된 룰이면 가능 유지", out2["판정"] == "가능", out2["판정"])

# 7) 인용 0건인데 판단불가가 아님
out, why = V({**정상, "인용": []})
확인("위반7 인용 0건 → 판단불가", out["판정"] == "판단불가", out["판정"])
확인("위반7 신뢰등급 없음", out["신뢰등급"] is None)

# ── 인용 치환 (§3-4 [2겹] · 2026-08-31 중앙세션 확정: 검증기가 한다) ────────────
out, why = V(정상)
c0 = out["인용목록"][0]
확인("치환: 조번호", c0["조번호"] == "제38조", str(c0["조번호"]))
확인("치환: 조제목", c0["조제목"] == "외주용역비")
확인("치환: 원문", c0["원문"].startswith("③ 용역금액이"), (c0["원문"] or "")[:20])
확인("치환: version", c0["version"] == "제14차, 2025.12.23")
확인("치환: extraction", c0["extraction"] == "native")
확인("버전스탬프는 인용된 것만", out["버전스탬프"] == "2025, 제14차, 2025.12.23",
     out["버전스탬프"] or "")

# 항호 불일치 — s맵(조립기)이 정본, DB 와 다르면 사유로 남긴다
out, why = V({**정상, "인용": ["S07"]})     # s맵 S07 항호=None, DB 항호=① → 불일치 아님(s맵 None)
확인("항호: s맵이 None 이면 불일치로 안 본다", not any("항호 불일치" in w for w in why))
s맵2 = {**S맵, "S07": ("chunk", 6789, "②")}
out, why = 검증({**정상, "인용": ["S07"]}, s맵2, 메타=메타, f경로=F경로)
확인("항호: s맵 ② vs DB ① → 불일치 기록", any("항호 불일치" in w for w in why), str(why))
확인("항호: s맵 값을 채택", out["인용목록"][0]["항호"] == "②")

# DB 에서 원본을 못 찾으면 폐기 (죽은 id)
out, why = 검증({**정상, "인용": ["S01", "S12"]}, S맵,
                메타={"S01": 메타["S01"]}, f경로=F경로)   # S12 메타 없음
확인("죽은 id: 인용 폐기", [c["s번호"] for c in out["인용목록"]] == ["S01"])
확인("죽은 id: 사유 기록", any("DB 에서 못 찾음" in w for w in why), str(why))

# 항 추출 — 반드시 원문의 정확한 부분 문자열이어야 한다 (생성 금지)
from llm_validate import _항_추출  # noqa: E402
본문 = "제38조(외주용역비) ① 첫째 항이다. ② 둘째 항이다. ③ 셋째 항이다."
잘림, 범위 = _항_추출(본문, "②")
확인("항추출: ② 만 잘라냄", 잘림 == "② 둘째 항이다." and 범위 == "항", f"{잘림!r}/{범위}")
확인("항추출: 결과가 본문의 부분 문자열", 잘림 in 본문)
잘림, 범위 = _항_추출(본문, "⑨")
확인("항추출: 마커 없으면 조 전체 + 사실 표시", 잘림 == 본문 and "미발견" in 범위, 범위)
잘림, 범위 = _항_추출(본문, None)
확인("항추출: 항호 없으면 조 전체", 잘림 == 본문 and 범위 == "조전체")
확인("항추출: 빈 본문 안전", _항_추출("", "①") == ("", "없음"))

# ── 해야할일 code (check_items 폐쇄 목록) ────────────────────────────────────
코드들 = ["외주사전심의", "비교견적준비"]
코드s = S.판정_스키마(코드들=코드들)
h = 코드s["properties"]["해야할일"]["items"]
확인("코드들 주면 해야할일에 code 필수", h["required"] == ["code", "항목", "설명"])
확인("code 가 enum 으로 닫힌다", h["properties"]["code"]["enum"] == 코드들)
확인("코드들 안 주면 §3-4 원형(2필드) 유지",
     S.판정_스키마()["properties"]["해야할일"]["items"]["required"] == ["항목", "설명"])

정상c = {**정상, "해야할일": [{"code": "외주사전심의", "항목": "사전심의 신청", "설명": ""}]}
out, why = V(정상c, 체크코드=코드들)
확인("정상 code 유지", len(out["해야할일"]) == 1)
out, why = V({**정상, "해야할일": [{"code": "지어낸코드", "항목": "뭔가", "설명": ""}]}, 체크코드=코드들)
확인("위반8 없는 code → 폐기", out["해야할일"] == [], str(out["해야할일"]))
확인("위반8 사유 기록", any("check_items 밖" in w for w in why), str(why))
out, why = V(정상, 체크코드=코드들)     # code 없는 항목(§3-4 원형)
확인("code 없는 항목은 통과 (원형 호환)", len(out["해야할일"]) == 1)

# 8) 계약 준수 — 위치인자 2개 호출이 그대로 되는가 (DB 를 타므로 시그니처만 확인)
import inspect  # noqa: E402
sig = inspect.signature(검증)
pos = [p for p in sig.parameters.values()
       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
확인("계약: 위치인자가 (llm출력, s맵) 2개", [p.name for p in pos] == ["llm출력", "s맵"],
     str([p.name for p in pos]))
확인("계약: 나머지는 전부 기본값 있는 키워드",
     all(p.default is not inspect.Parameter.empty
         for p in sig.parameters.values() if p.kind == p.KEYWORD_ONLY))

# ════════════════════════════════════════════════════════════════════════════
통과 = sum(1 for ok, _, _ in 결과 if ok)
print()
for ok, 이름, 상세 in 결과:
    if not ok:
        print(f"   🔴 실패  {이름}  {상세}")
print(f"{통과}/{len(결과)} 통과" + ("  ✅" if 통과 == len(결과) else "  🔴"))
sys.exit(0 if 통과 == len(결과) else 1)
