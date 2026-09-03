# W-GPU 재측정 기동 절차 — 켠 뒤에 헤매는 시간이 곧 돈이다

작성 2026-09-03 ai-98 · 중앙(ai-43) 「지금 켜라」가 오면 이 순서 그대로 간다.
🔴 이 문서는 `scratchpad/` 다. `docs/**` 는 이 레인에서 손대지 않는다.

## 0. 켜기 전에 (팟 없이 끝내 둔 것 — 이미 완료)

- [x] `scratchpad/wgpu_e2e.py` 목 모드 완주 — 스크립트 자체 버그 없음
- [x] 대조 로직 검산 — `python scratchpad/wgpu_대조검산.py` → 불일치 0건
- [x] DB 앵커 보존 — `tenant.decisions` 4275·4276·4277 (지우지 말 것)
- [ ] 중앙의 「지금 켜라」 + ai-d9·ai-de·ai-b2 반영 완료 **두 조건이 같이 서야 켠다**

## 1. 팟 (약 3분 + 모델 적재 10~15분)

```bash
python scripts/runpod_pod.py open          # 대장 .claude/_runpod_open.json 에 기록된다
```
- 🔴 **서버측 자동 종료는 없다** (2026-08-31 실측). `--terminate-after` 는 존재하지 않는 플래그다.
  끄는 것은 사람이다. 끝나면 `python scripts/runpod_pod.py close <id>` (= delete).
- 네트워크 볼륨 `fv5cl1y1ww` 에 `/workspace/venv` 와 HF 캐시가 그대로 있다 → 재설치 없음.

## 2. vLLM 기동 (SSH)

```bash
bash /tmp/podssh.sh "<pod-id>" "cd /workspace && source venv/bin/activate && \
  nohup vllm serve Qwen/Qwen3-8B --max-model-len 40960 --port 8000 > /workspace/vllm.log 2>&1 &"
```
- 🔴 `--max-model-len 40960`. 조립 프롬프트 실측 8,909 / 9,353 / 9,331 토큰 (전부 `finish_reason=stop`).
- 죽일 때는 `pkill -f 'vllm[ ]serve'` — 대괄호를 빼면 **명령 문자열 자신이 패턴에 걸려 SSH 세션이 같이 죽는다.**
- SSH 가 간헐적으로 안 붙는다 → `/tmp/podssh.sh` 가 25회 재시도한다.
- 뜬 것 확인: `curl -H 'User-Agent: suddoe/1.0' https://<pod>-8000.proxy.runpod.net/v1/models`
  🔴 **User-Agent 없으면 Cloudflare 가 403 "error code: 1010"** 을 준다 (`Python-urllib/3.x` 차단).

## 3. 계수 프록시 (닻 ①)

```bash
VLLM_TARGET=https://<pod>-8000.proxy.runpod.net python scratchpad/vllm_probe.py   # :8011
curl -s http://127.0.0.1:8011/__probe/stats
```
프록시를 **앱 밖**에 두는 이유: 앱 안에서 세면 앱이 스스로를 증명하는 꼴이다.

## 4. 로컬 API (실경로)

```bash
SUDDOE_MOCK=0 VLLM_URL=http://127.0.0.1:8011 SUDDOE_GPU_IDLE_MIN=0 \
SUDDOE_ADMIN_TOKEN=<값> SUDDOE_DSN=postgresql://postgres:devpw@localhost:5432/suddoe \
python -m uvicorn server.main:app --port 8080 --host 127.0.0.1
```
- `SUDDOE_GPU_IDLE_MIN=0` — 워치독이 측정 중에 팟에 stop 을 쏘지 않게.
- 🔴 `SUDDOE_ADMIN_TOKEN` 은 로컬에 **없다** (`.env` 파일 자체가 없고 `.env.example` 만 있다).
  없으면 `/admin/warmup` 은 403 이다. 값을 세션 간에 주고받지 않는다 — 오너/중앙이 로컬에 심는다.
- 확인: `curl -s :8080/api/health` → `"모드":"live"` 여야 한다. `mock` 이면 3·4 를 다시 본다.
- 🔴 DSN 은 **로컬** 이다. Cloud SQL(34.64.37.177)에는 이 레인이 쓰지 않는다.

## 5. 측정 3벌 — 같은 질문 · 같은 파라미터

```bash
python scratchpad/wgpu_e2e.py --경로 자연어              # 되짚기 경로 (기본값)
python scratchpad/wgpu_e2e.py --경로 자연어 --원문주입    # 되짚기 우회 대조군
python scratchpad/wgpu_e2e.py --경로 폼
```
- 벌마다 `_sse_*.txt` · `_보고_*.json` 이 남는다. `모드 != live` 면 파일명에 `mock_` 이 붙는다
  (목 실행이 실판정 원자료를 덮어쓴 사고가 한 번 났다 — 이제 구조로 막혀 있다).
- 워밍업은 스크립트가 **계수 리셋 앞**에서 부른다. 워밍업도 vLLM 을 한 방 쓴다.

## 6. 읽는 법 — 무엇이 답인가

| 재는 것 | 어디를 보나 | 어제 값 | 고쳐졌다면 |
|---|---|---|---|
| ⓐ 인용 건수 | `인용이벤트_건수` | 0 · 0 · 0 | DB 와 같은 수 |
| ⓑ DB↔SSE 내용 | **`인용키일치`** (s번호\|조번호\|항호\|doc_id) | 대조 불가(SSE 0건) | `true` |
| ⓒ 할일 중복 | `해야할일_중복건수` | DB 4275 는 10건 중 **9건 중복** | SSE 쪽 0 |
| ⓓ LLM 호출 | `judge_프록시수.chat` + `metrics` 증분 | 3 (정규화1+판정2) | 2 |

- 🔴 `캐시적중=true` 면 호출 0 이 정상이다. 안 가르면 「2회 달성」으로 잘못 읽는다.
- 🔴 ⓒ 는 DB 와 SSE 가 **달라도 정상이다** — `_할일_중복제거` 는 API 계층(`main.py:1010`)에만 있고
  DB 에는 오케 원본이 그대로 들어간다. 「DB 10 · SSE 1」은 결함이 아니라 설계다.

## 7. 끝나면

```bash
python scripts/runpod_pod.py close <pod-id>    # delete. 명령 성공 ≠ 팟 소멸
curl -H "Authorization: Bearer $KEY" https://rest.runpod.io/v1/pods   # 실물 0개인지 다른 닻으로
cat .claude/_runpod_open.json                                        # 대장도 비었는지
```
프록시(8011)·API(8080) 도 같이 내린다. 과금은 `createdAt` ~ 삭제 시각 × 시간당 단가로 계산해 보고한다.
