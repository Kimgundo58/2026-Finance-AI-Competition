#!/bin/bash
# RunPod 팟 환경 구축 — 볼륨(/workspace) 위에 vLLM venv 를 만든다. 멱등(venv 있으면 건너뜀).
# 템플릿 torch 가 CUDA 를 쓸 수 있으면 상속하고(--system-site-packages), 못 쓸 때만 드라이버에 맞춰 새로 깐다.
# torch·transformers 는 constraints 로 못박는다 — transformers 5.x 는 Qwen 토크나이저 API 가 안 맞는다.
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

  # torch 만 constraints 로 못박고 나머지 의존성은 vLLM 이 풀게 한다.
  TORCH_V=$("${VENV}/bin/python" -c 'import torch;print(torch.__version__.split("+")[0])')
  TV_V=$("${VENV}/bin/python" -c 'import torchvision;print(torchvision.__version__.split("+")[0])' 2>/dev/null || echo "")
  {
    echo "torch==${TORCH_V}"
    [ -n "${TV_V}" ] && echo "torchvision==${TV_V}"
    echo "transformers<5"
  } > /workspace/constraints.txt
  echo "torch 고정:"; cat /workspace/constraints.txt

  "${VENV}/bin/pip" install -q --constraint /workspace/constraints.txt "vllm==0.11.0"
else
  echo "=== 템플릿 torch 가 CUDA 를 못 쓴다. 드라이버에 맞춰 새로 깐다 ==="
  case "${CUDA_DRV}" in
    12.4*|12.5*|12.6*) IDX=cu124; TORCH=2.6.0; VLLM=0.8.5.post1 ;;
    12.8*|12.9*|13.*)  IDX=cu128; TORCH=2.8.0; VLLM=0.11.0 ;;
    *) echo "미검증 드라이버 ${CUDA_DRV}. 위 표를 갱신할 것"; exit 1 ;;
  esac
  python -m venv "${VENV}"
  "${VENV}/bin/pip" install -q --upgrade pip
  "${VENV}/bin/pip" install -q "torch==${TORCH}" --index-url "https://download.pytorch.org/whl/${IDX}"
  { echo "torch==${TORCH}"; echo "transformers<5"; } > /workspace/constraints.txt
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
