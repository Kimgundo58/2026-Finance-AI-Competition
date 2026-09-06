# Qwen 모델 선정 — 국제판(Singapore) 기준

방법: 국제판 엔드포인트(`dashscope-intl.aliyuncs.com`)에서 `GET /models` 직접 조회(165개,
텍스트/이미지/음성/임베딩 포함) + Alibaba Cloud 공식 문서(alibabacloud.com/help/en/model-studio)
원문(raw HTML) 대조. 판정 API 호출은 추가로 하지 않음(모델목록 조회만).

## ① json_schema strict — 필수 조건

공식 문서(구조화 출력 페이지) 원문:
> "Supported models: **Only selected qwen-plus models**" — 표에서 JSON Object 모드(대부분 모델 가능)와 대비.
> 세부 목록: "**Qwen3.7-Plus, Qwen3.7-Flash, Qwen3.7-Max, Qwen3.8-Max, Qwen3.8-Flash** series models. More models coming soon."

출처: https://www.alibabacloud.com/help/en/model-studio/qwen-structured-output

🔴 **불일치 발견 — 그대로 보고한다.** 위 목록에 맨 `qwen-plus`(별칭, `qwen-plus-2025-12-01`
동치)는 없다. 그런데 레인 Q 1차 실측(어제)에서 모델명 `"qwen-plus"` 그대로 strict:true 를
5/5 회 지켰다(enum 유출 0). 두 가능성: (a) 문서가 최신화 전이다, (b) `qwen-plus` 별칭이
가리키는 실체가 바뀌는 중이라 우연히 통과했다 — **재현성 미확인**. 문서에 명시된 모델
(`qwen3.7-plus` 등)으로 재검증 없이는 프로덕션 배선 근거로 못 쓴다.

## ② 후보 비교

| 모델ID | strict 지원(문서) | 컨텍스트 | Input $/1M | Output $/1M | 비고 |
|---|---|---|---|---|---|
| qwen3.7-plus | ✓ 문서 명시 | 1M(비사고 fallback 129K, 3rd-party) | $0.4→$1.2(256K↑) *한시 20%할인 표기* | $1.6→$4.8 | Plus 티어, strict 명시 지원 |
| qwen3.8-flash | ✓ 문서 명시 | 1M | $0.15 | $0.47 | 가장 저렴한 strict 지원 모델 |
| qwen3.8-max | ✓ 문서 명시 | 1M | $2 | $6 | 최상위 티어, 비쌈 |
| qwen-plus(별칭) | 문서에 없음 / **실측은 됨(재현 미확인)** | 1M(usable 129K, 3rd-party) | $0.4→$1.2 | $1.2→$3.6(비사고) | 어제 실측 성공, 근거 불충분 |
| qwen-max(별칭) | 문서 JSON Object만 | 1M | $1.6 | $6.4 | strict 미지원(문서) |
| qwen-turbo | 문서 JSON Object만, 단종 예정 | — | $0.05 | $0.2/$0.5 | "더 이상 업데이트 안 함, qwen-flash 로 전환 권장"(공식) |
| qwen3-32b | 목록에 있음(오픈소스), strict 문서 언급 없음 | — | $0.16 | $0.64 | 현재 vLLM 대체 대상. DashScope 로도 서빙되나 strict 근거 없음 |

가격 출처: https://www.alibabacloud.com/help/en/model-studio/model-pricing (raw HTML 직접
파싱, International/Singapore 열 기준. 한시 할인 문구는 조회 시점 기준이라 변동 가능)

컨텍스트: 문서 페이지(getting-started/models)에는 표 형태 상세 수치가 없었음 — 위 1M 수치는
3rd-party 요약(Vercel AI Gateway qwen3.7-flash 페이지, tech-insider.org qwen3.8-flash 기사)
교차 확인. **공식 1차 출처로 확정 못함** — 다만 우리 프롬프트 15,192~21,791자(≈6~10K 토큰
추정)는 어느 후보로도 여유 충분해 선택 기준에서 제외해도 됨.

## ③ 한국어 성능 — 공개 근거 없음

Qwen3.7/3.8 계열의 한국어 벤치마크는 Alibaba 공식 자료에서 찾지 못함. 제3자 논문
(arXiv 2508.10355 "Making Qwen3 Think in Korean with RL", KMMLU-Pro 논문)은 구세대
Qwen3(235B 등) 대상이라 3.7/3.8 세대와 직접 비교 불가. **모른다로 남긴다.**

## qwen-plus/max/turbo/flash 계열 차이 (문서 기준)

- Turbo: 저비용·저성능, 공식적으로 단종 수순(Flash 로 이전 권장)
- Flash: Turbo 후속, 저비용 + 최신 세대(3.8)부터 strict 지원
- Plus: 성능·비용 중간, 3.7 세대부터 strict 지원
- Max: 최상위 추론 성능, 비용 최고, strict 는 3.8-Max 부터

우리 판정 난이도(비목 10종 + code 40종 폐쇄목록 + S번호 인용, 15~22K자 컨텍스트 이해,
전제조건 다단 추론)는 단순 추출이 아니라 **다단 판단**이라 Turbo/Flash 저비용 티어보다
Plus 급 추론력이 안전하다.

## 추천

- **1순위: `qwen3.7-plus`** — strict 문서 명시 지원 + Plus급 추론력. 판정 1건당 2회 호출
  기준 비용은 프롬프트 규모(20K자≈8K토큰) 상 호출당 약 $0.01~0.02 대(256K 이하 티어,
  할인 전제 아님 list price 기준) 수준으로 추정 — **추정치, 실측 필요**.
- **대안: `qwen3.8-flash`** — strict 지원 + 압도적으로 저렴($0.15/$0.47). 정규화 호출(①)처럼
  구조가 단순한 자리에는 우선 적용해볼 만함. 판정 호출(④-b)의 정확도는 검증 전.
- **비추천**: `qwen-plus`(별칭, strict 재현성 미확인), `qwen-max`/`qwen-turbo`(문서상 strict
  미지원).

## 확인 못 한 것 (모른다)
- `qwen-plus` 별칭이 왜 문서 미기재 모델인데도 strict 를 지켰는지 — 원인 불명
- qwen3.7-plus/qwen3.8-flash 정확한 컨텍스트 토큰 수의 1차 공식 출처
- Qwen3.7/3.8 한국어 벤치마크 공식 수치
