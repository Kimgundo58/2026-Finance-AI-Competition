# 작업기록 — Vercel 실판정 리허설 (2026-09-06)

## 1. 무엇을 진행하는가
사용자가 수동으로 연 RunPod 팟(`thyaq6ynvt7k2s`)이 대장·백엔드 등록과 안 맞아
Vercel 프론트에서 실제 판정을 던지면 실패하는 상태였다. 정식 절차(8-7 §6)로 팟을
재시작해 vLLM 을 띄우고, Cloud Run 백엔드에 등록한 뒤, Vercel 프론트에서 실제
질문 1건을 던져 GPU 실판정이 끝까지 도는지 확인한다 — 9_미결.md 의
「GPU 창 1회 — 실판정 리허설」 항목.

## 2. 어떻게 진행했는가
- [x] 사전 확인: `/api/gpu/status`(vLLM_응답:false·팟_조회됨:false) + `runpod_pod.py ls`
      로 팟 불일치·initializing 상태 실측 → 사용자에게 보고, 재시작으로 결정받음
- [x] 사용자 지시(간소화): "우리가 팟 직접 키고 Vercel에서 돌려보자, 어차피 나중엔
      Qwen API로 대체" → 정식 pick_gpu 선정 절차는 유지하되 등록 스크립트 검증은 생략
- [x] `SUDDOE_GPU_IDLE_MIN` 확인 — 실측 이미 **30**(gcloud run services describe) →
      변경 불필요, 사용자에게 보고
- [x] 대장에 없는 기존 팟(`thyaq6ynvt7k2s`) 정리 — 포트 미개방(PORTS 빈칸)·볼륨 없음
      확인 후 `runpodctl remove pod`로 삭제, `ls`로 소멸 확인
- [x] `pick_gpu.py --task judge --dc US-KS-2` → A100 PCIe 80GB $1.59/h 추천
- [ ] `runpod_pod.py open`(볼륨 fv5cl1y1ww · ports 8000/http,22/tcp · A100 80GB PCIe)
      진행 중 — SSH 대기로 백그라운드 전환(task bdp2jedwm)
- [ ] vLLM 기동(`pod_serve.sh`) → `/v1/models` 로 `max_model_len=40960` 확인
- [ ] Cloud Run `VLLM_URL`/`RUNPOD_POD_ID` 새 팟으로 갱신(gcloud 직접, 등록 엔드포인트
      버그 이력 있어 우회) → `/api/gpu/status` 확인
- [ ] Vercel(`2026-finance-ai-competition-frontend-team-9ea8.vercel.app`)에서 실제
      질문 1건 제출 → 판정 결과 확인
- [ ] 팟 닫기(`runpod_pod.py close`) + `ls` 로 소멸 재확인

## 3. 결과
(진행 중 — 완료 후 채운다)
