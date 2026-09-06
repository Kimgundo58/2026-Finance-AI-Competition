# 작업기록 — Qwen 모델 선정 (국제판 기준, 레인 Q)

지시자: 중앙 ai-33 (cross-session), 오너 지시

## 1. 무엇을 진행하는가
DashScope 국제판(alibabacloud.com, 싱가포르)에서 실제로 쓸 수 있는 Qwen 모델을 조사해
① json_schema strict 지원 여부 ② 컨텍스트 길이 ③ 입출력 단가 ④ 한국어 성능 공개 근거를
문서 기준으로 확인하고, 우리 판정(프롬프트 15~22K자, 출력 JSON, 호출 2회/건)에 맞는
추천 1개 + 대안 1개를 정한다. 본토판(aliyun.com)이 아니라 국제판 기준이어야 한다.

## 2. 어떻게 진행했는가
- Alibaba Cloud Model Studio(국제판) 공식 문서를 WebFetch/WebSearch 로 직접 확인
- 가능하면 `GET /models`(OpenAI 호환) 로 국제판 엔드포인트에서 실제 모델 목록 조회해 대조
- 출처 URL 없는 수치는 쓰지 않음. 모르면 "모른다"로 명기
- 산출: `scratchpad/Q2_Qwen모델선정_국제판.md` (표+추천, 100줄 이내, 출처 URL 병기)

## 3. 결과
- 국제판 `/models` 165개 확인(텍스트/이미지/음성/임베딩 포함)
- 공식 문서(raw HTML 직접 파싱) 기준 **json_schema strict 지원은 `qwen3.7-plus/flash/max`,
  `qwen3.8-max/flash` 뿐** — 어제 실측에 쓴 맨 `qwen-plus` 별칭은 이 목록에 없음 (불일치,
  재현성 미확인 상태로 보고)
- 가격표(raw HTML)에서 qwen-max/plus/turbo/flash 및 qwen3.7/3.8 계열 국제판 단가 확보
- 한국어 성능 공식 근거 없음 확인(제3자 논문은 구세대 Qwen3 대상이라 직접 비교 불가)
- 추천: 1순위 `qwen3.7-plus`(strict 명시 지원 + Plus급 추론), 대안 `qwen3.8-flash`(strict
  지원 + 최저가)
- 산출: `scratchpad/Q2_Qwen모델선정_국제판.md` (71줄, 표+출처 URL)
- 중앙 ai-33 에 세 줄 보고 완료
