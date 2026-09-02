#!/bin/bash
# 팟 환경 구축 — **볼륨 위에 venv 를 만든다.**
#
# 🔴 컨테이너 디스크에 pip install 하면 팟이 죽을 때 같이 죽는다.
#    판정층은 프롬프트를 고치며 여러 번 돌릴 작업이라 그 비용을 매번 낸다.
#    venv 를 /workspace(볼륨)에 두면 모델 20GB 와 함께 살아남는다.
#
# 🔴 **머신마다 드라이버가 다르다.** 2026-08-31 에 같은 L40 을 두 번 받았는데
#    550.127.08(CUDA 12.4)과 580.178.04(CUDA 13.0)로 갈렸다.
#    버전을 하드코딩하면 한쪽에서 깨진다 — 실제로 깨졌다:
#      ① 고정 없이 `pip install vllm` -> torch 2.8.0+cu128 을 2.13.0+cu130 으로 갈아치움
#         드라이버가 12.4 라 RuntimeError: NVIDIA driver is too old (found 12040)
#      ② vLLM 0.8.5 를 깔았더니 transformers 가 최신이라 토크나이저 API 불일치
#         AttributeError: Qwen2Tokenizer has no attribute all_special_tokens_extended
#         🔴 2026-09-02 에 **다른 버전으로 재발**했다 (vllm 0.11.0 + transformers 5.16.1).
#         그래서 이제 constraints 에 `transformers<5` 를 같이 못박는다
#
#    그래서 **템플릿 torch 가 이미 CUDA 를 쓸 수 있으면 그걸 상속**하고
#    (`--system-site-packages` + `--no-deps`), 못 쓸 때만 드라이버에 맞춰 새로 깐다.
#    torch 를 건드리지 않는 것이 이 스크립트의 핵심이다.
#
# 사용:
#   scp pod_setup.sh root@<ip>:/workspace/ && ssh ... 'bash /workspace/pod_setup.sh'
set -euo pipefail

VENV=/workspace/venv
export HF_HOME=/workspace/hf

echo "=== 머신 확인 ==="
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
CUDA_DRV=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9.]+' | head -1)
TORCH_SYS=$(python -c 'import torch;print(torch.__version__)' 2>/dev/null || echo none)
TORCH_OK=$(python -c 'import torch;print(torch.cuda.is_available())' 2>/dev/null || echo False)
echo "드라이버 CUDA ${CUDA_DRV} · 템플릿 torch ${TORCH_SYS} · cuda avail ${TORCH_OK}"

if [ -x "${VENV}/bin/python" ] && "${VENV}/bin/python" -c 'import vllm' 2>/dev/null; then
  echo "=== venv 가 이미 있다. 건너뛴다 ==="
  "${VENV}/bin/python" -c 'import torch,vllm;print("torch",torch.__version__,"avail",torch.cuda.is_available(),"| vllm",vllm.__version__)'
  echo "SETUP_DONE"
  exit 0
fi

rm -rf "${VENV}"

if [ "${TORCH_OK}" = "True" ]; then
  echo "=== 템플릿 torch 가 정상이다. 상속하고 torch 만 고정한다 ==="
  python -m venv --system-site-packages "${VENV}"
  "${VENV}/bin/pip" install -q --upgrade pip

  # 🔴 핀을 하나씩 쫓지 않는다. **torch 만 constraints 로 못박고 나머지는 vLLM 이 풀게 한다.**
  #    2026-08-31 에 --no-deps + 손으로 의존성 나열을 시도했다가 xgrammar·numba·
  #    outlines_core·setuptools 가 줄줄이 어긋났다. 하나 고치면 다음 게 튀어나온다.
  #    vLLM 은 자기 의존성 표를 갖고 있으니 그걸 쓰게 하고, 우리는 torch 만 지킨다.
  TORCH_V=$("${VENV}/bin/python" -c 'import torch;print(torch.__version__.split("+")[0])')
  TV_V=$("${VENV}/bin/python" -c 'import torchvision;print(torchvision.__version__.split("+")[0])' 2>/dev/null || echo "")
  {
    echo "torch==${TORCH_V}"
    [ -n "${TV_V}" ] && echo "torchvision==${TV_V}"
    # 🔴 transformers 도 못박는다 (2026-09-02 재발). vLLM 이 자기 의존성으로 5.x 를 끌어오면
    #    토크나이저 API 가 안 맞아 부팅이 죽는다 — 아래 함정 ② 와 같은 병, 다른 버전이다.
    #      vllm 0.11.0 + transformers 5.16.1 -> Qwen2Tokenizer has no attribute
    #      all_special_tokens_extended.  4.57.6 으로 내려서 해결했다
    echo "transformers<5"
  } > /workspace/constraints.txt
  echo "torch 고정:"; cat /workspace/constraints.txt

  "${VENV}/bin/pip" install -q --constraint /workspace/constraints.txt "vllm==0.11.0"
else
  echo "=== 템플릿 torch 가 CUDA 를 못 쓴다. 드라이버에 맞춰 새로 깐다 ==="
  case "${CUDA_DRV}" in
    12.4*|12.5*|12.6*) IDX=cu124; TORCH=2.6.0; VLLM=0.8.5.post1 ;;
    12.8*|12.9*|13.*)  IDX=cu128; TORCH=2.8.0; VLLM=0.11.0 ;;
    *) echo "🔴 미검증 드라이버 ${CUDA_DRV}. 위 표를 갱신할 것"; exit 1 ;;
  esac
  python -m venv "${VENV}"
  "${VENV}/bin/pip" install -q --upgrade pip
  "${VENV}/bin/pip" install -q "torch==${TORCH}" --index-url "https://download.pytorch.org/whl/${IDX}"
  { echo "torch==${TORCH}"; echo "transformers<5"; } > /workspace/constraints.txt   # transformers 5.x 는 토크나이저 API 불일치 (2026-09-02)
  "${VENV}/bin/pip" install -q --constraint /workspace/constraints.txt "vllm==${VLLM}"
fi

echo "=== 검증 ==="
"${VENV}/bin/python" -c "
import torch, transformers, vllm
print('torch       ', torch.__version__, '| cuda', torch.version.cuda, '| avail', torch.cuda.is_available())
print('transformers', transformers.__version__)
print('vllm        ', vllm.__version__)
assert torch.cuda.is_available(), 'CUDA 를 못 쓴다'
"
echo "SETUP_DONE"
