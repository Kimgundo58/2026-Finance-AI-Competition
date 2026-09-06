# D — GCP Document AI 판독 통합 설계 (레인 D, 2026-09-06)

## 1. 만든 것

| 파일 | 내용 |
|---|---|
| `scripts/docai_extract.py` (신규) | `extract(path) -> (본문, 페이지오프셋)` — `vlm_extract.py` 와 **같은 계약**. 페이지 판정·렌더링(`vlm_extract.페이지별_글자수`·`_렌더`)과 표 직렬화(`table_splice._마크다운_표`)를 재사용해 새 규칙을 안 만들었다 |
| `scripts/l3_parse.py` (수정, 15줄) | `SUDDOE_L3_판독기=vlm\|docai`(기본 `vlm`)로 import 를 고르는 분기만 추가. 그 아래 파싱 로직은 한 글자도 안 건드렸다 |
| `requirements-api.txt` (수정) | `google-cloud-documentai==3.15.0` 추가(`pip index versions` 로 확인한 최신판, 2026-09-06). 겸사겸사 `pypdfium2==5.12.1` 도 추가함 — 원래 없었던 게 이번에 눈에 띔(vlm_extract.py 가 이미 쓰고 있었는데 requirements 에 빠져 있었다. 이 레인 소관은 아니지만 같은 파일이라 같이 고쳤다) |

계약 준수: `docai_extract.extract(path)` 시그니처·반환형이 `vlm_extract.extract(path)` 와 동일 —
`l3_parse.py` 의 호출부(`_vlm_시도()` 이하)는 손대지 않았다.

## 2. 자가검사 결과 (API 호출 없음)

```
PYTHONIOENCODING=utf-8 python scripts/docai_extract.py --selftest
✅ self-test 전건 통과 (API·SDK 호출 없음)
```
검사한 것: 게이트 차단(`SUDDOE_ALLOW_DOCAI` 없으면 예외) · 설정누락 예외 · 표→파이프마크다운
변환(가짜 Document 객체로) · 저신뢰 문단→`[판독불가]` 치환 · 표 구간과 겹치는 저신뢰 문단은
표 치환이 우선 · `vlm_extract.MIN_CHARS_PER_PAGE`(50) 공유 확인.

## 3. 🔴 정확도 — 못 잰다 (숨기지 않는다)

- `documentai.googleapis.com` 비활성 상태다. 이 세션은 **gcloud 쓰기 금지**라 켤 수 없다
- 설령 켜져 있어도 프로세서(Form Parser 등)가 없어서 실제 API 호출 자체가 안 된다
- 그래서 "인공 스캔본으로 vlm vs docai 비교" 를 **실행할 수 없었다** — 추정치를 적지 않는다
- 대신 자가검사(2절)로 **코드 계약·표 직렬화·저신뢰 마커 로직**만 검증했다. 이건 "정확도"가
  아니라 "코드가 API 응답을 올바르게 가공하는가" 다 — 다른 층위의 확인이라는 걸 분명히 한다
- 활성화 후 central·오너가 실제로 비교하려면: 기존 규정집 PDF 한 페이지를
  `vlm_extract._렌더(path, N)` 로 PNG 렌더 → 두 판독기에 각각 먹이고 `표_판독_불확실`/
  `판독불가마커수`, 그리고 **표 칸 수를 손으로 세어** 대조. 이 절차 자체는 이미
  `docai_extract.py` 의 함수들로 짤 수 있으니 API 만 열리면 바로 돌릴 수 있다

## 4. 활성화 절차 (명령만 — 실행 안 함)

```bash
# ① API 활성화
gcloud services enable documentai.googleapis.com --project=project-35d896d7-67d7-4b2a-a8f

# ② 프로세서 생성 — 표를 살리는 걸 원하면 FORM_PARSER_PROCESSOR (표를 Table 객체로 준다)
#    콘솔(권장, 프로세서 생성은 API 로도 되지만 리전 확인이 콘솔이 편하다):
#    https://console.cloud.google.com/ai/document-ai/processors?project=project-35d896d7-67d7-4b2a-a8f
#    또는 CLI:
gcloud documentai processors create \
  --project=project-35d896d7-67d7-4b2a-a8f \
  --location=us \
  --display-name="suddoe-l3-form-parser" \
  --type=FORM_PARSER_PROCESSOR

# ③ 생성된 processor 이름(projects/.../locations/us/processors/<ID>)에서 <ID> 를 뽑아 env 로
```

🔴 **리전 주의** — Document AI 프로세서는 리전이 고정이고 한국 리전(asia-northeast3 등)은
지원 프로세서가 제한적이다(확인 안 됨 — 이 세션에서 리전별 지원 프로세서 목록을 조회할
방법이 없었다). `us`(멀티리전) 이 가장 무난하다고 알고 있으나 **이것도 실측이 아니라
일반 지식이다** — 활성화 전에 콘솔에서 리전별 지원 프로세서 목록을 central·오너가 직접
확인해야 한다. 데이터가 GCP 밖으로 안 나간다는 전제(3)-①에는 영향 없다(같은 GCP 안,
리전만 다르다).

## 5. 서비스계정 권한 · 인증

| 실행 위치 | 인증 방식 | 필요 역할(role) |
|---|---|---|
| Cloud Run (운영) | **ADC**(Application Default Credentials) — Cloud Run 런타임 서비스계정이 메타데이터 서버로 자동 발급. 키 파일 불필요 | 런타임 SA 에 `roles/documentai.apiUser` (문서 처리 호출만, 프로세서 생성·삭제 불가 — 최소권한) |
| 로컬(이 스크립트 CLI 로 테스트) | `gcloud auth application-default login` 로 로컬 ADC 발급, 또는 서비스계정 키 JSON(`GOOGLE_APPLICATION_CREDENTIALS`) | 같음(`documentai.apiUser`). 프로세서를 직접 만들 사람은 `roles/documentai.editor` 필요(1회성, 배포 계정에만) |

`docai_extract.py` 는 `documentai_v1.DocumentProcessorServiceClient()` 를 인자 없이 생성한다
— google-cloud 클라이언트 라이브러리가 ADC 를 자동으로 찾는다(Cloud Run 이면 메타데이터 서버,
로컬이면 `GOOGLE_APPLICATION_CREDENTIALS` 또는 `gcloud auth application-default login` 캐시).
코드에 키를 하드코딩하지 않는다.

## 6. 비용 — 정확한 단가는 «확정 안 함»

Document AI 는 페이지 단위 과금이고 프로세서 종류(Form Parser vs OCR vs Layout Parser)에
따라 단가가 다르다. 🔴 **이 세션은 공식 가격표를 실시간으로 조회할 방법이 없어 숫자를
적으면 추정치가 확정값처럼 보일 위험이 있다 — central·오너가 활성화 전에
https://cloud.google.com/document-ai/pricing 를 직접 확인해야 한다.** 참고할 점만:
- 무료 등급이 매달 첫 N페이지(정확한 N 은 확인 필요)까지 있는 편이다
- Anthropic 비전 API(현행 vlm_extract, 페이지당 이미지 1장 + 프롬프트)와 나란히 비교하려면
  "페이지당 단가 × 예상 스캔 페이지 수"로 계산해야 하는데, 예상 스캔 페이지 수 자체도
  실측이 없다(§7 참고) — 두 겹의 미확정이라 지금 결론 내지 않는다

## 7. 참고 — 판독 대상이 지금 0건이다 (ai-33 이 잰 값, 재확인 안 함)

`_l3_업로드` 의 pdf 는 14바이트 빈 파일, `_vlm_캐시` 디렉터리는 없다. 즉 VLM 경로도 아직
실전에서 한 번도 안 돌았고, Document AI 로 바꿔도 당장 돌려볼 실제 스캔 PDF가 없다.
비교·비용 추정 모두 **실물 샘플이 생긴 뒤**에야 의미가 있다.

## 8. 남은 판단 (central·오너)

1. 리전(§4) 확정 — 콘솔에서 지원 프로세서 확인 후
2. 프로세서 종류 확정 — Form Parser(표를 Table 객체로, 안정적) vs Layout Parser(최신, RAG 친화적 블록 구조지만 이 세션이 스펙을 직접 확인 못 함 — 안다고 적지 않는다)
3. `SUDDOE_ALLOW_DOCAI` 를 언제 켤지 — 실물 스캔 PDF 가 생긴 뒤 vlm 과 나란히 비교하는 걸 권장(§3 절차)
4. `SUDDOE_L3_판독기` 기본값을 언제 `docai` 로 바꿀지 — 지금은 `vlm` 유지(기존 동작 안 바뀜, 이 레인은 «갈아끼울 수 있게만» 만든 것)
