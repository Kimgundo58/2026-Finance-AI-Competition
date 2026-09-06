# HANDOFF — 2026-09-06 밤

이 문서 하나만 읽으면 이어받을 수 있게 쓴다. 긴 설명은 `docs/` 에 있다.

## 0. 지금 상태 (DB 실측)

```
HEAD  d189cc8        배포  v10 (e7ed2e1) — 그 뒤 커밋 쌓임. 서빙코드는 1414ed2(VLM 판독)
rules       74행 · 금지예시 358 · 허용예시 225 · 🔴 TIPS «0행»
item_vocab  18 (창업10 · RND8) · PK (비목,계통)
item_alias  326행 · 칼럼 10 (근거 3칸 추가 · 175행 채움)
check_items 52 · 질문문 52
golden_set  315 / verified 307 · 🔴 L3 문항 «0건»
recheck     대기 281
🔴 P1 측정 «0회» — 오늘 켠 것들의 효과는 «모른다»
```

## 1. 오늘 한 일 — 대부분 「조용히 죽어 있던 것」을 켠 것

| 커밋 | 무엇이 죽어 있었나 |
|---|---|
| 5d03e39 | 금지·허용예시가 DB 엔 있는데 «프롬프트까지 못 갔다» |
| 8ae285f | 사전승인_조건 병합이 first-non-null — 21/21 조합에서 «매일 L2 를 버렸다» |
| 99c333f | b5_문장이 없는 컬럼을 물어 «트랜잭션을 죽이고» 그 뒤 전부를 조용히 비웠다 |
| bdd6d25 | 전제해소에 f값을 안 넘겨 「아는 건 안 묻는다」 절반이 죽어 있었다 |
| c13526e | B0 에 불가/조건부 구분선 (407 -> 644 -> «850자») |
| 72a5fb7 | 캡 기준을 「토큰」 -> 「안전망이 있는가」. 조건부 금지예시는 항상 전부 |
| d26b96b | case_chunks 드랍 (뷰가 물고 있어 CASCADE 안 쓰고 재정의) |
| e3dece0 | 골든고정()이 «정답 라벨 변경» 을 못 잡던 것 |
| 01edc96 | 정본 규칙을 DB 로 못박음 + 동기화 스크립트 |

+ 골든셋 verified 113→307 · 판정 5건 수정 · 룰 121건 append · VLM 판독 · L1L2 자동적재
+ Cloud Run v8→v10 · CORS 개통 · GPU 유휴 30분 · item_vocab PK · item_alias 근거칸

## 2. 🔴 오늘 다섯 번 밟은 함정 — 다음 세션도 밟는다

```
① 개수를 세고 «내용이 맞다» 고 한다
   「근거 칼럼 52/52」 -> 실제로 날짜 숫자가 근거 조문에 «47/48 없었다»
② except 가 삼켜 «조용히 빈 값» 이 나간다
   _사례() · b5_값 · b5_문장 · 캡 판별기 — 넷 다 같은 모양
③ 스키마를 본 것을 «함수를 부른 것» 으로 친다
   병합을 재구성해 85/5 -> effective_rule() 직접 태우니 «79/11»
④ 검사기가 틀렸는데 대상이 틀린 줄 안다 (두 번). 눈으로 보고서야 갈렸다
⑤ 「참조 0건」을 FK 로만 셌다 — 뷰가 물고 있었다. pg_depend 를 봐야 했다

=> 규칙: 잰 수치를 붙여라 · 「0건」을 「깨끗」으로 세기 전에 «대조군» 으로 검출력을 증명해라
        고친 뒤 «최종 상태에서 다시» 재라
```

## 3. 🔴 잘못 잡았다가 바로잡은 프레임 (다시 틀리지 마라)

```
「룰이 불가를 못 말한다 (허용='불가' 0행)」  ->  «틀린 프레임»
  `허용` 은 «비목 단위» 진술이다. 「지급수수료 비목 전체가 불가」인 사업은 없다
  정답셋 182건은 «질문 단위» 다. 범주가 다르다. 조건부 쏠림은 «정상»
  설계상 불가는 «금지예시» 로 난다

「게이트 A 4.4% 는 구멍이다」  ->  «틀린 프레임»
  오늘 배관을 열어(5d03e39) 금지예시가 «프롬프트에 실린다». LLM 이 받아 판단한다
  실측: 「차량 임차」는 게이트 미적중인데 「차량 등의 임차 경비」가 B4문장에 «들어가 있다»
  => «경로 분담» 이지 «못 하는 것» 이 아니다
     게이트 A = 빠른 길(LLM 0회·재현 100%) / LLM = 나머지(의미 매칭)
```

## 4. 판정 수단

```
GPU (RunPod)   팟 «0개». A6000·L40·6000Ada 재고 없음. A100 은 프로비저닝 실패
  다시 열 때 «확정 조합» (두 번 밟고 얻었다)
    이미지 runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04
    볼륨 fv5cl1y1ww -> /workspace (모델 20GB 캐시. 다운로드 없음) · 컨테이너 60GB
    🔴 볼륨 venv 를 «쓰지 마라» (py3.12 라 이미지와 어긋난다)
       uv venv --python 3.12 /opt/vv
       uv pip install vllm==0.11.0 "transformers<5"   🔴 제약을 «같이». 빠뜨리면
         "Qwen2Tokenizer has no attribute all_special_tokens_extended" 로 죽는다
    HF_HOME=/workspace/hf · VLLM_CACHE_ROOT=/workspace/vllm_cache
    소요 «약 15분» (팟 3~5분 + 모델 로딩 10~12분)
  🔴 워치독이 「알수없음」을 «가동으로 접는다». 팟이 없어도 「가동」이라 답한다
     -> v10 부터 `팟_조회됨: false` 가 같이 나온다. «그걸 봐라»

API (DashScope)  🟢 열렸다. 심사용 본선
  https://dashscope-intl.aliyuncs.com/compatible-mode/v1   (싱가포르 국제판)
  키: Secret Manager `DASHSCOPE_API_KEY` «버전 2» (버전 1은 본토판 · 403)
  qwen3-32b 200 (0.47초) — 우리가 쓰는 그 모델
  🔴 json_schema strict = «받아놓고 조용히 무시» (출력이 쓰레기)
  🟢 json_object = 정상  ->  «검증 재시도 루프» 가 필요하다
  🔴 f_axis 게이트(adapter.py:374)가 외부 제공자를 막는다. 열어야 판정이 나간다
```

## 5. 배포

```
IMG=asia-northeast3-docker.pkg.dev/project-35d896d7-67d7-4b2a-a8f/suddoe/api:v11
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t $IMG .
docker push $IMG
gcloud run deploy suddoe-api --region=asia-northeast3 --image=$IMG
🔴 --set-env-vars 를 붙이지 마라 (기존 환경변수가 덮인다). 바꿀 땐 --update-env-vars
🔴 굽기 전에 «서빙 코드 쓰기 홀드» 를 걸어라. 워킹트리를 굽는다 — 찢어진 스냅샷이 나간다
실서버 https://suddoe-api-1081277785480.asia-northeast3.run.app
프론트 https://2026-finance-ai-competition-fronten.vercel.app  (별개 레포 wori1206/…_Frontend)
       시연 계정 prototype@ssudo.kr / 123456
```

## 6. 남은 작업

```
[도는 중]  N3 증빙발급처URL · N4 실제집행등록+N5 · V 비목_enum분기 · T0 TIPS매핑표 · VLM · 적재API
[열 것]    API(DashScope) · T1·T2·T3(TIPS 룰) · L2(L3 골든셋 30~40건) · T4+L3(검산기)
[전권위임]  크롤링 자동화 — 7/8 확정. 창업중심대학만 «위치 미확인»
           지정한 두 곳 밖도 뒤지는 것 오너 승인됨
           예외 둘만 오너에게: robots.txt/약관 금지 · 로그인 필요
```

## 7. 오너 결정 대기

```
· 공모전 «마감일»        문서 어디에도 없다. 뒤쪽을 어디서 자를지가 여기 걸린다
· 공고문 «평가 항목»      표준지표 요구가 있으면 RAGAS 매핑표가 필요
                        우리 지표: hit@k · 4-way 일치율 · «틀린 가능» 건수 ·
                        판단불가율 2분할 · 잡음 영점(≥1.4%)
· 표 잔재 제거 4건        3건에 정답청크 39개. P1 «측정 뒤» 로 미루기로 함
· f_axis 게이트 해제      API 로 판정하려면 열어야 한다
```

## 8. 다음 관문

```
1) API 어댑터 -> Vercel 에서 «실판정 한 건»
2) 🔴 P1 측정 278문항 — 오늘의 «숫자». 없으면 제출물이 성립하지 않는다
   해석 주의를 `eval.runs.설정` 에 «기계 판독 필드» 로:
     B0 850자 (run 195 는 407자) · 배관개방·UNION·캡·B5 들어간 뒤 · TIPS 룰 없음
3) 시연 완주 + 녹화
4) md 최신화 — 숫자는 DB·run 출력에서 «직접». «무엇을 센 숫자인지» 를 같이 적어라
   (오늘 item_vocab 을 두고 31/18/17 세 값이 문서마다 달랐다 — 행수/별칭합계/미상)
```
