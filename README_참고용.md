<div align="center">

# 🐄 익산시 악취 민원 선제 대응 AI

**민원 확산을 예측하고 현장 대응 문서를 생성하는 AI 기반 의사결정 지원 서비스**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.62-FF4B4B?style=flat-square&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![XGBoost](https://img.shields.io/badge/XGBoost-3.x-EB5B25?style=flat-square)](https://xgboost.ai/)

[🚀 데모 실행하기](https://smart-livestock-ai-2.streamlit.app/) · [▶️ 데모 영상 보기](https://www.youtube.com/watch?v=ZC-iM7M16fY) · [📘 프로젝트 상세 문서](PROJECT_CONTEXT.md)

</div>

---

## 📋 목차

1. [프로젝트에 대한 정보](#1-프로젝트에-대한-정보)
2. [시작 가이드](#2-시작-가이드)
3. [기술 스택](#3-기술-스택)
4. [주요 기능](#4-주요-기능)
5. [모델 성능](#5-모델-성능)
6. [프로젝트 구조](#6-프로젝트-구조)
7. [데이터와 활용 한계](#7-데이터와-활용-한계)

---

<a id="1-프로젝트에-대한-정보"></a>
## 1. 📌 프로젝트에 대한 정보

### 프로젝트 소개

과거 악취 민원의 시공간 패턴을 학습해 현재 Event 이후 30분 안에 **추가 민원이 접수될 가능성이 높은 1km 권역 Top 3**를 제시합니다. 예측 결과를 바탕으로 담당자 검토용 상황 브리핑, 현장점검 지시서 및 사후 결과보고서를 생성합니다.

> 예측 결과는 악취 농도나 실제 배출 확률이 아닌 **민원 접수 위험의 상대적 우선순위**입니다. 특정 시설을 원인으로 판정하거나 행정조치를 자동으로 결정하지 않습니다.

| 구분 | 내용 |
|---|---|
| 프로젝트명 | 익산시 악취 민원 선제 대응 AI |
| 소속 | 인공지능 및 빅데이터 연합동아리 BITAmin |
| 대상 지역 | 전북특별자치도 익산시 |
| 예측 목표 | 초기 30분 이후, 향후 30분의 추가 민원 위험 권역 Top 3 |
| 운영 단위 | 1km 격자 |
| 배포 주소 | [Streamlit 데모](https://smart-livestock-ai-2.streamlit.app/) |
| 저장소 | [GitHub Repository](https://github.com/yeonseochoi/smart-livestock-ai-2) |

### 🖥️ 데모 메인 화면

<img width="1635" height="907" alt="image" src="https://github.com/user-attachments/assets/8cdf192e-f3fd-4a29-bf2c-09e1b032237c" />

### ▶️ 데모 영상

아래 이미지를 클릭하면 YouTube 데모 영상으로 이동합니다.

<p align="center">
  <a href="https://www.youtube.com/watch?v=ZC-iM7M16fY">
    <img src="https://img.youtube.com/vi/ZC-iM7M16fY/hqdefault.jpg" alt="익산시 악취 민원 선제 대응 AI 데모 영상" width="720">
  </a>
</p>

<p align="center"><a href="https://www.youtube.com/watch?v=ZC-iM7M16fY"><b>유튜브에서 데모 영상 시청하기</b></a></p>

---

<a id="2-시작-가이드"></a>
## 2. 🚀 시작 가이드

### 요구 사항

- Python 3.12
- Windows PowerShell 또는 호환 터미널
- 선택 사항: Gemini 또는 OpenAI API 키

### Streamlit 데모 실행

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

터미널에 표시되는 로컬 주소를 브라우저에서 열면 됩니다. API 키가 없어도 안전 템플릿 모드로 모든 핵심 기능을 확인할 수 있습니다.

### Windows 데모 실행

- `run_demo_template.bat`: 외부 API를 호출하지 않는 안전 템플릿 모드
- `run_demo.bat`: `.env` 설정에 따른 Gemini/OpenAI 연동 모드

실행 후 브라우저에서 <http://127.0.0.1:8765>를 엽니다.

### LLM 연동

`.env.example`을 `.env`로 복사한 뒤 사용할 공급자의 값을 설정합니다. **실제 API 키는 GitHub에 커밋하지 마세요.**

```dotenv
# Gemini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.6-flash

# OpenAI를 사용할 경우 위 설정 대신 사용
# LLM_PROVIDER=openai
# OPENAI_API_KEY=your_openai_api_key_here
# OPENAI_MODEL=your_supported_model_id
```

Streamlit Community Cloud에서는 실행 파일을 `streamlit_app.py`로 지정하고, App settings의 Secrets에 [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example)과 같은 형식으로 키를 등록합니다.

---

<a id="3-기술-스택"></a>
## 3. ✨ 기술 스택

### AI · Data

<p>
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Pandas-150458?style=for-the-badge&logo=pandas&logoColor=white" alt="Pandas">
  <img src="https://img.shields.io/badge/scikit--learn-F7931E?style=for-the-badge&logo=scikitlearn&logoColor=white" alt="scikit-learn">
  <img src="https://img.shields.io/badge/XGBoost-EB5B25?style=for-the-badge" alt="XGBoost">
</p>

### Demo · Map · LLM

<p>
  <img src="https://img.shields.io/badge/Streamlit-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white" alt="Streamlit">
  <img src="https://img.shields.io/badge/Folium-77B829?style=for-the-badge&logo=leaflet&logoColor=white" alt="Folium">
  <img src="https://img.shields.io/badge/Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white" alt="Gemini">
  <img src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white" alt="OpenAI">
</p>

---

<a id="4-주요-기능"></a>
## 4. 📍 주요 기능

- **민원 확산 예측** — 초기 30분 민원을 바탕으로 향후 30분의 추가 민원 위험 권역을 예측합니다.
- **운영 격자 선정** — 1km·1.5km·2km 격자 모델을 비교하고 시간순으로 검증합니다.
- **Top 3 우선순위** — 1km 격자의 상위 3개 권역과 상대 위험도를 지도에 표시합니다.
- **현장 참고정보** — 풍속·풍향·습도·강수 정보를 담당자의 현장 판단 자료로 제공합니다.
- **행정 대응 Agent** — 상황 브리핑과 현장점검 지시서 초안, Event별 AI 대응 참고 가이드를 생성합니다.
- **사후 결과 기록** — 출동·측정·조치 결과를 직접 입력하고 완성된 결과보고서를 내려받습니다.
- **안전한 문서 생성** — API 키가 없거나 LLM 호출이 실패하면 검증 가능한 안전 템플릿을 사용합니다.

---

<a id="5-모델-성능"></a>
## 5. 📊 모델 성능

민원 Event를 시간순으로 70% 학습, 30% 테스트로 분리했습니다. 운영 기준인 1km 격자의 테스트 결과입니다.

| 평가 지표 | 결과 |
|---|---:|
| ROC-AUC | 0.908 |
| PR-AUC | 0.486 |
| Top-K Recall | 0.507 |
| Recall@3 | 0.443 |
| Event Hit@3 | 85.1% |
| 실제 추가 민원 격자 비율 | 약 10.7% |
| Event당 평균 후보 권역 | 약 31개 |
| 선택 모델 | XGBoost 분류모델 `xgb_d3` |
| 운영 출력 | 상위 3개 권역 |

`Event Hit@3`는 추가 민원이 존재하는 평가 Event 중 예측 상위 3개 권역 하나 이상에 실제 추가 민원이 포함된 비율입니다. 테스트 Event 48개 가운데 추가 민원이 실제 발생해 평가가 가능한 Event는 47개이며, 그중 40개에서 상위 3개 권역이 적중했습니다. 표본이 47개이므로 이 비율의 95% 신뢰구간은 대략 75~95%입니다. 연도별·현장 검증이 추가로 필요합니다.

후보 모델은 학습 구간 안에서만 선택했습니다. 학습 112개 Event의 앞 80%로 학습하고 뒤 20%를 내부 검증에 사용해 5개 후보 중 하나를 고른 뒤, 최종 테스트 48개 Event는 한 번만 사용했습니다. 모델 출력은 절대확률이 아니라 하나의 Event 안에서 후보 권역 간 상대적 우선순위로 활용합니다.

> 재현 참고자료: [실험 코드](compare_operational_grid_sizes.py) · [결과 산출물](outputs/operational_grid_comparison/metrics.json)

---

<a id="6-프로젝트-구조"></a>
## 6. 🗂️ 프로젝트 구조

```text
administrative_agent/              행정 대응 문서 모델·템플릿·LLM 연동
data/                              원본 및 가공 데이터
demo/                              브라우저 데모와 로컬 API 서버
docs/screenshots/                  README 데모 스크린샷
outputs/                           모델 평가 및 생성 문서
tests/                             행정 대응 Agent와 Streamlit 테스트
streamlit_app.py                   Streamlit Community Cloud 실행 파일
compare_operational_grid_sizes.py  격자 크기 비교와 최종 성능 재현
generate_agent_documents.py        예측 결과 기반 문서 생성
optimize_early_prediction.py       후보 모델 학습·평가
sensitivity_early_prediction.py    격자·시간창 후보 비교(설계 근거, 제품 입력 아님)
fetch_kma_weather.py               현장 참고용 기상자료 수집
```

### 예측 및 문서 재생성

```powershell
# 모델 비교 결과 재생성
.venv\Scripts\python.exe compare_operational_grid_sizes.py

# 최신 Event 행정문서 생성
.venv\Scripts\python.exe generate_agent_documents.py

# 특정 Event 행정문서 생성
.venv\Scripts\python.exe generate_agent_documents.py --event-id EVT-0175

# 전체 테스트
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

생성된 문서는 `outputs/administrative_agent/`에 저장됩니다.

---

<a id="7-데이터와-활용-한계"></a>
## 7. ⚠️ 데이터와 활용 한계

- 데이터: `data/익산시 악취 민원 데이터_20190528-20260818.xlsx`
- 실제 민원 시각 범위: 2019-05-28 23:23 ~ 2026-08-17 22:54
- 전체 16,113건 중 익산시 민원 14,994건
- 공통 Event 160개, 시간순 학습 112개 / 테스트 48개
- 신고 위치는 신고 행동이 반영된 데이터이며 실제 악취 영향권과 같지 않습니다.
- 장기간의 현장 출동 결과와 배출원 정보가 없어 악취 발생 자체나 원인 시설을 학습하지 않았습니다.
- 기상정보는 최종 예측 점수에 사용하지 않으며 현장 판단을 위한 참고자료로만 제공합니다.

예측 엔진과 행정 대응 Agent는 분리되어 있습니다. Agent는 예측 엔진이 전달한 Top 3와 확인 가능한 상황정보만 사용하며, 민원 원본이나 시설 데이터를 직접 조회하거나 원인 시설을 추론하지 않습니다.

더 자세한 설계와 의사결정 근거는 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)를 확인하세요.
