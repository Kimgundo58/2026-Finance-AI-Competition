<div align="center">

# 💸 써도돼요

**창업지원금 지출비 사전승인을 근거와 함께 판정하는 AI 서비스**

[![Python](https://img.shields.io/badge/Python-3.10-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector)
[![Cloud Run](https://img.shields.io/badge/Cloud%20Run-GCP-4285F4?style=flat-square&logo=googlecloud&logoColor=white)](https://cloud.google.com/run)
[![Qwen](https://img.shields.io/badge/LLM-Qwen3.7--plus-FF6A00?style=flat-square)](https://www.alibabacloud.com/en/product/modelstudio)

[🚀 데모 실행하기](https://2026-finance-ai-competition-fronten.vercel.app/) · [🩺 실서버 헬스체크](https://suddoe-api-1081277785480.asia-northeast3.run.app/api/health) · [📘 문서 지도](docs/README.md)

</div>

---

## 📋 목차

1. [프로젝트에 대한 정보](#1-프로젝트에-대한-정보)
2. [시작 가이드](#2-시작-가이드)
3. [기술 스택](#3-기술-스택)
4. [주요 기능](#4-주요-기능)
5. [성능](#5-성능)
6. [프로젝트 구조](#6-프로젝트-구조)
7. [데이터와 활용 한계](#7-데이터와-활용-한계)

---

<a id="1-프로젝트에-대한-정보"></a>
## 1. 📌 프로젝트에 대한 정보

### 프로젝트 소개

창업기업이 "이거 사도 돼요?"를 물으면 중소벤처기업부·창업진흥원·주관기관 규정을 찾아
**가능 · 조건부 · 불가 · 판단불가** 네 갈래로 답한다. 근거는 LLM이 새로 쓰지 않고, 검색·룰
조회로 찾은 원문 문장에 번호(S01…)를 매겨 그 번호만 인용하게 한다. 계산과 효력 판단은
전부 코드가 하고 LLM은 정규화와 최종 조립만 맡아, 판정 1건이 LLM 호출 2~3회로 고정된다.

> 이 판정은 참고용이며 **최종 승인 권한은 주관기관·전문기관에 있다.** 서비스는
> 사전에 근거를 정리해 담당자에게 물어볼 시점과 내용을 좁혀주는 역할이다.

| 구분 | 내용 |
|---|---|
| 프로젝트명 | 써도돼요 |
| 대상 사업 | 창업진흥원 8종(예비창업패키지·초기창업패키지·창업도약패키지·창업중심대학·재도전성공패키지·초격차 스타트업 프로젝트·TIPS·모두의창업) |
| 판정 결과 | 가능 · 조건부 · 불가 · 판단불가 |
| 배포 주소 | [Vercel 데모](https://2026-finance-ai-competition-fronten.vercel.app/) |
| 구성 | 백엔드 FastAPI on Cloud Run(asia-northeast3) + Cloud SQL(PostgreSQL 17 + pgvector), 프론트 Vercel(별도 레포) |
| 저장소 | 이 레포(백엔드·수집·평가) |

### 🖥️ 시연 홈화면

<p align="center">
  <a href="https://2026-finance-ai-competition-fronten.vercel.app/">
    <img src="docs/screenshots/홈화면.png" alt="써도돼요 시연 홈화면" width="860">
  </a>
</p>

<p align="center"><a href="https://2026-finance-ai-competition-fronten.vercel.app/"><b>브라우저에서 데모 열기</b></a></p>

---

<a id="2-시작-가이드"></a>
## 2. 🚀 시작 가이드

### 로컬 실행

```powershell
# 1) DB
cd db
docker compose up -d
# http://localhost:8081 (pgweb, 로그인 없음) — v_적재현황 이 첫 확인 지점

# 2) 환경변수
copy ..\.env.example ..\.env
# 값은 채워 넣는다. 이름과 뜻은 .env.example 주석 참조

# 3) 서버
cd ..
uvicorn server.main:app --reload
```

### 목 모드

DB·LLM 없이 API 형태만 확인할 때는 `SUDDOE_MOCK=1`. 기본값이 목업이라 별도 설정 없이도
`/api/health`가 200을 낸다. 실 판정을 태우려면 `SUDDOE_MOCK=0`이 필요하다.

### 배포

```bash
docker build -t suddoe-api:local .
docker build -t $IMG . && docker push $IMG
gcloud run deploy suddoe-api --image=$IMG --region=asia-northeast3 \
  --memory=8Gi --cpu=2 --min-instances=1 --concurrency=4 --timeout=300 \
  --set-env-vars=SUDDOE_MOCK=0,SUDDOE_LLM=qwen,SUDDOE_QWEN_MODEL=qwen3.7-plus
```

배포는 수동이다(GitHub push로 자동 반영되지 않는다). `DOCKER_BUILDKIT=1`이 필수이고
(Dockerfile의 HF 캐시 마운트 때문), arm 머신에서는 `--platform linux/amd64`를 붙인다.
전체 절차는 [`docs/8_운영/8-5_배포_레시피.md`](docs/8_운영/8-5_배포_레시피.md).

### LLM 설정

운영 기본값은 DashScope Qwen3.7-plus다.

```dotenv
SUDDOE_LLM=qwen
DASHSCOPE_API_KEY=your_dashscope_api_key_here
SUDDOE_QWEN_MODEL=qwen3.7-plus
```

---

<a id="3-기술-스택"></a>
## 3. ✨ 기술 스택

### Backend

<p>
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Uvicorn-2A2A2A?style=for-the-badge" alt="Uvicorn">
  <img src="https://img.shields.io/badge/Pydantic-E92063?style=for-the-badge&logo=pydantic&logoColor=white" alt="Pydantic">
</p>

### Data · Search

<p>
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/pgvector-336791?style=for-the-badge" alt="pgvector">
  <img src="https://img.shields.io/badge/KURE--v1-CPU%20임베딩-6E56CF?style=for-the-badge" alt="KURE-v1">
  <img src="https://img.shields.io/badge/kiwipiepy-BM25-FFB000?style=for-the-badge" alt="kiwipiepy">
</p>

### LLM

<p>
  <img src="https://img.shields.io/badge/DashScope-Qwen3.7--plus-FF6A00?style=for-the-badge" alt="DashScope Qwen">
</p>

### Infra

<p>
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Cloud%20Run-4285F4?style=for-the-badge&logo=googlecloud&logoColor=white" alt="Cloud Run">
  <img src="https://img.shields.io/badge/Cloud%20SQL-4285F4?style=for-the-badge&logo=googlecloud&logoColor=white" alt="Cloud SQL">
  <img src="https://img.shields.io/badge/Supabase%20Auth-3FCF8E?style=for-the-badge&logo=supabase&logoColor=white" alt="Supabase Auth">
  <img src="https://img.shields.io/badge/Vercel-000000?style=for-the-badge&logo=vercel&logoColor=white" alt="Vercel">
</p>

---

<a id="4-주요-기능"></a>
## 4. 📍 주요 기능

- **4지 판정** — 가능·조건부·불가·판단불가로 닫는다. 근거가 모자라면 "아마 가능"을
  만들지 않고 판단불가로 떨어진다(오답 비대칭: 최악은 "틀린 가능").
- **근거 인용(S번호)** — LLM은 검색·룰 조회로 찾은 문장 번호만 고르고, 원문·조번호·출처는
  코드가 DB에서 그대로 채운다. 원문을 LLM이 "쓰지" 않으므로 인용 환각이 구조적으로 막힌다.
- **체크리스트** — 결제 전후 확인 항목(52개)을 코드가 채운다. 문항 문구는 LLM이 새로 쓰지
  않는다(재현성).
- **전제 해소** — 판정에 필요한데 아직 없는 사실(F축 데이터)을 짚어 채우게 하거나,
  채울 수 없으면 담당자 문의로 넘긴다.
- **문의초안** — 판단불가일 때 담당자에게 보낼 질문 초안을 코드가 조립한다(LLM 미사용).
- **L3 업로드** — 주관기관 자체 규정(HWP/HWPX/PDF)을 업로드하면 배경에서 파싱·적재하고,
  L1·L2와 함께 판정에 반영한다. 검색하지 않고 통째로 읽어 다른 기관 규정과 섞이지 않는다.
- **규정 자동 수집** — 발행처 현행판을 주기적으로 비교해 바뀐 조문만 사람 검수 큐로 올리는
  경로(`/ingest/weekly`)가 배선돼 있다. 스케줄러 연결은 아직이다.

---

<a id="5-성능"></a>
## 5. 📊 성능

정답셋 342문항(검증 315·채점 분모 320) 전체를 운영 코퍼스(2026-09-07 동기화) 기준으로
채점한 run 206 결과다.

| 축 | 값 |
|---|---:|
| 전체 일치율 | **67.8%**(다수결 기준선 57.8%, +10.0%p) |
| 세트별 일치율 | 적대적 96.3 · 공식 70.0 · 본세트 69.3 · 공통 68.2 · L3 55.6 · 보강 25.0 |
| 정답유형별 일치율 | 불가 85.9 · 조건부 69.4 · 가능 14.3 · 판단불가 9.5 |
| 인용 정확도 / 근거 도달률 | 63.9% / 63.2% |
| 안전 지표(불가를 가능으로 오판) | **0 / 320** |
| 전수 평가 1회 비용 | $2.92(문항당 약 12원) |
| 판정 지연 | 1건 약 60초(정규화 4초 + 판정 55~59초) |

전체 일치율은 유형별 편차를 가린다. **가능(14.3%)·판단불가(9.5%)가 지금 약점**이다.
세트별로 보면 병목은 검색이 아니라 판단이다 — 적대적 세트는 근거를 못 찾고도 판정은
맞히고(인용 33.3%), 보강 세트는 근거를 찾아놓고도 해석에서 틀린다(인용 81.0%). 검색
(RRF top-5)은 이미 충분하고, 개선 여지는 판정 프롬프트 쪽에 남아 있다. 상세와 세트 정의는
[`docs/제출_공모전_설명.md`](docs/제출_공모전_설명.md).

---

<a id="6-프로젝트-구조"></a>
## 6. 🗂️ 프로젝트 구조

```text
server/                          FastAPI 앱 — 라우트·인증·판정 오케스트레이션 진입점
scripts/                         정규화·검색·룰조회·판정·검증·L3 파싱·수집 파이프라인
db/init/                         스키마·RLS·시드 SQL (docker compose 로 적재)
docs/                             설계·운영 문서 (docs/README.md 가 지도)
2026_Finance_DATA_FOR_RAG/       원본 규정·PMS 문서 및 데이터셋 구조 설명
법령 PDF/                        L1(중기부 법령) 원본 PDF
_hwp변환/                        HWP 원본을 변환한 중간 산출물
Dockerfile                       API 컨테이너 빌드(FastAPI + KURE-v1 CPU 임베딩)
requirements-api.txt             API 컨테이너 의존성 (torch CPU 휠 고정 등)
.env.example                     환경변수 이름표(값은 Secret Manager)
```

---

<a id="7-데이터와-활용-한계"></a>
## 7. ⚠️ 데이터와 활용 한계

지금 코퍼스(2026-09-07 기준):

| 항목 | 값 |
|---|---|
| 문서 | 283 (활성 245) |
| 검색 청크 | 20,648 |
| 조문 간 참조 | 44,865 |
| 룰 | 82행(금지예시 358 · 허용예시 225, 전부 verified) |
| 체크항목 | 52 |
| 비목 | 창업 10 · RND(TIPS) 8 · 별칭 326 |
| 정답셋 | 342문항(검증 315 · 채점 분모 320) |
| L3(업로드분) | 224조각 |

규범은 세 층으로 관리한다.

- **L1 중소벤처기업부** — 법률·고시·훈령·통합관리지침. 우리가 조달한다.
- **L2 창업진흥원** — 사업별 세부관리기준. 우리가 조달한다.
- **L3 주관기관** — 기관 규정·협약 특약. 사용자가 업로드한다.

충돌 해소는 단순히 "더 엄격한 쪽이 이긴다"가 아니다. 8개 사업 중 6개가 `L2 > L3`를
명시하고(주관기관 규정이 더 엄격해도 진다), 초격차·모두의창업·TIPS는 `L1 > L2`도 갖는다
(지침에 없는 것만 세부관리기준으로 채운다). 이 관계는 근거 조문과 함께 우선순위 룰
9행에 들어 있다. 검색 인덱스는 L1·L2에만 걸려 있고(`layer IN ('L1','L2')`), 다른
기관 규정이나 정답셋은 구조적으로 인덱스에 들어가지 않는다.

한계로 알고 있는 것:

- 규정 자동 수집(`/ingest/weekly`)은 배선만 됐고 스케줄러 연결은 미결이다.
- L3(기관 규정) 세트는 다른 세트보다 일치율이 낮다(55.6%) — 조건단위로 보면 9개뿐인
  경상국립대 사례로 검증했고, 그중 L1/L2와 실제로 갈리는 조건은 9개뿐이라 판정력을
  넓게 일반화하기는 이르다.
- 게스트 사용자는 F축(기관 프로필) 없이 판정하므로 전제가 다수 남는다 — 로그인은
  정확도를 높이는 다이얼이지 관문이 아니다.

더 자세한 판정 흐름·배포 구조는 [`docs/README.md`](docs/README.md)와
[`docs/제출_공모전_설명.md`](docs/제출_공모전_설명.md)를 확인한다.
</content>
