# GPU

이 문서가 답하는 질문:
- 어느 GPU·DC 를 쓰는가
- 팟을 어떻게 열고 반드시 어떻게 닫는가
- 볼륨과 컨테이너 디스크는 무엇이 다른가

## 0. 작업마다 GPU 를 다르게 고른다 — 요구 VRAM 은 가격이 아니라 모델 크기에서 나온다

| 작업 | 모델 | 요구 VRAM |
|---|---|---:|
| 임베딩 | KURE-v1(568M) | 8GB |
| ①정규화·⑤문장생성 | Qwen3 8B | 24GB |
| ④-b 판정 조립 | Qwen3 32B AWQ + 투표N=3~5 | 48GB+ |

임베딩에 A100(80GB)을 잡는 건 8GB 짜리 일에 10배 쓰는 것이다.

```bash
python scripts/pick_gpu.py --task embed        # embed | llm8b | judge 프리셋
python scripts/pick_gpu.py --min-vram 24
```

## 1. 확정 구성

✅ **US-KS-2 · `suddoe-weights` · 50GB · 볼륨 id `fv5cl1y1ww`**

| GPU | VRAM | 볼륨 되는 DC | $/h |
|---|---:|---|---:|
| L40S | 48 | EU-NL-1, US-TX-3 | 0.99 |
| A100 SXM | 80 | EUR-IS-1, US-KS-2 등 | 1.59 |
| RTX PRO 6000 | 96 | EUR-IS-1, US-NC-2 등 | 2.09 |

모델 `Qwen/Qwen3-32B-AWQ`(4bit, 약20GB). US-KS-2 는 48GB+ 카드 5종이라 재고가 빠져도
볼륨을 두고 갈아탈 수 있다. ⚠️ A40 은 폐기 — 볼륨 지원 DC(CA-MTL-1/EU-SE-1)가 없다.

## 2. 비용 가드 — 서버측 자동종료는 없다

팟은 누군가 지울 때까지 돈다. 세 겹 가드, 셋 다 완전하지 않다:

| 겹 | 무엇 | 언제 뚫리나 |
|---|---|---|
| 워치독 | `runpod_pod.py open --hours N` 로컬 프로세스 | PC 꺼지면 같이 죽음 |
| 목록 | `.claude/_runpod_open.json` | 사람이 `ls` 안 보면 무용 |
| 잔액 | 크레딧 소진 시 RunPod 정지 | 손실 상한 = 잔액 |

**원칙은 사람이 닫는 것이다.**

```bash
PYTHONIOENCODING=utf-8 python scripts/runpod_pod.py ls      # 도는 팟 + 경과비용
PYTHONIOENCODING=utf-8 python scripts/runpod_pod.py close   # 목록의 팟 전부
```

## 3. 흐름 (1회 세션)

```
(0)DC/GPU 확정 → (1)볼륨(최초1회) → (2)팟생성(SSH키 먼저, 볼륨은 생성시점에만 붙는다)
→ (3)가중치 다운로드(최초1회, 볼륨에 잔존) → (4)vLLM 기동 → (5)더미호출1회(워밍업)
→ (6)정답셋 추론(결과 로컬 회수) → (7)delete-pod(볼륨은 남김)
다음 세션은 (2)부터 — 가중치가 볼륨에 있어 (3)이 사라진다
```

## 4. 저장 위치 — 팟과 서버리스는 경로가 다르다

```
네트워크 볼륨
  팟:       /workspace          (마운트 경로 직접 지정)
  서버리스: /runpod-volume      (고정)
  hf/       HF 캐시(HF_HOME)     outputs/  LoRA(사다리5단계 전까지 빈다)
컨테이너 디스크(20GB) — 이미지·패키지만, 팟 죽으면 같이 죽는다
```

## 5. 함정

- 볼륨은 팟 생성 시점에만 붙는다 — 나중에 못 붙이고 못 뗀다
- SSH 키는 팟 생성 전에 등록(부팅 시 주입)
- 팟·볼륨·엔드포인트는 전부 같은 DC 여야 스케줄링된다
- 컨테이너 디스크는 중지·종료 시 지워진다 — 남길 것은 전부 볼륨에
- 서버리스 `/runsync` 는 Cloudflare 100초 제한 — 첫 호출은 `/run`+`/status` 폴링,
  `/health` 의 `workers.ready:1` 이 떠도 vLLM 은 아직 부팅 중일 수 있다

## 6. vLLM 기동
```bash
export HF_HOME=/workspace/hf
vllm serve Qwen/Qwen3-32B-AWQ --reasoning-parser qwen3 --enable-prefix-caching \
  --max-model-len 20000 --gpu-memory-utilization 0.92 --port 8000
```
`guided_json` 은 기동 플래그가 아니라 요청 본문에 넣는다. 기동 로그에 `awq_marlin` 확인.

## 7. 사다리 5단계(캘리브레이션) 미결

- vLLM LoRA 서빙 절차가 RunPod 문서에 없다 — upstream vLLM 문서로 확인 필요
- AWQ 4bit 위에 LoRA 를 얹는 조합 — 6단계 진입 전 결정 필요

## 참고

전문·명령 전체는 `GPU Guideline.md`. 사다리 정의는 [[6-1_호출_설계]].

## 관련 문서

[[6-1_호출_설계]] · [[8-1_실행_절차]]
