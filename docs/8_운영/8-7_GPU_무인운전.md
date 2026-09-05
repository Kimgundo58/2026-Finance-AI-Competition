# GPU 무인 운전 (Q2, 2026-09-04)

이 문서가 답하는 질문:
- 로그인 한 번이 어떻게 GPU 를 깨우는가 — 판정 요청과 왜 분리했는가
- 하루 무인 운전의 유일한 하드 가드는 무엇인가
- `SUDDOE_GPU_IDLE_MIN` 을 다시 켤 때 몇 분으로 하는가

## 0. 오늘 실측이 뒤집은 것 — 비용가드 넷은 코드에 없었다

`내일_작업_3건_0904.md`·`8-3_GPU.md` 가 적은 "동시 팟 1 · 하루 깨우기 3회 · 수명 1시간 ·
잔액 경보"는 **정책 문장이었지 코드가 아니었다**(2026-09-04 코드 읽기 확인).

| | 이전 | 지금 |
|---|---|---|
| 동시 팟 1 | 코드 가드 없음 | 여전히 없음 — `RUNPOD_POD_ID` 가 단일값이라는 구조로만 성립. 사람이 `runpod_pod.py ls` 로 감사 |
| 하루 3회 | 없음 | `server/gpu_watchdog.py` `SUDDOE_GPU_WAKE_DAILY_CAP`(기본 3) — 신설, `tests/test_gpu_watchdog.py` 로 발동 확인 |
| 수명 1시간 | `scripts/archive/cli/runpod_pod.py open --hours` 뿐 (수동 오픈 전용, 이 워치독과 다른 경로) | 그대로 — 자동-wake 경로엔 아직 없음 |
| 잔액 경보 | 없음 | **서버 프로세스에서 구현 불가로 확정.** 아래 §3 |

## 0-1. (2026-09-04 관측, 2026-09-05 낡음) 운영엔 키가 없었다 — 그게 헬스체크를 가렸다

✅ **2026-09-05 뒤집힘.** 같은 명령(`gcloud run services describe suddoe-api
--region=asia-northeast3 --format="value(spec.template.spec.containers[0].env)"`)으로
재확인하니 **둘 다 secretKeyRef 로 연결돼 있다** — `RUNPOD_POD_ID=5ofl00dpzidchp` ·
`RUNPOD_API_KEY`(시크릿) · `SUDDOE_GPU_WAKE_DAILY_CAP=8` · `SUDDOE_GPU_IDLE_MIN=15`.
아래 09-04 관측(키 없음)과 그 원인 사슬 분석은 **당시 사실**로 남기되, 지금 상태를
설명하지 않는다.

2026-09-04 관측: `gcloud run services describe` 로 직접 확인 —
env 도 secretRef 도 `RUNPOD_API_KEY`·`RUNPOD_POD_ID` 를 안 싣는다(`VLLM_URL`·`SUDDOE_DSN`·
`SUDDOE_ADMIN_TOKEN` 등만 있다). `gcloud secrets list` 에도 `RUNPOD_API_KEY` 라는 이름의
시크릿이 없다(현재 활성 프로젝트 기준 — 다른 프로젝트/계정에 있을 가능성은 남는다).

**원인 사슬** (ai-d7/Q1 이 2026-09-04 운영에서 잡은 증상이 계기다 — `/api/gpu/status` 가
`상태:가동` 인데 `/api/normalize`·`/api/judge` 는 전부 `LLM실패`/`판단불가` 였다):

```text
RUNPOD_API_KEY 없음 → 팟제어.가능=False → _팟상태 는 영원히 알수없음
  → 현황() 이 프론트 3값 계약(가동|중지|기동중) 을 지키려고 알수없음→가동 으로 접는다
  → /status 는 팟이 완전히 죽어도 「가동」 을 준다
```

**처방**: `현황()` 에 `vLLM_응답` 필드를 추가했다 — `제어.가능`·`활성` 과 **무관하게** vLLM
`/health` 를 직접 물어서 얹는다(`검사주기초`=60s 캐시, HTTP 는 락 밖에서 — `/status` 는
프론트 폴링 핫패스라 지연이 다른 요청을 막으면 안 된다). 상태 3값 계약 자체는 안 건드렸다
— `상태:가동`·`vLLM_응답:false` 조합이 바로 이 사각지대의 신호다. `tests/test_gpu_watchdog.py`
가 제어불가 상황에서도 이 필드가 채워지는지 확인한다.

## 1. `/api/gpu/wake` — 판정 요청과 분리한 이유

🔴 `SUDDOE_GPU_START_SEC` 기본값과 Cloud Run `timeout` 이 **둘 다 300초**다. 콜드부팅
실측은 10~12분(600~720초) — 판정 요청 SSE 안에서 기동을 기다리면 그 요청 자체가
Cloud Run 타임아웃에 먼저 끊긴다(`server/main.py:592·695` 가 그 자리).

`POST /api/gpu/wake` 는 그래서 **즉시 반환**한다. 기동은 백그라운드 스레드가 하고
(`_기동_락` 이 중복 기동을 막는다), 진행은 `GET /api/gpu/status` 폴링으로 본다.
프론트 모달은 로그인 직후 `wake` 를 먼저 치고 `준비됨` 이 될 때까지 기다린 뒤에야
실제 질문을 보내야 위 충돌을 안 만난다(화면 쪽은 Q6 소관).

`POST /api/gpu/reap` 은 `한번_검사()` 1회를 노출한다 — Cloud Scheduler 이관용 계약만
먼저 굳혔다. 🔴 **아직 미배선**이다. 지금 있는 `시작_루프()`(컨테이너 내 60초 스레드)를
끄고 Scheduler 로 넘기는 결정은 중앙 몫 — `minScale 1` 인 한 스레드 쪽도 동작은 한다,
다만 `maxScale 3` 에서 인스턴스마다 유휴 상태가 **로컬 메모리라 서로 안 보인다**는
문제는 남는다(§4).

## 2. 하루 깨우기 캡

`SUDDOE_GPU_WAKE_DAILY_CAP`(기본 3, 운영 실측 2026-09-05 값 **8** — UTC 자정 리셋). 한도를 넘으면 `제어.시작()` 을
**아예 안 부른다** — RunPod 에 쏘고 실패하는 게 아니라 시도 자체를 안 한다. 팟상태는
`중지` 로 남고 `게이트()` 가 판단불가로 닫는다. 이미 가동 중인 팟을 계속 쓰는 것,
이미 기동중인 상태를 폴링하는 것은 캡을 안 먹는다. 새 stop→start 왕복만 센다.

🔴 **오늘 QA 리허설·테스터 여럿이 15분 넘게 쉬었다 다시 들어오는 패턴이 반복되면
3회는 금방 찬다.** 오너·중앙이 QA 창 동안만 캡을 올릴지(예: 8) 판단할 것 — 이 문서는
기본값만 정한다.

## 3. 잔액 경보 — 서버 프로세스에서 구현 불가로 확정

2026-09-04 `https://rest.runpod.io/v1/openapi.json` 대조: REST v1 에 `/user`·잔액
엔드포인트가 **없다**. `/billing/pods`·`/billing/endpoints`·`/billing/networkvolumes`
뿐이고 전부 과거 사용량이다. 지금 잔액이 아니다.

`scripts/archive/cli/runpod_pod.py:cmd_close` 가 잔액을 찍는 건 `runpodctl user` 다(GraphQL 경유, CLI 전용).
그 바이너리는 배포 이미지에 없다.

**잔액 관찰은 사람이 로컬에서 `runpod_pod.py ls`/`close` 로 하는 것이 정본이다.** `server/gpu_watchdog.py::팟제어.잔액()` 은 이유를 남기고 항상 `None`.

## 4. 다중 인스턴스 유휴 판단 — 아직 못 닫은 구멍

`GPU워치독._마지막호출`·`_팟상태` 는 프로세스 메모리다. `maxScale 3` 이면 인스턴스마다
따로 들고 있다 — 인스턴스 A 로 트래픽이 30분 안 오면 A 는 정지를 쏘는데, 그 순간
인스턴스 B 가 판정을 실행 중일 수 있다. `db/init/14_gpu_pod.sql`의 `last_call_at` 컬럼이
이걸 닫기 위한 자리 — **코드 배선은 아직 별개로 안 됐다**(아래와 혼동 금지).

✅ **팟 주소 DB 이전은 배선 완료(2026-09-05, `c0fb014`)** — `scripts/adapter.py::vllm_url()`·
`server/gpu_watchdog.py::_팟id()` 가 `ops.gpu_pod`(id='default')를 우선 읽고 env 로 폴백한다
(30초 캐시). 🔴 **남은 것: wake 가 새 팟을 "만드는" 경로는 아직 없다.** 지금도
`POST /pods/{id}/start`(정지된 기존 팟 켜기)뿐이다 — 팟을 새로 만들면 여전히
`ops.gpu_pod` 행을 사람이 갱신해야 한다.

## 5. `SUDDOE_GPU_IDLE_MIN` 재활성 값

✅ **적용됨(2026-09-05 확인) — 15분.** 제안 당시 근거:
- 30분(기존 기본값)은 QA 세션 사이 자연스러운 휴지에도 안 꺼져 예산을 깎는다
- 5분은 화면 읽는 시간만으로 다음 판정이 10~12분 콜드부팅을 다시 맞는다 — QA 경험을 죽인다
- 15분은 "커피 브레이크 길이"는 견디고, 방치는 최대 15분 유휴 비용으로 막는다

## 6. QA 당일 사전 기동 운용안

```text
시작 20분 전  runpod_pod.py ls 로 팟 0개 확인 → 팟 생성(볼륨 fv5cl1y1ww, US-KS-2)
              → pod_serve.sh 로 vLLM 기동 → /v1/models 로 max_model_len=40960 확인
시작 5분 전   더미 호출 1회(워밍업) → /api/gpu/status 로 「가동」 확인
QA 중         키보드 앞엔 아무도 없어도 워치독이 유휴 15분 뒤 알아서 끈다 —
              다음 요청이 자동으로 다시 깨운다(단, 하루 3회 캡 안에서)
종료 후       runpod_pod.py close 로 사람이 직접 닫는다 (§0 표 「동시 팟 1」의 최종 확인)
```

## 관련 문서

[[8-3_GPU]] · [[8-4_GPU_비용]] · `db/init/14_gpu_pod.sql`(배선완료) · `tests/test_gpu_watchdog.py`
