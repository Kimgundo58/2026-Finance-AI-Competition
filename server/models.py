# -*- coding: utf-8 -*-
"""🔴 **API 계약 동결본.** 프론트가 보는 필드 이름은 전부 여기서 정해진다.

근거 — `프론트_데이터요구서_0901.md` (화면별 요소표) · `프론트 연동 사양.md` §8 ·
`프로토타입_해부_구현명세.md` §5. 대상 프로토타입은
https://checkumait-user-clean.yeemmin.chatgpt.site/ 이다.

🔴 **이 파일은 조율 세션(Phase 0)이 소유한다. 레인 A·B·C 는 읽기만 한다.**
   필드가 모자라면 직접 고치지 말고 보고할 것. 세 레인이 각자 필드를 늘리면
   프론트가 받는 응답이 세 갈래로 갈린다 — 그게 이 파일을 만든 이유다.

명명 규칙 (프론트 요구서 어휘를 그대로 쓴다):
    지출명   → `제목`         예상 비목 → `확정비목`
    예상금액 → `금액`         예상지출일 → `집행예정일`
    AI 점검 상태 → `판정` (4-way | null). null = 「점검 전」이고 판정이 아니다.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ════════════════════════════════════════════════════════════════════
# 공통
# ════════════════════════════════════════════════════════════════════

판정타입 = Literal["가능", "조건부", "불가", "판단불가"]


class 오류응답(BaseModel):
    """모든 4xx·5xx 가 이 모양이다. 프론트가 판정 화면과 같은 틀로 그린다."""
    오류: str
    상태: int


class F5(BaseModel):
    """판정 후 폐기. 저장하지 않는다 (`서비스 아키텍쳐.md` §6)."""
    친족거래: bool = False
    전직임직원업체: bool = False


# ════════════════════════════════════════════════════════════════════
# ① 정규화 — 화면 8 「새 지출 계획 ① 기본 정보」
#    🔴 2026-09-01 방향 전환: 입력은 자연어가 아니라 **폼**이다.
#       (`프로토타입_해부_구현명세.md` §1). 자연어 경로는 골든셋 재현용으로 병행 유지.
# ════════════════════════════════════════════════════════════════════

class 정규화요청(BaseModel):
    # ── 폼 경로 (프론트 정식 경로) ──
    품목: str | None = None
    금액: float | None = None
    용도: str | None = None
    집행예정일: str | None = None
    거래처: str | None = None          # 저장만. 🔴 판정 미사용 — 라벨에 명시할 것
    추가설명: str | None = None        # 폼이 못 담는 예외 맥락. `용도` 에 합류한다

    # ── 자연어 경로 (골든셋 77문항 재현용. 없애지 말 것) ──
    질문: str | None = Field(default=None, max_length=2000)

    # ── 공통 ──
    사업명: str | None = None
    f5: F5 = Field(default_factory=F5)

    @model_validator(mode="after")
    def _질문_XOR_폼(self):
        폼 = any(v is not None and v != "" for v in (self.품목, self.금액, self.용도))
        자연어 = bool(self.질문 and self.질문.strip())
        if 폼 and 자연어:
            raise ValueError("질문(자연어)과 폼 필드를 동시에 보낼 수 없습니다. 하나만 쓰세요.")
        if not 폼 and not 자연어:
            raise ValueError("품목·금액·용도(폼) 또는 질문(자연어) 중 하나는 필요합니다.")
        if 폼 and not (self.품목 and self.금액 is not None and self.용도):
            raise ValueError("폼 경로는 품목·금액·용도가 모두 필요합니다.")
        return self


class 비목후보(BaseModel):
    비목: str
    신뢰도: float


# 🔴 필드 이름이 클래스 이름을 가리면 pydantic 이 타입을 못 푼다. 별칭으로 참조한다.
_비목후보 = 비목후보


class 정규화응답(BaseModel):
    품목: str | None = None
    금액: float | None = None
    금액_추정여부: bool = False
    용도: str | None = None
    비목후보: list[_비목후보] = Field(default_factory=list)
    # 🔴 하위항목은 폼이 묻지 않는다. 정규화가 품목·용도에서 뽑는 값이다.
    #    지급수수료로 분류될 때만 17종 매칭을 시도하고, 못 정하면 null 로 둔다.
    하위항목: str | None = None
    결제수단: str | None = None
    구매명의: str | None = None
    신청일: str | None = None
    비교견적: str | None = None
    # 폼 값을 문장으로 합성한 것. 저장·검색·표시 전용.
    # ⚠️ 이 문장을 다시 LLM 입력으로 쓰지 않는다 — 필드→문장→필드 왕복은 정보를 잃는다.
    질문원문: str | None = None


# ════════════════════════════════════════════════════════════════════
# ② 판정 — 기존 계약 (동결. 건드리지 말 것)
# ════════════════════════════════════════════════════════════════════

class 판정요청(BaseModel):
    정규화: dict[str, Any] = Field(default_factory=dict)
    확정비목: str | None = None
    사업명: str | None = None
    org_id: str | None = None
    plan_id: int | None = None         # 있으면 판정 후 expense_plans 에 연결한다
    f5: F5 = Field(default_factory=F5)


# ════════════════════════════════════════════════════════════════════
# ④ 할일 「확인필요」 — 화면 11 ⑧ 집행 준비 · 화면 6 ⑥ 다가오는 일정   [레인 B]
#    🔴 체크리스트와 캘린더는 같은 테이블 같은 행이다. due_date 유무로만 갈린다.
# ════════════════════════════════════════════════════════════════════

class 할일(BaseModel):
    task_id: int
    plan_id: int | None = None         # null = 계획과 무관한 사용자 일정
    출처: Literal["ai", "user"] = "ai"
    코드: str | None = None            # corpus.check_items(code). 못 맞추면 null
    구분: Literal["결제전", "결제후", "집행"] = "결제전"
    항목: str
    설명: str | None = None
    due_date: str | None = None        # null 이면 체크리스트에만, 값이 있으면 캘린더에도
    유형: Literal["기타", "계약", "비교견적"] = "기타"
    날짜_사용자수정: bool = False
    상태: Literal["준비필요", "집행예정", "완료"] = "준비필요"
    계획제목: str | None = None        # 캘린더 행의 「연결 지출계획」 표시용


class 할일생성(BaseModel):
    """사용자 직접 추가. 🔴 출처는 서버가 'user' 로 강제한다 — 재판정이 안 건드린다."""
    항목: str
    설명: str | None = None
    구분: Literal["결제전", "결제후", "집행"] = "결제전"
    due_date: str | None = None
    유형: Literal["기타", "계약", "비교견적"] = "기타"


class 할일수정(BaseModel):
    """「확인필요」 토글 · 날짜 지정. 보낸 필드만 바뀐다."""
    상태: Literal["준비필요", "집행예정", "완료"] | None = None
    due_date: str | None = None
    유형: Literal["기타", "계약", "비교견적"] | None = None


class 할일동기화(BaseModel):
    """판정 결과 → plan_tasks 적재. 출처='ai' 행만 만든다."""
    decision_id: int | None = None
    해야할일: list[dict[str, Any]] = Field(default_factory=list)


class 할일동기화응답(BaseModel):
    생성: int = 0
    갱신: int = 0
    보존_user: int = 0                 # 🔴 손대지 않은 사용자 행 수
    보존_날짜수정: int = 0             # 날짜_사용자수정=true 라 덮지 않은 행 수
    코드매칭: int = 0                  # check_items 코드를 맞춘 행 수
    코드미상: int = 0                  # 코드=NULL 로 넣은 행 수


class 할일목록응답(BaseModel):
    건수: int = 0
    항목: list[할일] = Field(default_factory=list)



# 🔴 필드 이름 `할일` 이 클래스 이름을 가리므로 별칭으로 참조한다 (pydantic forward ref).
_할일 = 할일

# ════════════════════════════════════════════════════════════════════
# ③ 지출계획 — 화면 6 홈 · 화면 7 목록 · 화면 11 상세   [레인 A]
# ════════════════════════════════════════════════════════════════════

class 계획생성(BaseModel):
    사업명: str
    제목: str | None = None            # 없으면 정규화 품목으로 서버가 채운다
    품목: str
    금액: float
    용도: str
    집행예정일: str | None = None
    거래처: str | None = None
    추가설명: str | None = None
    확정비목: str | None = None        # 화면 9 에서 사용자가 확정한 값
    정규화: dict[str, Any] = Field(default_factory=dict)
    질문원문: str | None = None        # 폼 합성 문장. 없으면 서버가 합성한다
    org_id: str | None = None


class 계획요약(BaseModel):
    """목록 표 한 행 (`프론트_데이터요구서_0901.md` §화면7-④)."""
    plan_id: int
    제목: str | None = None            # 지출명
    확정비목: str | None = None        # 예상 비목
    금액: float | None = None          # 예상 금액
    판정: 판정타입 | None = None       # AI 점검 상태. null = 「점검 전」
    집행예정일: str | None = None      # 예상 지출일
    updated_at: str | None = None      # 최근 수정일
    사업명: str | None = None
    상태: Literal["draft", "judged"] = "draft"   # 🔴 진행이지 판정이 아니다


class 계획통계(BaseModel):
    """화면 6 ④ 통계 카드 4개 + ③ 전체 현황.

    🔑 프론트 요구서: «통계 전용 API 불필요 — 한 번의 JOIN 이면 다 나온다».
       그래서 목록 응답에 얹어 보낸다. 프론트가 부르기 편한 쪽을 택했다.
    ⚠️ 전체 = 확인필요 + 위험 + 특이사항없음 + 점검전 이다.
    """
    전체: int = 0
    확인필요: int = 0                  # 판정 ∈ (조건부, 판단불가)
    위험: int = 0                      # 판정 = 불가
    특이사항없음: int = 0              # 판정 = 가능
    점검전: int = 0                    # 판정 없음 (draft)
    금액합계: float = 0


class 계획목록응답(BaseModel):
    통계: 계획통계 = Field(default_factory=계획통계)
    건수: int = 0                      # 필터 적용 후 총 건수 (페이지 아님)
    페이지: int = 1
    크기: int = 20
    항목: list[계획요약] = Field(default_factory=list)


class 계획상세(계획요약):
    질문원문: str | None = None
    용도: str | None = None
    거래처: str | None = None
    추가설명: str | None = None
    정규화: dict[str, Any] = Field(default_factory=dict)
    latest_decision_id: int | None = None
    # 판정 전문 (요약·해야할일·인용·전제·신뢰등급·버전스탬프·참조사슬·문의초안)
    판정상세: dict[str, Any] | None = None
    할일: list[_할일] = Field(default_factory=list)
    created_at: str | None = None


# ════════════════════════════════════════════════════════════════════
# ⑤ L3 업로드 — 화면 4 온보딩 ③ 기관 기준 등록   [레인 C]
# ════════════════════════════════════════════════════════════════════

class L3업로드응답(BaseModel):
    doc_id: str
    파일명: str
    확장자: str
    # 🔴 파싱은 이 레인이 하지 않는다. 접수까지만 하고 202 로 돌려준다.
    상태: Literal["파싱대기", "파싱중", "완료", "실패"] = "파싱대기"
    조_건수: int | None = None
    dangling: list[dict[str, Any]] = Field(default_factory=list)
    메시지: str | None = None


# ════════════════════════════════════════════════════════════════════
# ⑥ F 프로필 — 화면 12 내 정보 (기존 계약. 동결)
#    ⚠️ 현물 없음 (2026-08-31). f1 은 2칸, f3 에 「형태」 없음, f4 에 이름칸 없음.
# ════════════════════════════════════════════════════════════════════

class F1(BaseModel):
    정부지원_현금: float = 0
    자기부담_현금: float = 0
    협약시작일: str | None = None
    협약종료일: str | None = None


class F3항(BaseModel):
    비목: str
    재원: Literal["정부지원", "자기부담"]
    거래처: str | None = None
    인력역할: str | None = None
    귀속월: str | None = None
    금액: float = 0


class F4항(BaseModel):
    역할: str                          # 🔴 이름 칸은 만들지 않는다
    고용형태: str | None = None
    타사업참여율: float = 0
    소속기관유형: str | None = None
    겸직: bool = False


class 프로필(BaseModel):
    f1: F1 = Field(default_factory=F1)
    f3: list[F3항] = Field(default_factory=list)
    f4: list[F4항] = Field(default_factory=list)


