# -*- coding: utf-8 -*-
"""LLM 어댑터 — 슬롯 라우팅과 **f_axis 차단 게이트** (`LLM.md` §2 · `서비스 아키텍쳐.md` §6 §11).

                    판정 오케스트레이터
                           |
                LLMAdapter.호출(슬롯, 프롬프트, 스키마)
                           |
        +------------------+------------------+
     LocalVLLM        AnthropicAPI        (예비)
     자체/임대 GPU     비활성 폴백
     (OpenAI 호환)    (F축 슬롯 제외)

🔴 **이 파일의 존재 이유는 게이트 하나다.**
   「F축이 프롬프트에 들어가는 호출은 외부 제공자로 라우팅되면 안 된다」 —
   `서비스 아키텍쳐.md` §11 이 "설계됐으나 미구현" 으로 박아둔 항목이고,
   2026-08-31 실측으로 코드베이스 전체에 `f_axis` 식별자가 없었다.
   지침이 아니라 **코드가 막는다.** 막히면 `tenant.incidents(종류='ROUTING_BLOCK')` 에 남는다.

게이트는 두 겹이다 — 어느 한쪽만으로는 새는 걸 실측으로 확인했다:

  [1겹] 슬롯 선언   `f_axis=True` 슬롯 + 외부 제공자 → 차단
        슬롯 표가 정확하다는 전제다. 오케스트레이터가 슬롯 이름을 잘못 넘기면 뚫린다

  [2겹] 프롬프트 검사  F축 필드명·라벨이 프롬프트에 있으면 슬롯 선언과 무관하게 차단
        요건의 원문이 "F축이 **프롬프트에 들어가는** 호출" 이다. 슬롯 라벨이 아니라
        내용이 기준이므로 이쪽이 오히려 요건에 더 가깝다

무엇이 F축인가 (`RAG.md` §2-3)
  F1 협약(정부지원·자기부담·협약기간) · F2 사업계획서 · F3 집행내역 ·
  F4 인력(역할·고용형태·참여율) · F5 거래처(친족·전직임직원) · F6 기업정보 · F7 수혜이력
  ⚠️ 2026-08-31 현물 제거로 `정부지원_현물`·`자기부담_현금 외 현물`·`f_exec.형태` 는 사라졌다.
     이미 없어진 필드명은 탐지어에 넣지 않는다 — 없는 걸 막는 규칙은 유지비만 든다

실행:
    PYTHONIOENCODING=utf-8 python scripts/adapter.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402

DSN = db.DSN


class RoutingBlocked(RuntimeError):
    """F축 프롬프트가 외부 제공자로 나가려 했다. 판정은 판단불가로 떨어진다."""


class ProviderDisabled(RuntimeError):
    """비활성 폴백을 부르려 했다."""


# ────────────────────────────────────────────────────────────────────
# 슬롯 (LLM.md §1)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class 슬롯:
    이름: str
    설명: str
    f_axis: bool
    기본모델: str
    # 🔴 1500 -> 3000 (2026-09-01 격상). 실전 1차에서 판단불가 5건이 전부 잘림이었다.
    max_tokens: int = 3000
    판정회수: bool = True      # "판정 1건 = LLM 2회" 셈에 들어가는가


슬롯표: dict[str, 슬롯] = {
    # (1) 자연어 → JSON. F5(친족·전직임직원 체크박스)가 프롬프트에 들어간다
    "정규화":   슬롯("정규화", "(1) 자연어 → JSON", True, "Qwen/Qwen3-8B"),
    # F 문서 파싱 — 사업계획서 원문이 통째로 들어간다
    "F문서파싱": 슬롯("F문서파싱", "사업계획서 등 F2 파싱", True, "Qwen/Qwen3-8B", 4000, False),
    # (4)-a 조항별 스크리닝. 채택 자체가 미결(LLM.md §4)이나 슬롯 자리는 잡아둔다
    "스크리닝": 슬롯("스크리닝", "(4)-a 조항별 독립 판정 (채택 미결)", True, "Qwen/Qwen3-8B"),
    # (4)-b 판정 조립. 🔴 B5 에 F 프로필이 들어간다 — 외부 배치 불가의 근원
    "판정조립": 슬롯("판정조립", "(4)-b 최종 판정 + 인용", True, "Qwen/Qwen3-32B-AWQ", 1500),
    # (5) 표현만. 판단불가 경로 전용 — 문의 초안·안내문·A4 diff 요약.
    #     F축이 안 들어가므로 이론상 외부 라우팅이 가능한 유일한 슬롯이다.
    #     그래도 [2겹] 프롬프트 검사는 그대로 탄다
    "문장생성": 슬롯("문장생성", "(5) 표현 생성 (판단불가 경로 전용)", False, "Qwen/Qwen3-8B", 1200, False),
}


# ────────────────────────────────────────────────────────────────────
# [2겹] 프롬프트 F축 탐지
# ────────────────────────────────────────────────────────────────────

# 컬럼명·API 필드명·화면 라벨을 함께 본다. 오케스트레이터가 어느 표기로 넣든 걸리게.
# 한 단어짜리 흔한 말(‘금액’ 등)은 넣지 않는다 — 오탐이 나면 판정이 통째로 막힌다.
_F축_패턴: list[tuple[str, re.Pattern]] = [
    ("F1.협약", re.compile(r"정부지원[_\s]?현금|자기부담[_\s]?현금|협약시작일|협약종료일|"
                           r"협약\s*총액|f_profile|f1\s*[:：]")),
    ("F2.사업계획서", re.compile(r"사업계획서\s*(원문|요약|파싱)|과업범위요약")),
    ("F3.집행내역", re.compile(r"집행내역|f_exec|귀속월|누적\s*집행|재원\s*[:：]\s*(정부지원|자기부담)")),
    ("F4.인력", re.compile(r"f_personnel|타사업참여율|소속기관유형|고용형태|겸직")),
    ("F5.거래처", re.compile(r"친족\s*거래|전직\s*임직원")),
    ("F6.기업정보", re.compile(r"사업자등록번호|법인등록번호")),
    ("F7.수혜이력", re.compile(r"수혜이력|기수혜")),
]


def F축_흔적(텍스트: str) -> list[str]:
    """프롬프트에서 F축 필드의 자취를 찾는다. 값이 아니라 **필드명만** 돌려준다.

    🔴 매칭된 원문을 돌려주지 않는다. 이 반환값이 incidents 에 저장되는데,
       거기에 F축 실데이터가 실리면 사고 기록이 새 유출 경로가 된다.
    """
    return [이름 for 이름, pat in _F축_패턴 if pat.search(텍스트 or "")]


# ────────────────────────────────────────────────────────────────────
# 제공자
# ────────────────────────────────────────────────────────────────────

@dataclass
class 제공자:
    이름: str
    외부: bool          # 우리가 통제하지 않는 하드웨어로 프롬프트가 나가는가
    활성: bool = True

    def 호출(self, 프롬프트: str, 스키마: dict | None, s: 슬롯,
             *, 온도: float, 타임아웃: int) -> tuple[dict | str, dict]:
        raise NotImplementedError


@dataclass
class LocalVLLM(제공자):
    """자체/임대 GPU 의 vLLM (OpenAI 호환).

    ⚠️ RunPod 는 3자 하드웨어지만 **외부 API 제공자가 아니다.** 우리가 띄운 프로세스에
       우리가 HTTP 를 친다. `LLM.md` §1 이 "제출본·데모는 테스트 데이터만 다루므로
       RunPod 서빙 허용, 실서비스의 F축 실데이터 슬롯은 자체 GPU 전제" 로 갈라둔 그대로다.
       그 구분은 `SUDDOE_VLLM_SELF_HOSTED=0` 으로 뒤집을 수 있게 남겨둔다.
    """
    이름: str = "LocalVLLM"
    외부: bool = field(default_factory=lambda:
                       os.environ.get("SUDDOE_VLLM_SELF_HOSTED", "1") != "1")
    활성: bool = True
    url: str = field(default_factory=lambda: os.environ.get("VLLM_URL", "http://localhost:8000"))

    def 호출(self, 프롬프트, 스키마, s, *, 온도, 타임아웃):
        본문 = {
            "model": os.environ.get("VLLM_MODEL", s.기본모델),
            "messages": [{"role": "user", "content": 프롬프트}],
            "temperature": 온도,          # 🔴 0 고정이 기본 — 재현성이 이 도메인의 요건
            "max_tokens": s.max_tokens,
        }
        if 스키마:
            # 🔴 최상위. `extra_body` 로 감싸면 HTTP 직호출에서 조용히 버려진다
            #    (2026-08-31 실측 — judge_run.py 주석 참조). 무음 실패라 더 나쁘다
            본문["guided_json"] = 스키마
        req = urllib.request.Request(
            f"{self.url}/v1/chat/completions",
            data=json.dumps(본문, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"})
        t = time.time()
        with urllib.request.urlopen(req, timeout=타임아웃) as r:
            d = json.loads(r.read().decode())
        내용 = d["choices"][0]["message"]["content"]
        메타 = {"제공자": self.이름, "모델": 본문["model"],
                "지연ms": int((time.time() - t) * 1000),
                "토큰": d.get("usage", {}),
                "종료이유": d["choices"][0].get("finish_reason")}
        return (json.loads(내용) if 스키마 else 내용), 메타


@dataclass
class AnthropicAPI(제공자):
    """비활성 폴백 (`LLM.md` §1).

    재개 조건은 §6 사다리에서 치명 오답 0 제약이 실패할 때뿐이고, 그때도
    **F축 슬롯은 제외**다. 여기서 살아나도 게이트가 f_axis 슬롯을 막는다 —
    그 이중구조가 이 클래스를 지워버리지 않고 남겨두는 이유다.
    """
    이름: str = "AnthropicAPI"
    외부: bool = True
    활성: bool = field(default_factory=lambda: os.environ.get("SUDDOE_ALLOW_EXTERNAL") == "1")

    def 호출(self, 프롬프트, 스키마, s, *, 온도, 타임아웃):
        raise ProviderDisabled(
            "AnthropicAPI 는 비활성 폴백이다. 재개 조건은 LLM.md §6 사다리뿐이고 "
            "그때도 F축 슬롯은 제외다.")


# ────────────────────────────────────────────────────────────────────
# 어댑터
# ────────────────────────────────────────────────────────────────────

def _사고기록(종류: str, 상세: dict, dsn: str | None = None) -> None:
    """`tenant.incidents` 에 남긴다. 🔴 기록 실패가 차단을 삼키면 안 된다."""
    try:
        with db.connect(dsn, connect_timeout=3) as conn:
            conn.execute(
                'INSERT INTO tenant.incidents ("종류", "상세") VALUES (%s, %s::jsonb)',
                (종류, json.dumps(상세, ensure_ascii=False)))
            conn.commit()
    except Exception as e:                                   # noqa: BLE001
        print(f"⚠️ incidents 기록 실패({type(e).__name__}) — 차단 자체는 그대로 유효하다",
              file=sys.stderr)


class LLMAdapter:
    """슬롯 → 제공자 라우팅. 게이트를 통과한 호출만 나간다."""

    def __init__(self, 제공자들: list[제공자] | None = None, *, dsn: str | None = None,
                 기록: bool = True):
        self.제공자들 = 제공자들 or [LocalVLLM(), AnthropicAPI()]
        self.dsn = dsn or DSN
        # 🔴 `기록=False` 는 자가검사 전용이다. 자가검사가 돌 때마다 사고 기록이
        #    쌓이면 tenant.incidents 가 감사 자료로서의 값을 잃는다 — 진짜 사고가
        #    테스트 잡음에 묻힌다. 운영 경로는 항상 True 로 둔다.
        self.기록 = 기록
        self.차단로그: list[dict] = []      # 테스트·서버 진단용 (프로세스 수명)

    # ── 게이트 ──────────────────────────────────────────────────────
    def 검사(self, s: 슬롯, p: 제공자, 프롬프트: str) -> None:
        흔적 = F축_흔적(프롬프트)
        if not p.외부:
            return                                   # 자체 통제 하드웨어 — 통과
        사유 = []
        if s.f_axis:
            사유.append("슬롯선언")                    # [1겹]
        if 흔적:
            사유.append("프롬프트탐지")                 # [2겹]
        if not 사유:
            return
        상세 = {"슬롯": s.이름, "제공자": p.이름, "사유": 사유,
                "f_axis": s.f_axis, "탐지필드": 흔적,
                "설명": "F축이 프롬프트에 들어가는 호출은 외부 제공자로 라우팅될 수 없다"}
        self.차단로그.append(상세)
        if self.기록:
            _사고기록("ROUTING_BLOCK", 상세, self.dsn)
        raise RoutingBlocked(
            f"[f_axis 게이트] 슬롯 '{s.이름}' → 외부 제공자 '{p.이름}' 차단. "
            f"사유={'+'.join(사유)} 탐지필드={흔적 or '없음'}. "
            f"서비스 아키텍쳐.md §6 · LLM.md §2")

    def _고르기(self, s: 슬롯, 강제: str | None) -> 제공자:
        후보 = [p for p in self.제공자들 if 강제 is None or p.이름 == 강제]
        if not 후보:
            raise LookupError(f"제공자 '{강제}' 가 등록돼 있지 않다")
        # 활성 제공자 우선. 강제 지정이면 활성 여부와 무관하게 그것을 고른다 —
        # 🔴 게이트 테스트가 비활성 외부 제공자를 지목해 차단을 확인해야 하기 때문이다
        if 강제:
            return 후보[0]
        살아있는 = [p for p in 후보 if p.활성]
        if not 살아있는:
            raise ProviderDisabled(f"슬롯 '{s.이름}' 에 쓸 활성 제공자가 없다")
        return 살아있는[0]

    # ── 호출 ────────────────────────────────────────────────────────
    def 호출(self, 슬롯이름: str, 프롬프트: str, 스키마: dict | None = None, *,
             온도: float = 0.0, 타임아웃: int = 180,
             제공자강제: str | None = None) -> tuple[dict | str, dict]:
        try:
            s = 슬롯표[슬롯이름]
        except KeyError:
            raise LookupError(
                f"모르는 슬롯 '{슬롯이름}'. 슬롯은 {list(슬롯표)} 뿐이다 — "
                f"function calling 미개방이라 슬롯이 늘어나면 호출 수 예측이 깨진다") from None
        p = self._고르기(s, 제공자강제)
        self.검사(s, p, 프롬프트)                      # 🔴 나가기 전에 반드시
        if not p.활성:
            raise ProviderDisabled(f"제공자 '{p.이름}' 비활성")
        return p.호출(프롬프트, 스키마, s, 온도=온도, 타임아웃=타임아웃)


기본어댑터 = LLMAdapter()


def 호출(슬롯이름: str, 프롬프트: str, 스키마: dict | None = None, **kw):
    """모듈 수준 편의 함수. 오케스트레이터·서버가 이걸 쓴다."""
    return 기본어댑터.호출(슬롯이름, 프롬프트, 스키마, **kw)


# ────────────────────────────────────────────────────────────────────

def _selftest() -> int:
    """게이트가 실제로 막는지 확인한다. GPU·DB 없이 돈다."""
    실패 = 0

    def 확인(설명, 조건):
        nonlocal 실패
        print(("  ✅ " if 조건 else "  🔴 ") + 설명)
        실패 += 0 if 조건 else 1

    print("f_axis 게이트 자가검사")
    기록 = "--record" in sys.argv       # 기본은 DB 에 안 남긴다 (위 주석)
    a = LLMAdapter([LocalVLLM(), AnthropicAPI(활성=True)], 기록=기록)

    print("\n[1겹] 슬롯 선언 — f_axis=True 슬롯을 외부 제공자로")
    for 이름 in ("정규화", "F문서파싱", "스크리닝", "판정조립"):
        try:
            a.호출(이름, "비목이 무엇인가", 제공자강제="AnthropicAPI")
            확인(f"{이름} → AnthropicAPI 차단", False)
        except RoutingBlocked:
            확인(f"{이름} → AnthropicAPI 차단", True)

    print("\n[2겹] 프롬프트 탐지 — f_axis=False 슬롯인데 F축이 섞였을 때")
    샘플 = {
        "F1": "협약총액 5,000만원 중 정부지원_현금 4,000만원",
        "F3": "집행내역: 재원=정부지원, 귀속월 2026-03",
        "F4": "인력 역할 개발자, 타사업참여율 30%, 겸직 여부 N",
        "F5": "친족 거래 여부: 예",
    }
    for 축, 텍스트 in 샘플.items():
        try:
            a.호출("문장생성", 텍스트, 제공자강제="AnthropicAPI")
            확인(f"문장생성 + {축} → 차단", False)
        except RoutingBlocked as e:
            확인(f"문장생성 + {축} → 차단  ({str(e).split('탐지필드=')[-1][:40]})", True)

    print("\n[통과해야 하는 것] F축 없는 문장생성 → 외부 허용")
    try:
        a.호출("문장생성", "다음 문장을 정중하게 다듬어라: 규정을 확인해 주세요",
               제공자강제="AnthropicAPI")
        확인("F축 없는 프롬프트는 게이트를 통과한다", False)
    except RoutingBlocked:
        확인("F축 없는 프롬프트는 게이트를 통과한다 (게이트가 과차단)", False)
    except ProviderDisabled:
        확인("F축 없는 프롬프트는 게이트를 통과한다 (제공자 단계에서 멈춤 = 정상)", True)
    except NotImplementedError:
        확인("F축 없는 프롬프트는 게이트를 통과한다", True)

    print("\n[내부 제공자] f_axis 슬롯 + LocalVLLM → 게이트 통과해야 한다")
    b = LLMAdapter([LocalVLLM()], 기록=기록)
    try:
        b.검사(슬롯표["판정조립"], b.제공자들[0], "협약총액 5,000만원 자기부담_현금 1,000만원")
        확인("판정조립 + F축 + LocalVLLM 통과", True)
    except RoutingBlocked:
        확인("판정조립 + F축 + LocalVLLM 통과", False)

    print("\n[SUDDOE_VLLM_SELF_HOSTED=0] 임대 GPU 를 외부로 선언하면 막혀야 한다")
    c = LLMAdapter([LocalVLLM(외부=True)], 기록=기록)
    try:
        c.검사(슬롯표["판정조립"], c.제공자들[0], "질문")
        확인("자체호스팅 아님 선언 시 f_axis 슬롯 차단", False)
    except RoutingBlocked:
        확인("자체호스팅 아님 선언 시 f_axis 슬롯 차단", True)

    print("\n[모르는 슬롯] function calling 미개방 — 슬롯이 늘면 안 된다")
    try:
        a.호출("아무거나", "x")
        확인("등록되지 않은 슬롯은 거부", False)
    except LookupError:
        확인("등록되지 않은 슬롯은 거부", True)

    확인(f"차단로그 {len(a.차단로그)}건이 메모리에 남았다 (서버 /admin/gate 가 읽는다)",
         len(a.차단로그) >= 8)
    print(f"\nincidents 기록: {'ON (--record)' if 기록 else 'OFF — --record 로 켠다'}")
    print(f"{'✅ 전부 통과' if not 실패 else f'🔴 실패 {실패}건'}")
    return 1 if 실패 else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
    print("슬롯:")
    for k, v in 슬롯표.items():
        print(f"  {k:<10} f_axis={str(v.f_axis):<5} 판정회수={str(v.판정회수):<5} {v.설명}")
