# -*- coding: utf-8 -*-
"""API 요청·응답 스키마(pydantic). 프론트가 보는 필드 이름은 모두 여기서 정한다.

여러 라우터가 같이 쓰는 계약이라 필드를 추가할 때는 프론트와 먼저 합의한다.

필드 이름은 프론트 요구사항의 용어를 따른다:
    지출명   → 제목           예상 비목 → 확정비목
    예상금액 → 금액           예상지출일 → 집행예정일
    AI 점검 상태 → 판정 (가능·조건부·불가·판단불가 또는 null). null 은 '점검 전' 이며 판정 값이 아니다.

이름을 바꾼 필드는 한 릴리스 동안 요청에서 이전 이름도 받고(validation_alias), 응답에는 이전 이름의
읽기 전용 필드(computed_field)를 함께 내보낸다. 해당 줄에는 호환 표식 주석을 단다.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, computed_field, model_validator

# --- 공통 ---

판정타입 = Literal["가능", "조건부", "불가", "판단불가"]


class 특수관계(BaseModel):
    """특수관계 거래 여부. 판정 요청에만 쓰고 저장하지 않는다."""
    친족거래: bool = False
    전직임직원업체: bool = False


# 필드 이름이 클래스 이름을 가리면 pydantic 이 타입을 해석하지 못한다. 별칭으로 참조한다.
_특수관계 = 특수관계


# --- 정규화 (POST /api/normalize) ---
# 입력은 폼 경로가 기본이다. 자연어 경로는 골든셋 평가를 재현하려고 함께 유지한다.

class 정규화요청(BaseModel):
    # 폼 경로 (프론트 기본 경로)
    품목: str | None = None
    금액: float | None = None
    용도: str | None = None
    집행예정일: str | None = None
    거래처: str | None = None          # 저장만 하고 판정에는 쓰지 않는다
    추가설명: str | None = None        # 폼 필드에 없는 예외 상황 설명. 용도 뒤에 붙여 LLM 에 넘긴다

    # 자연어 경로 (골든셋 질문이 이 경로로 들어온다. 지우지 않는다)
    질문: str | None = Field(default=None, max_length=2000)

    # 공통
    사업명: str | None = None
    특수관계: _특수관계 = Field(default_factory=_특수관계,
                             validation_alias=AliasChoices("특수관계", "f5"))  # 구 이름 호환(제거 예정: 다음 릴리스)

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


class 답변항목(BaseModel):
    """/api/judge 가 받는 심층질문 답 한 건."""
    code: str
    값: Any


# --- 판정 (POST /api/judge) ---

class 판정요청(BaseModel):
    정규화: dict[str, Any] = Field(default_factory=dict)
    확정비목: str | None = None
    사업명: str | None = None
    org_id: str | None = None
    plan_id: int | None = None         # 있으면 판정 후 expense_plans 에 연결한다
    특수관계: _특수관계 = Field(default_factory=_특수관계,
                             validation_alias=AliasChoices("특수관계", "f5"))  # 구 이름 호환(제거 예정: 다음 릴리스)
    # 심층질문 답. 비어 있으면 orchestrate.판정() 이 사용자 답 없이 프로필값을 계산한다.
    답변: list[답변항목] = Field(default_factory=list)


# --- 할일 (plan_tasks) ---
# 체크리스트와 캘린더는 같은 테이블의 같은 행이다. due_date 가 있으면 캘린더에도 표시한다.

class 할일(BaseModel):
    task_id: int
    plan_id: int | None = None         # null 이면 계획과 무관한 사용자 일정
    출처: Literal["ai", "user"] = "ai"
    # corpus.check_items.code. 매칭하지 못하면 null. DB 컬럼 plan_tasks.코드 와 서버가 매핑한다
    code: str | None = Field(default=None, validation_alias=AliasChoices("code", "코드"))  # 구 이름 호환(제거 예정: 다음 릴리스)
    구분: Literal["결제전", "결제후", "집행"] = "결제전"
    항목: str
    설명: str | None = None
    due_date: str | None = None        # null 이면 체크리스트에만, 값이 있으면 캘린더에도 표시
    유형: Literal["기타", "계약", "비교견적"] = "기타"
    날짜_사용자수정: bool = False
    상태: Literal["준비필요", "집행예정", "완료"] = "준비필요"
    계획제목: str | None = None        # 캘린더 행에 연결된 지출계획 제목

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 코드(self) -> str | None:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.code


class 할일생성(BaseModel):
    """사용자가 직접 추가하는 할일. 출처는 서버가 'user' 로 고정한다(재판정해도 바뀌지 않는다)."""
    항목: str
    설명: str | None = None
    구분: Literal["결제전", "결제후", "집행"] = "결제전"
    due_date: str | None = None
    유형: Literal["기타", "계약", "비교견적"] = "기타"


class 할일수정(BaseModel):
    """할일 상태 변경·날짜 지정. 보낸 필드만 바뀐다."""
    상태: Literal["준비필요", "집행예정", "완료"] | None = None
    due_date: str | None = None
    유형: Literal["기타", "계약", "비교견적"] | None = None


class 할일동기화(BaseModel):
    """판정 결과의 해야할일을 plan_tasks 에 반영하는 요청. 출처='ai' 행만 만든다."""
    decision_id: int | None = None
    해야할일: list[dict[str, Any]] = Field(default_factory=list)


class 할일동기화응답(BaseModel):
    생성: int = 0
    갱신: int = 0
    보존_사용자: int = 0               # 건드리지 않은 사용자 행 수
    보존_날짜수정: int = 0             # 날짜_사용자수정=true 라 덮어쓰지 않은 행 수
    코드매칭: int = 0                  # check_items code 를 매칭한 행 수
    코드미매칭: int = 0                # code 를 매칭하지 못해 NULL 로 넣은 행 수

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 보존_user(self) -> int:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.보존_사용자

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 코드미상(self) -> int:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.코드미매칭


class 할일목록응답(BaseModel):
    건수: int = 0
    항목: list[할일] = Field(default_factory=list)



# 필드 이름 `할일` 이 클래스 이름을 가리므로 별칭으로 참조한다(pydantic forward ref)
_할일 = 할일

# --- 지출계획 (expense_plans) ---

class 계획생성(BaseModel):
    사업명: str
    제목: str | None = None            # 없으면 서버가 정규화 품목으로 채운다
    품목: str
    금액: float
    용도: str
    집행예정일: str | None = None
    거래처: str | None = None
    추가설명: str | None = None
    확정비목: str | None = None        # 사용자가 확정한 비목
    정규화: dict[str, Any] = Field(default_factory=dict)
    질문원문: str | None = None        # 폼 값으로 합성한 문장. 없으면 서버가 합성한다
    org_id: str | None = None


class 계획요약(BaseModel):
    """계획 목록 표의 한 행."""
    plan_id: int
    제목: str | None = None            # 지출명
    확정비목: str | None = None        # 예상 비목
    금액: float | None = None          # 예상 금액
    판정: 판정타입 | None = None       # AI 점검 상태. null 은 '점검 전'
    집행예정일: str | None = None      # 예상 지출일
    updated_at: str | None = None      # 최근 수정일
    사업명: str | None = None
    상태: Literal["draft", "judged"] = "draft"   # 진행 상태이지 판정 값이 아니다


class 계획통계(BaseModel):
    """홈 화면 통계 카드 값. 목록 응답에 포함해 보낸다(통계 전용 API 없음).

    전체 = 확인필요 + 위험 + 특이사항없음 + 점검전.
    """
    전체: int = 0
    확인필요: int = 0                  # 판정 ∈ (조건부, 판단불가)
    위험: int = 0                      # 판정 = 불가
    특이사항없음: int = 0              # 판정 = 가능
    점검전: int = 0                    # 판정 없음 (draft)
    금액합계: float = 0


class 계획목록응답(BaseModel):
    통계: 계획통계 = Field(default_factory=계획통계)
    건수: int = 0                      # 필터 적용 후 총 건수 (페이지 크기가 아니다)
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
    # 판정 전문 (요약·해야할일·인용·전제·신뢰등급·규정버전·참조관계·검증사유·문의초안)
    판정상세: dict[str, Any] | None = None
    할일: list[_할일] = Field(default_factory=list)
    created_at: str | None = None


# --- 기관 자체규정(L3) 업로드 ---

class L3업로드응답(BaseModel):
    doc_id: str
    파일명: str
    확장자: str
    # 업로드 요청은 접수까지만 하고 202 를 돌려준다. 파싱은 백그라운드 작업이 한다.
    상태: Literal["파싱대기", "파싱중", "완료", "실패"] = "파싱대기"
    조_건수: int | None = None
    # 등록된 규정에서 찾지 못한 상위 규정 참조. 업로드 실패 안내에 쓴다
    미해결참조: list[dict[str, Any]] = Field(default_factory=list)
    메시지: str | None = None
    # 상태가 '완료' 여도 warn(조문은 추출했지만 품질이 낮음)과 pass 를 프론트가 구분할 수 있게 한다
    파싱품질: Literal["pending", "pass", "warn", "fail"] | None = None
    # 실패 분류 코드(스캔본·보호문서·조문없음·중단 등). 실패가 아니면 null. 안내 문구는 메시지에 있다
    실패사유: str | None = None
    # 이 기관이 지금 판정에 쓰는 L3 문서 이름(이 문서일 수도 있다). 없으면 null — 상위 규정만으로 판정한다
    적용중_문서: str | None = None

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def dangling(self) -> list[dict[str, Any]]:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.미해결참조


class L3현재문서(BaseModel):
    """org 의 active L3 문서 한 건(적용 중·처리 중·최근 실패). superseded 문서는 뺀다.

    파싱품질이 pending 인 채로 오래된 문서(파싱이 끊김)는 fail 로 내보낸다(server/routes_l3.py)."""
    doc_id: str
    원본파일명: str
    version: str | None = None
    시행일: str | None = None
    # created_at 값. 시행일이 대부분 NULL 이라 화면에는 이 값을 표시한다
    등록일: str | None = None
    파싱품질: Literal["pending", "pass", "warn", "fail"] | None = None
    조_건수: int = 0


class L3현재문서목록응답(BaseModel):
    """GET /api/l3/current 응답. org_id 가 없으면(게스트) 빈 목록이고, 다른 org 의 문서는 포함하지 않는다."""
    문서: list[L3현재문서] = Field(default_factory=list)


# --- 사업 목록 (GET /api/programs) ---

class 우선순위규칙(BaseModel):
    """corpus.precedence_rules 한 행. L1/L2/L3 중 어느 계층이 우선하는지."""
    우선계층: str
    후순위계층: str
    범위: str | None = None
    우선규범: str | None = None
    해석: str | None = None

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 열위계층(self) -> str:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.후순위계층


class 사업정보(BaseModel):
    사업명: str
    별칭: list[str] = Field(default_factory=list)
    비목체계: str | None = None        # 창업 | RND
    트랙범위: str | None = None
    # 프론트가 우선순위 규칙을 하드코딩하지 않도록 내려준다(추가 필드라 기존 계약은 그대로다)
    우선순위규칙목록: list[우선순위규칙] = Field(default_factory=list)

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 비목계통(self) -> str | None:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.비목체계


class 사업목록응답(BaseModel):
    사업: list[사업정보] = Field(default_factory=list)
    비고: str | None = None


# --- 기업 프로필 (GET·PUT /api/profile) ---
# 현물 항목은 없다. 협약은 현금 2개 필드이고 집행내역에는 형태(현금/현물) 필드, 참여인력에는
# 이름 필드가 없다.

class 협약정보(BaseModel):
    정부지원_현금: float = 0
    자기부담_현금: float = 0
    협약시작일: str | None = None
    협약종료일: str | None = None


class 집행내역항목(BaseModel):
    비목: str
    재원: Literal["정부지원", "자기부담"]
    거래처: str | None = None
    인력역할: str | None = None
    귀속월: str | None = None
    금액: float = 0


class 참여인력항목(BaseModel):
    역할: str                          # 실명은 받지 않는다
    고용형태: str | None = None
    타사업참여율: float = 0
    소속기관유형: str | None = None
    겸직: bool = False


class 프로필(BaseModel):
    협약: 협약정보 = Field(default_factory=협약정보,
                       validation_alias=AliasChoices("협약", "f1"))  # 구 이름 호환(제거 예정: 다음 릴리스)
    집행내역: list[집행내역항목] = Field(default_factory=list,
                                 validation_alias=AliasChoices("집행내역", "f3"))  # 구 이름 호환(제거 예정: 다음 릴리스)
    참여인력: list[참여인력항목] = Field(default_factory=list,
                                 validation_alias=AliasChoices("참여인력", "f4"))  # 구 이름 호환(제거 예정: 다음 릴리스)
