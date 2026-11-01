# -*- coding: utf-8 -*-
"""rule_lookup 회귀 확인 CLI: 순수 함수 자기검사, 실제 DB 스모크, 금지목록 매칭 감사.

판정 경로에서는 쓰지 않는다. --self-test 는 DB 없이 돌고, --smoke·--audit 은 corpus·tenant·eval
스키마를 읽기만 한다(--audit 만 결과 JSON 을 scripts/_work/ 에 쓴다).
--audit 의 자기적중 검사는 금지목록 매칭(정규화 뒤 문자 그대로 일치)이 바뀌지 않았는지 보는
회귀 검사다. 무조건 금지예시 전부가 자기 자신에 적중해야 통과한다.

실행: PYTHONIOENCODING=utf-8 python scripts/rule_lookup_check.py --self-test | --smoke | --audit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db, paths                                            # noqa: E402
import rule_lookup as rl                                              # noqa: E402

_감사_출력 = paths.ROOT / "scripts" / "_work" / "rule_lookup_audit.json"


def _connect():
    """읽기 전용 커넥션(autocommit).

    읽기만 하는 커넥션이 트랜잭션을 열어 두면 idle in transaction 상태로 락을 쥔 채 남아 DDL 과
    교착할 수 있어 트랜잭션을 열지 않는다.
    """
    return db.connect(autocommit=True)


# 자기검사 (DB 없음)

class _가짜커서:
    """FROM 절의 테이블 이름으로 미리 정한 행을 돌려주는 커서. effective_rule 의 연결을 DB 없이 본다.

    WHERE 조건은 해석하지 않는다. 실행한 SQL 과 인자는 기록에 남는다. 실패 에 준 문자열이 SQL 에
    있으면 RuntimeError 를 낸다(DB 오류 주입).
    """

    def __init__(self, 표: dict, 실패: str | None = None):
        self.표, self.실패 = 표, 실패
        self.기록: list[tuple[str, list]] = []
        self.description: list[tuple] | None = None
        self._결과: list[tuple] = []

    def execute(self, sql, args=None):
        self.기록.append((sql, list(args or [])))
        if self.실패 and self.실패 in sql:
            raise RuntimeError("주입한 DB 오류")
        컬럼, 행 = self.표.get(re.search(r"FROM\s+(\S+)", sql).group(1), ([], []))
        self.description = [(c,) for c in 컬럼]
        self._결과 = [tuple(r.get(c) for c in 컬럼) if isinstance(r, dict) else tuple(r)
                     for r in 행]
        return self

    def fetchone(self):
        return self._결과[0] if self._결과 else None

    def fetchall(self):
        return list(self._결과)


def _b4(비목, 허용, 사전승인=False, 증빙=(), 비교=None, 적용층=None, 우선규범=None,
        참고_L3=None, *, 금지예시=None, 허용예시=None, 사전승인_조건=None) -> str:
    """병합 결과 dict 를 만들어 B4문장 을 부른다."""
    eff = {"허용": 허용, "사전승인": 사전승인, "증빙": list(증빙), "적용층": 적용층,
           "금지예시": 금지예시, "허용예시": 허용예시,
           "사전승인_조건": [사전승인_조건] if 사전승인_조건 else []}
    return rl.B4문장(비목, eff, 비교, 우선규범, 참고_L3)


def _self_test() -> int:
    실패: list[str] = []

    def eq(이름, got, want):
        if got != want:
            실패.append(f"{이름}: {got!r} != {want!r}")

    # 정규화 대칭: 조사가 붙든 말든 같은 정규형
    eq("조사1", rl._norm("기프티콘을"), rl._norm("기프티콘"))
    eq("조사2", rl._norm("유니폼 제작은"), rl._norm("유니폼제작"))
    eq("조사3", rl._norm("손잡이를"), rl._norm("손잡이"))
    eq("공백", rl._norm("광고 선전비"), rl._norm("광고선전비"))
    eq("한글자보호", rl._norm("회의"), "회의")

    # 금지예시 해부: 예외 단서 판별
    해부 = rl.금지예시_해부
    eq("예외1", 해부("귀금속·보석·원석(사전승인 시 예외)")["무조건"], False)
    eq("예외2", 해부("범용성 사무용 소프트웨어(사전검토 시 예외)")["무조건"], False)
    eq("예외3", 해부("전대차 계약 사무실임차료(공유오피스 월단위 제외)")["무조건"], False)
    eq("예외4", 해부("시제품 제작에 직·간접적으로 활용되는 기구·비품"
                    "(기계장치비로 구매해야 함)")["무조건"], False)
    # 아래는 예외가 아니다
    eq("비예외1", 해부("특허 등록 성공보수(대행 수수료는 가능)")["무조건"], True)
    eq("비예외2", 해부("창업기업 대표자 인건비(현물로만 계상 가능)")["무조건"], True)
    eq("비예외3", 해부("개인 간 중고거래")["무조건"], True)
    eq("본문추출", 해부("귀금속·보석·원석(사전승인 시 예외)")["본문"], "귀금속·보석·원석")
    # 판별은 괄호 안의 (예외|제외|집행 제한) 만 본다. "다만 승인 시 가능" 은 무조건으로 분류되지만
    # 게이트 A 도 같은 판별을 쓰므로 즉시 불가로 잡혀 빈틈이 생기지 않는다.
    eq("판별_괄호안_단서없음", 해부("항목(다만 승인 시 가능)")["무조건"], True)
    eq("판별_예외라는_말", 해부("항목(사전승인 시 예외)")["무조건"], False)

    # 단위 해부
    u = rl._단위해부("원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)")
    eq("단위기본", u["기본"], "원")
    eq("단위분모", u["분모"], ["인", "일"])
    eq("단위한정", u["한정"], "멘토링비")
    # 중첩 괄호: 비중첩 정규식으로 지우면 분모에 산문이 섞인다
    n = rl._단위해부("원/인/일(멘토링비 한정. 별도 한도 2건 — (예비)재창업자별 멘토링비 "
                    "총액 500만원(사업기간 중 합산) · 세무·회계 기장대행 수수료 월 20만원)")
    eq("중첩_기본", n["기본"], "원")
    eq("중첩_분모", n["분모"], ["인", "일"])
    eq("중첩_한정", n["한정"], "멘토링비")

    # 금액비교: 입력이 없으면 추측하지 않는다
    비교 = rl.금액비교
    r금액 = {"한도_유형": "금액", "한도_값": 300000.0, "한도_단위": "원/인/일"}
    eq("금액_미입력", 비교(r금액, None)["초과"], None)
    eq("금액_분모없음", 비교(r금액, {"금액": 500000})["초과"], None)
    eq("금액_초과", 비교(r금액, {"금액": 500000, "인원": 1, "일수": 1})["초과"], True)
    eq("금액_이내", 비교(r금액, {"금액": 200000, "인원": 1, "일수": 1})["초과"], False)
    r비율 = {"한도_유형": "비율", "한도_값": 50.0, "한도_단위": "%(선급금/계약총액)"}
    eq("비율_미입력", 비교(r비율, {"금액": 1000})["초과"], None)
    eq("비율_초과", 비교(r비율, {"선급금": 700, "계약총액": 1000})["초과"], True)
    r개수 = {"한도_유형": "개수", "한도_값": 1.0, "한도_단위": "대/인(PC·노트북·태블릿)"}
    eq("개수_초과", 비교(r개수, {"수량": 3, "인원": 2})["초과"], True)
    eq("개수_이내", 비교(r개수, {"수량": 2, "인원": 2})["초과"], False)
    eq("유형_미상", 비교({"한도_유형": "기타", "한도_값": 1.0, "한도_단위": "원"}, {})["초과"], None)
    # 하위항목 한정: 멘토링비 한도를 사무실임차료에 적용하면 안 된다
    r멘토 = {"한도_유형": "금액", "한도_값": 300000.0,
             "한도_단위": "원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)"}
    eq("한정_불일치", 비교(r멘토, {"금액": 9_000_000, "인원": 1, "일수": 1},
                          하위항목="사무실임차료"), None)
    eq("한정_미상", 비교(r멘토, {"금액": 9_000_000, "인원": 1, "일수": 1})["초과"], None)
    eq("한정_일치", 비교(r멘토, {"금액": 500000, "인원": 1, "일수": 1},
                        하위항목="멘토링비")["초과"], True)

    # B4 문장: 원시 한도값이 나가면 안 된다
    s = _b4("지급수수료", "조건부", True, ["세금계산서"],
            {"초과": True, "사유": "단가가 한도를 초과한다"}, "L2", None)
    if any(t in s for t in ("300000", "300,000", "30만")):
        실패.append(f"B4문장에 원시 한도값 노출: {s}")

    # 실체없음: 금지예시·허용예시·한도·사전승인·증빙 중 하나라도 있으면 False
    eq("실체없음_전부없음", rl._실체없음(None, None, None, False, []), True)
    eq("실체없음_금지예시", rl._실체없음(["ㄱ"], None, None, False, []), False)
    eq("실체없음_허용예시", rl._실체없음(None, ["ㄴ"], None, False, []), False)
    eq("실체없음_한도", rl._실체없음(None, None, {"초과": None, "사유": "x"}, False, []), False)
    eq("실체없음_사전승인", rl._실체없음(None, None, None, True, []), False)
    eq("실체없음_증빙", rl._실체없음(None, None, None, False, ["갑"]), False)

    # 룰은 있는데 구체 내용이 없으면 "조건을 붙여 허용한다" 대신 그 사실을 말한다
    s3 = _b4("여비", "조건부", False, [], None, "L2", None)
    if "조건을 붙여 허용한다" in s3:
        실패.append(f"실체없음인데 확정 문장이 나감: {s3}")
    if "구체적으로 다루는" not in s3:
        실패.append(f"실체없음 문장이 안 나옴: {s3}")

    # 구체 내용이 있으면 개수가 아니라 항목 자체가 들어간다
    s2 = _b4("여비", "불가", False, [], None, "L2", None,
             금지예시=["귀금속", "성공보수"], 허용예시=["시제품용 소재"])
    eq("B4_금지예시_포함", ("귀금속" in s2) and ("성공보수" in s2), True)
    eq("B4_허용예시_포함", "시제품용 소재" in s2, True)
    if "구체적으로 다루는" in s2:
        실패.append(f"금지예시가 있는데 실체없음 문장이 나감: {s2}")

    # 예시 개수 제한: 최대 개수를 넘으면 자르고 "외 N종" 을 붙인다
    많음 = [f"항목{i}" for i in range(12)]
    s캡 = _b4("여비", "불가", False, [], None, "L2", None, 금지예시=많음)
    eq("캡_8개만", all(f"항목{i}" in s캡 for i in range(8)), True)
    eq("캡_9번째_제외", "항목8" not in s캡, True)
    eq("캡_외N종", "외 4종" in s캡, True)
    s무캡 = _b4("여비", "불가", False, [], None, "L2", None, 금지예시=[f"항목{i}" for i in range(3)])
    eq("캡_미만_안잘림", "외" not in s무캡, True)

    # 금지예시는 무조건 항목만 자른다. 무조건 금지는 게이트 A 가 잡고, 예외 단서가 붙은
    # 조건부 항목은 게이트 A 가 잡지 않아 전부 넣는다. 조건부 2 + 무조건 12 → 10개 표시 + "외 4종"
    섞임 = ([f"조건항목{i}(사전승인 시 예외)" for i in range(2)]
            + [f"무조건항목{i}" for i in range(12)])
    s절충 = _b4("여비", "불가", False, [], None, "L2", None, 금지예시=섞임)
    eq("절충_조건부_전부", all(f"조건항목{i}" in s절충 for i in range(2)), True)
    eq("절충_무조건_8개", all(f"무조건항목{i}" in s절충 for i in range(8)), True)
    eq("절충_무조건_9번째_제외", "무조건항목8" not in s절충, True)
    eq("절충_외N종은_무조건만", "외 4종" in s절충, True)
    조건부만 = [f"조건항목{i}(사전승인 시 예외)" for i in range(20)]
    s조건부만 = _b4("여비", "불가", False, [], None, "L2", None, 금지예시=조건부만)
    # "외" 만 찾으면 "예외" 안의 글자에 걸린다. 잘림 표시는 "외 N종" 꼴로 찾는다
    eq("절충_조건부만_안잘림", re.search(r"외 \d+종", s조건부만) is None, True)
    # 허용예시에는 이 기준을 쓰지 않는다(판별 기준이 없다). 8개 제한 그대로
    s허용캡 = _b4("여비", "조건부", False, [], None, "L2", None,
                허용예시=[f"허용{i}(사전승인 시 예외)" for i in range(12)])
    eq("허용예시_절충없음", "외 4종" in s허용캡, True)

    s4 = _b4("여비", "조건부", True, [], None, None, None, 사전승인_조건="전문기관 승인")
    if "전문기관 승인" not in s4:
        실패.append(f"사전승인_조건이 안 들어감: {s4}")
    # 허용 자체가 None 이면 실체없음 판단을 하지 않는다. "없다"와 "있는데 내용 없다"는 다르다
    eq("허용None_문장", "명시되어 있지 않다" in _b4("여비", None, False, [], None, None, None), True)

    # 병합
    def R(layer, 허용, 한도=None, rid=0, ver=True):
        return {"layer": layer, "허용": 허용, "한도_유형": "금액" if 한도 else None,
                "한도_값": 한도, "한도_단위": "원", "사전승인": False,
                "사전승인_조건": [], "증빙": [], "근거": [], "verified": ver,
                "rule_id": rid}
    unspec = [{"우선계층": "L1", "열위계층": "L2", "범위": "unspecified_only"}]
    l1 = {**R("L1", "조건부", None, 1), "금지예시": ["ㄱ"], "증빙": ["갑"]}
    l2 = {**R("L2", "가능", 100.0, 2), "금지예시": ["ㄴ"], "증빙": ["을"], "사전승인": True}

    # unspecified_only 는 허용·우선규범에만 걸린다. 행은 늘 둘 다 넘어간다
    골, 우선 = rl._층병합([l1, l2], unspec)
    eq("층병합_unspec_행", sorted(r["rule_id"] for r in 골), [1, 2])
    eq("층병합_unspec_허용층", 우선, "L1")
    m = rl._엄격병합(골, 허용층우선=우선)
    eq("unspec_허용은L1", m["허용"], "조건부")
    eq("unspec_한도는L2", m["한도_값"], 100.0)          # NULL 이 한도를 지우면 안 된다
    eq("unspec_금지합집합", sorted(m["금지예시"]), ["ㄱ", "ㄴ"])
    eq("unspec_증빙합집합", sorted(m["증빙"]), ["갑", "을"])
    eq("unspec_사전승인OR", m["사전승인"], True)
    eq("unspec_적용층", m["적용층"], "L1+L2")

    # 조항 없음: 허용은 엄격한 쪽, 나머지는 합집합
    골2, 우선2 = rl._층병합([l1, l2], [])
    eq("층병합_조항없음_행", sorted(r["rule_id"] for r in 골2), [1, 2])
    eq("층병합_조항없음_허용층", 우선2, None)
    m2 = rl._엄격병합(골2)
    eq("조항없음_허용엄격", m2["허용"], "조건부")
    eq("조항없음_한도유지", m2["한도_값"], 100.0)
    eq("조항없음_적용층", m2["적용층"], "L1+L2")
    eq("조항없음_한도출처", [r["layer"] for r in m2["한도목록"]], ["L2"])
    eq("단층_적용층", rl._엄격병합([l2])["적용층"], "L2")
    eq("층병합_빈입력", rl._층병합([], [])[0], [])

    # 허용예시도 층 무관 합집합이고, 허용예시만 준 층도 적용층에 들어간다
    l1c = {**l1, "허용예시": ["ㄷ"]}
    l2c = {**l2, "허용예시": ["ㄹ"]}
    mc = rl._엄격병합([l1c, l2c])
    eq("허용예시_합집합", sorted(mc["허용예시"]), ["ㄷ", "ㄹ"])
    eq("허용예시_적용층기여",
       rl._엄격병합([R("L1", "조건부", None, 5), {**R("L2", "가능", None, 6), "허용예시": ["ㄹ"]}])["적용층"],
       "L1+L2")
    l3fx = {**R("L3", "조건부", None, 3), "허용예시": ["ㅁ"], "금지예시": [], "증빙": []}
    eq("오버레이_허용예시_합집합", sorted(rl._오버레이(mc, l3fx)["허용예시"]), ["ㄷ", "ㄹ", "ㅁ"])

    # 사전승인_조건: 리스트화
    eq("리스트화_문자열", rl._조건_리스트화("문장."), ["문장."])
    eq("리스트화_배열", rl._조건_리스트화(["가.", "나."]), ["가.", "나."])
    eq("리스트화_None", rl._조건_리스트화(None), [])
    eq("리스트화_빈문자열", rl._조건_리스트화("   "), [])
    eq("리스트화_글자단위_아님", rl._조건_리스트화("abc") != list("abc"), True)
    # 원소 하나에 든 여러 문장도 문장 단위로 나눈다
    조건1 = rl._엄격병합([{**R("L1", "조건부", None, 10),
                         "사전승인_조건": ["2천만원 초과 시 사전심의. 다만 불가피하면 생략할 수 있다."]}])
    eq("조건_문장분할", 조건1["사전승인_조건"],
       ["2천만원 초과 시 사전심의.", "다만 불가피하면 생략할 수 있다."])
    # 마침표 없는 문장에는 마침표를 붙인다
    eq("조건_마침표", rl._엄격병합([{**R("L1", "조건부", None, 17),
                                    "사전승인_조건": ["제출하여야 한다(제38조④)"]}])["사전승인_조건"],
       ["제출하여야 한다(제38조④)."])
    # 서로 다른 층의 조건은 둘 다 남는다
    합조건 = rl._엄격병합([{**R("L1", "조건부", None, 11), "사전승인_조건": ["L1 조건 문장."]},
                          {**R("L2", "조건부", None, 12), "사전승인_조건": ["L2 조건 문장."]}])
    eq("조건_L1L2_합집합", 합조건["사전승인_조건"], ["L1 조건 문장.", "L2 조건 문장."])
    # 여러 원소에 걸친 같은 문장은 한 번만
    합dup = rl._엄격병합([{**R("L1", "조건부", None, 13),
                          "사전승인_조건": ["같은 문장이다.", "다른 문장이다."]},
                         {**R("L2", "조건부", None, 14), "사전승인_조건": ["같은 문장이다."]}])
    eq("조건_문장중복_한번", 합dup["사전승인_조건"].count("같은 문장이다."), 1)
    eq("조건_빈배열", rl._엄격병합([R("L1", "조건부", None, 15)])["사전승인_조건"], [])
    # 오버레이: base 에 없는 L3 문장만 뒤에 붙는다
    ov_cond = rl._오버레이(dict(mc, 사전승인_조건=["기존 조건 문장."]),
                         {**R("L3", "조건부", None, 16),
                          "사전승인_조건": ["기존 조건 문장.", "L3 새 조건."]})
    eq("오버레이_조건추가", ov_cond["사전승인_조건"], ["기존 조건 문장.", "L3 새 조건."])
    eq("오버레이_조건_B4",
       "(기존 조건 문장. L3 새 조건.)" in rl.B4문장("여비", dict(ov_cond, 사전승인=True), None, None),
       True)

    # 산문 속 추가 한도: "이내"라고 단정하면 틀린 "가능"이 된다
    eq("숨은한도_별도", rl.미파싱한도("원/인/일(멘토링비 한정. 별도 한도 2건 — 총액 500만원 "
                                    "· 세무·회계 기장대행 수수료 월 20만원)") != [], True)
    eq("숨은한도_지역", rl.미파싱한도("원/인/박(국내 숙박비 · 특별시 기준. 광역시 8만원, "
                                    "그 밖의 지역 7만원)") != [], True)
    eq("숨은한도_시간당", rl.미파싱한도("원/인/일(멘토링비 한정, 시간당 10만원 초과 불가)") != [], True)
    # 원천징수 규칙과 비율 분모 이름은 한도가 아니다
    eq("숨은한도_세금아님", rl.미파싱한도("원/인/일(멘토링비 한정, 12만5천원 초과 시 "
                                        "기타소득세 8.8% 공제)"), [])
    eq("숨은한도_분모아님", rl.미파싱한도("%(선급금/계약총액)"), [])
    eq("숨은한도_없음", rl.미파싱한도("대/인(PC·노트북·태블릿)"), [])
    숨김행 = {**R("L2", "조건부", 300000.0, 9), "한도_유형": "금액",
              "한도_단위": "원/인/일(시간당 10만원 초과 불가)"}
    eq("숨은한도_이내를미상으로", rl._한도전수([숨김행], {"금액": 100, "인원": 1, "일수": 1})["초과"], None)
    eq("숨은한도_초과유지", rl._한도전수([숨김행], {"금액": 9e6, "인원": 1, "일수": 1})["초과"], True)

    # 한도 NULL 이 한도 있는 층을 가리면 안 된다
    가림 = rl._엄격병합([R("L1", "조건부", None, 1), R("L2", "조건부", 1.0, 2)])
    eq("한도NULL_안가림", 가림["한도_값"], 1.0)
    eq("한도NULL_적용층", 가림["적용층"], "L1+L2")

    # 단위가 다른 한도는 둘 다 남는다
    개 = {**R("L2", "조건부", 1.0, 3), "한도_유형": "개수", "한도_단위": "대/인"}
    원 = {**R("L2", "조건부", 500000.0, 4), "한도_유형": "금액", "한도_단위": "원/월"}
    다단위 = rl._엄격병합([개, 원])
    eq("다단위_보존", len(다단위["한도목록"]), 2)
    eq("다단위_초과", rl._한도전수(다단위["한도목록"],
                                 {"수량": 3, "인원": 1, "금액": 100, "월수": 1})["초과"], True)
    eq("다단위_이내", rl._한도전수(다단위["한도목록"],
                                 {"수량": 1, "인원": 1, "금액": 100, "월수": 1})["초과"], False)
    eq("다단위_미상", rl._한도전수(다단위["한도목록"], {"수량": 1, "인원": 1})["초과"], None)

    # L3 정규화: corpus.rules 행이 아니다(rule_id 없음, article_id 로 인용)
    e3 = rl._l3정규화({"layer": "L3", "비목": "재료비", "허용": "불가", "참조만": False,
                       "사전승인": True, "한도_값": None, "verified": False,
                       "근거": [{"article_id": 24, "doc_id": "x", "조번호": "제5조"}]})
    eq("L3정규화_rule_id없음", e3["rule_id"], None)
    eq("L3정규화_article_id", e3["article_id"], 24)
    eq("L3정규화_기관없음", "기관id" in e3, False)
    eq("L3정규화_빈배열", (e3["증빙"], e3["금지예시"]), ([], []))
    eq("L3정규화_None", rl._l3정규화(None), None)
    eq("L3더엄격_허용", rl._L3더엄격(e3, {"허용": "조건부", "한도목록": []}), True)
    eq("L3더엄격_관대", rl._L3더엄격({**e3, "허용": "가능"}, {"허용": "조건부", "한도목록": []}), False)
    # 한도는 유형·단위가 같은 것끼리만 비교한다. 1 대/인 과 200000 원은 비교하지 않는다
    l2대 = {**R("L2", "조건부", 1.0, 30), "한도_유형": "개수", "한도_단위": "대/인"}
    l2원 = {**R("L2", "조건부", 300000.0, 31), "한도_유형": "금액", "한도_단위": "원"}
    l3원 = rl._l3정규화({"허용": "조건부", "한도_유형": "금액", "한도_값": 200000, "한도_단위": "원",
                         "verified": False, "근거": []})
    eq("L3더엄격_단위다름_새제약", rl._L3더엄격(l3원, rl._엄격병합([l2대])), True)
    eq("L3더엄격_같은단위_낮음", rl._L3더엄격(l3원, rl._엄격병합([l2대, l2원])), True)
    eq("L3더엄격_같은단위_높음",
       rl._L3더엄격({**l3원, "한도_값": 500000.0}, rl._엄격병합([l2대, l2원])), False)

    # 오버레이(L2>L3 조항이 없는 사업): 엄격한 쪽을 적용한다
    base쪽 = rl._엄격병합([R("L1", "조건부", None, 1), R("L2", "조건부", 300000.0, 2)])
    L3불가 = rl._l3정규화({"허용": "불가", "verified": False,
                           "근거": [{"article_id": 24, "doc_id": "x", "조번호": "제5조"}]})
    ov = rl._오버레이(base쪽, L3불가)
    eq("오버레이_L3불가우선", ov["허용"], "불가")
    eq("오버레이_적용층", ov["적용층"], "L1+L2+L3")
    # L3 가 더 관대하면 base 유지. 오버레이는 완화하지 않는다
    ov2 = rl._오버레이(base쪽, rl._l3정규화({"허용": "가능", "verified": True, "근거": []}))
    eq("오버레이_완화안함", ov2["허용"], "조건부")
    eq("오버레이_적용층불변", ov2["적용층"], base쪽["적용층"])
    # 한도는 더 낮은 쪽(L3 20만 < L2 30만)
    ov3 = rl._오버레이(base쪽, rl._l3정규화({"허용": "조건부", "한도_유형": "금액", "한도_값": 200000,
                                          "한도_단위": "원", "verified": False, "근거": []}))
    eq("오버레이_한도낮은쪽", ov3["한도_값"], 200000.0)
    eq("오버레이_한도출처", [r["layer"] for r in ov3["한도목록"]], ["L3"])
    # 단위가 다른 L3 한도는 최솟값이 아니어도 한도목록에 남아 구속하므로 적용층에 L3 가 들어간다
    ov4 = rl._오버레이(rl._엄격병합([l2대]), {**l3원, "허용": "가능"})
    eq("오버레이_다른단위_L3기여", ov4["적용층"], "L2+L3")
    # 마침표 유무만 다른 같은 문장은 한 번만 남고, 붙는 L3 문장은 마침표로 끝난다
    ov5 = rl._오버레이(dict(mc, 사전승인_조건=["기존 조건 문장."]),
                     {**R("L3", "조건부", None, 32), "사전승인_조건": ["기존 조건 문장", "L3 새 조건"]})
    eq("오버레이_조건_마침표", ov5["사전승인_조건"], ["기존 조건 문장.", "L3 새 조건."])

    # B4 기관 규정 문장은 병합 모드의 실제 결과만 말한다
    eq("L3안내_사업우선_엄격", rl._L3_안내({"모드": "사업우선", "L3더엄격": True}),
       "귀 기관 규정은 더 엄격하나, 본 사업 관리기준이 우선한다.")
    eq("L3안내_사업우선_관대", rl._L3_안내({"모드": "사업우선", "L3더엄격": False}),
       "귀 기관 규정에도 관련 조항이 있으나, 본 사업 관리기준이 우선한다.")
    eq("L3안내_엄격병합_엄격", rl._L3_안내({"모드": "엄격병합", "L3더엄격": True, "L3반영": True}),
       "귀 기관 규정이 더 엄격해 그 기준을 함께 적용했다.")
    eq("L3안내_엄격병합_요건", rl._L3_안내({"모드": "엄격병합", "L3더엄격": False, "L3반영": True}),
       "귀 기관 규정의 요건도 함께 적용했다.")
    eq("L3안내_엄격병합_무반영", rl._L3_안내({"모드": "엄격병합", "L3더엄격": False, "L3반영": False}),
       None)
    _, 참고 = rl._l3_반영(base쪽, L3불가, [])
    eq("l3반영_엄격병합_참고", 참고, {"모드": "엄격병합", "L3더엄격": True, "L3반영": True})
    _, 참고 = rl._l3_반영(base쪽, rl._l3정규화({"허용": "가능", "verified": True, "근거": []}), [])
    eq("l3반영_엄격병합_무반영", 참고, {"모드": "엄격병합", "L3더엄격": False, "L3반영": False})

    # L3 게이팅 네 분기
    eq("게이팅1", rl.l3_게이팅(None)["need_upper"], True)
    eq("게이팅2", rl.l3_게이팅({"허용": "가능", "근거": [{"doc_id": "x", "조번호": "제1조"}],
                              "금지예시": [], "허용예시": [], "증빙": [],
                              "한도_값": None, "사전승인": False})["분기"], "2-참조만")
    eq("게이팅3", rl.l3_게이팅({"허용": "불가", "근거": [], "금지예시": ["a"],
                              "허용예시": [], "증빙": [], "한도_값": None,
                              "사전승인": False})["need_upper"], False)
    eq("게이팅4", rl.l3_게이팅({"허용": "가능", "근거": [], "금지예시": [],
                              "허용예시": ["a"], "증빙": [], "한도_값": None,
                              "사전승인": False})["need_upper"], True)

    _effective_rule_검사(eq, 실패)

    for f in 실패:
        print("  FAIL:", f)
    print(f"self-test {'FAIL ' + str(len(실패)) + '건' if 실패 else 'PASS'}")
    return 1 if 실패 else 0


def _effective_rule_검사(eq, 실패: list[str]) -> None:
    """가짜 커서로 effective_rule·금지적중 의 조회 연결을 확인한다."""
    룰컬럼 = [c.strip() for c in rl._룰컬럼.split(",")]
    우선컬럼 = ["사업명", "우선계층", "열위계층", "범위", "우선규범", "근거", "원문", "해석", "verified"]

    def 룰행(layer, 허용, rid, **kw):
        d = {"rule_id": rid, "layer": layer, "사업명": None if layer == "L1" else "초기창업패키지",
             "비목": "광고선전비", "허용": 허용, "사전승인": False, "사전승인_조건": [],
             "한도_유형": None, "한도_값": None, "한도_단위": None, "증빙": [], "금지예시": [],
             "허용예시": [], "근거": [], "verified": True}
        return {**d, **kw}

    def 표(계통="창업", 우선=()):
        return {"corpus.programs": (["비목계통", "우선규범"], [(계통, None)] if 계통 else []),
                "corpus.rules": (룰컬럼, [룰행("L1", "조건부", 1, 증빙=["갑"]),
                                         룰행("L2", "조건부", 2, 금지예시=["기프티콘"])]),
                "corpus.precedence_rules": (우선컬럼, list(우선))}

    l3불가 = {"허용": "불가", "참조만": False, "사전승인": False, "증빙": ["내부결재 문서"],
              "근거": [{"article_id": 7}], "verified": False}
    L2우선 = [("초기창업패키지", "L2", "L3", "all", None, [], None, None, True)]

    # L2 > L3 (범위 all): L3 는 반영하지 않고 안내 문장만 붙는다
    e = rl.effective_rule(_가짜커서(표(우선=L2우선)), "초기창업패키지", "광고선전비", l3룰=l3불가)
    eq("L2우선_키", e["L2우선L3"], True)
    eq("L2우선_허용", e["허용"], "조건부")
    eq("L2우선_적용층", e["적용층"], "L1+L2")
    eq("L2우선_안내", "본 사업 관리기준이 우선한다" in e["B4문장"], True)
    eq("L2우선_룰들", [r["layer"] for r in e["룰들"]], ["L1", "L2", "L3"])
    eq("결과_키", sorted(e), sorted(["허용", "적용층", "적용층_기여", "증빙", "B4문장", "룰들",
                                    "L2우선L3"]))

    # 조항 없음: 엄격한 쪽(L3 불가)을 적용한다
    e = rl.effective_rule(_가짜커서(표()), "초기창업패키지", "광고선전비", l3룰=l3불가)
    eq("조항없음_키", e["L2우선L3"], False)
    eq("조항없음_허용", e["허용"], "불가")
    eq("조항없음_적용층", e["적용층"], "L1+L2+L3")
    eq("조항없음_안내", "귀 기관 규정이 더 엄격해 그 기준을 함께 적용했다." in e["B4문장"], True)
    eq("조항없음_사업우선문구없음", "본 사업 관리기준이 우선한다" in e["B4문장"], False)

    # R&D 계통(TIPS): L1 을 조회 조건에서 빼고 L3 를 붙이지 않는다
    cur = _가짜커서(표(계통="RND"))
    e = rl.effective_rule(cur, "TIPS", "광고선전비", l3룰=l3불가)
    룰조회 = [a for s, a in cur.기록 if "FROM corpus.rules" in s]
    eq("RND_L1제외", 룰조회 and 룰조회[0][0], False)
    eq("RND_L3없음", "L3" in [r["layer"] for r in e["룰들"]], False)

    # 모르는 사업명: 룰을 조회하지 않고 None
    cur = _가짜커서(표(계통=None))
    eq("모르는사업_None", rl.effective_rule(cur, "없는사업", "광고선전비", l3룰=l3불가), None)
    eq("모르는사업_룰조회없음", any("FROM corpus.rules" in s for s, _ in cur.기록), False)

    # corpus.programs 조회 실패는 삼키지 않는다(판정 파이프라인이 판단불가로 반환한다)
    for 이름, 호출 in (("effective_rule", lambda c: rl.effective_rule(c, "TIPS", "인건비")),
                     ("금지적중", lambda c: rl.금지적중(c, "기프티콘", None, "TIPS", None))):
        try:
            호출(_가짜커서(표(), 실패="FROM corpus.programs"))
            실패.append(f"DB오류_전파_{이름}: 예외 없이 반환됨")
        except RuntimeError:
            pass


# 실제 DB 스모크

_결과_키 = ("허용", "적용층", "적용층_기여", "증빙", "B4문장", "룰들", "L2우선L3")


def _smoke() -> int:
    """실제 DB 로 공개 함수를 사업 × 비목 × (L3 가 있는 기관 + 없음) 조합마다 호출한다.

    --self-test 는 순수 함수만 다뤄 DB 값의 타입 문제를 잡지 못한다. 그래서 DB 에서 읽은 값을
    가공 없이 넘긴다(org_id 는 UUID 객체 그대로). 반환 형태, 허용 enum, 기관 식별자 누출을 본다.
    """
    import l3_load                                              # noqa: PLC0415
    실패: list[str] = []
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT 사업명 FROM corpus.programs WHERE 활성 ORDER BY 사업명")
        사업들 = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT 비목 FROM corpus.item_vocab WHERE 계통='창업' ORDER BY 정렬")
        비목들 = [r[0] for r in cur.fetchall()]
        # L3 조문이 있는 기관만 본다. 나머지 기관은 L3 가 없어 None 과 같은 경로다.
        cur.execute("SELECT DISTINCT org_id FROM tenant.l3_articles")
        orgs = [r[0] for r in cur.fetchall()]

        n = 0
        for 사업 in 사업들 + [None]:
            for 비목 in 비목들:
                for org in orgs + [None]:
                    n += 1
                    try:
                        l3룰 = l3_load.l3룰(cur, org, 비목) if org else None
                        e = rl.effective_rule(cur, 사업, 비목, l3룰=l3룰,
                                              수치={"금액": 100000, "인원": 1, "일수": 1})
                        if e is not None:
                            if e["허용"] not in rl.허용_강도:
                                실패.append(f"{사업}/{비목}/{org}: 허용 '{e['허용']}'")
                            for k in _결과_키:
                                if k not in e:
                                    실패.append(f"{사업}/{비목}/{org}: '{k}' 누락")
                            # 기관 식별자가 반환값에 섞이면 프롬프트로 나간다(TENANT_LEAK)
                            if org and str(org) in json.dumps(e, ensure_ascii=False, default=str):
                                실패.append(f"{사업}/{비목}: 반환값에 org_id 포함")
                        rl.l3_게이팅(l3룰)
                        rl.금지적중(cur, "노트북", "업무용", 사업, 비목)
                        rl.비목확정(cur, "맥북", 사업, 벡터허용=False)
                    except Exception as ex:                     # noqa: BLE001
                        실패.append(f"{사업}/{비목}/{org}: "
                                    f"{type(ex).__name__}: {str(ex).splitlines()[0][:70]}")
    for f in 실패[:20]:
        print("  FAIL:", f)
    print(f"smoke {n}조합 (사업 {len(사업들) + 1} × 비목 {len(비목들)} × org {len(orgs) + 1}) "
          f"{'FAIL ' + str(len(실패)) + '건' if 실패 else 'PASS'}")
    return 1 if 실패 else 0


# 금지목록 매칭 감사

def _audit() -> int:
    """금지목록 매칭 감사: 금지예시 자기적중 · 괄호 내용 분류 · 정답셋 적중 목록.

    무조건 금지예시의 본문을 그대로 품목으로 넣으면 반드시 자기 자신에 적중해야 한다. 자기적중
    수가 무조건 항목 수와 다르면 실패(exit 1)다. 정답셋 적중은 문항에 라벨된 비목으로 금지후보를
    본다. 결과 JSON 은 scripts/_work/rule_lookup_audit.json 에 쓴다.
    """
    괄호분류: dict[str, dict] = {}
    자기일치 = {"전체": 0, "무조건": 0, "예외단서": 0, "자기적중": 0}
    with _connect() as c, c.cursor() as cur:
        cur.execute("SELECT rule_id, 사업명, 비목, unnest(금지예시) FROM corpus.rules")
        for rule_id, 사업명, 비목, 예시 in cur.fetchall():
            h = rl.금지예시_해부(예시)
            자기일치["전체"] += 1
            자기일치["무조건" if h["무조건"] else "예외단서"] += 1
            for m in rl._괄호.findall(rl._nfkc(예시)):
                괄호분류.setdefault(m, {"건수": 0, "예외로_판정": bool(
                    rl._예외단서.search(m) or rl._비목재분류.search(m))})["건수"] += 1
            if h["무조건"] and rl.금지적중(cur, h["본문"], None, 사업명, 비목):
                자기일치["자기적중"] += 1

        정답셋 = {"적중": 0, "조건부근접": 0, "전체": 0, "적중목록": []}
        cur.execute("SELECT gold_id, 사업명, 질문, 정답판정, 비목 FROM eval.golden_set ORDER BY gold_id")
        for gold_id, 사업명, 질문, 정답, 비목 in cur.fetchall():
            정답셋["전체"] += 1
            r = rl.금지후보(cur, 질문, None, 사업명, 비목)
            if r["무조건"]:
                정답셋["적중"] += 1
                정답셋["적중목록"].append({"gold_id": gold_id, "정답": 정답,
                                        "예시": r["무조건"][0]["예시"]})
            elif r["조건부"]:
                정답셋["조건부근접"] += 1

    무조건수 = 자기일치["무조건"]
    print(f"금지예시 {자기일치['전체']}개 — 무조건 {무조건수} / 예외단서 {자기일치['예외단서']}")
    print(f"자기적중(본문을 품목으로 넣으면 적중): {자기일치['자기적중']}/{무조건수}")
    print(f"정답셋 {정답셋['전체']}문항 중 금지적중 {정답셋['적중']} · 조건부근접 {정답셋['조건부근접']}")
    for x in 정답셋["적중목록"]:
        print(f"   #{x['gold_id']} 정답={x['정답']}  ← {x['예시']}")
    _감사_출력.parent.mkdir(parents=True, exist_ok=True)
    _감사_출력.write_text(json.dumps({"자기일치": 자기일치, "정답셋": 정답셋, "괄호_분류": 괄호분류},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {_감사_출력}")
    통과 = 자기일치["자기적중"] == 무조건수
    print(f"audit {'PASS' if 통과 else 'FAIL'}")
    return 0 if 통과 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="rule_lookup 회귀 확인")
    ap.add_argument("--self-test", action="store_true", help="순수 함수 자기검사 (DB 없음)")
    ap.add_argument("--smoke", action="store_true", help="실제 DB 스모크 (반환 형태·예외)")
    ap.add_argument("--audit", action="store_true", help="금지목록 매칭 감사 (자기적중)")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.smoke:
        return _smoke()
    if a.audit:
        return _audit()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
