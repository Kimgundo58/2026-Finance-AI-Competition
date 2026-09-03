# syntax=docker/dockerfile:1
# 써도돼요 API 컨테이너 — FastAPI + KURE-v1(CPU 임베딩) 한 덩어리.
#
# 멀티스테이지인 이유는 용량이 아니라 «시점»이다:
#   빌더에서 모델(~1.1GB)을 «굽는다». 런타임 다운로드를 허용하면
#     ① 콜드스타트에 HF 왕복이 얹히고  ② HF rate limit 이 배포를 랜덤하게 죽인다.
#   런타임 단계에서는 HF_HUB_OFFLINE=1 로 네트워크를 아예 막아 그 실수를 못 하게 한다.
#
# GPU(vLLM)는 이 이미지에 없다 — RunPod 별도. requirements-gpu.txt 참조.

# ════════════════════════════════════════════════════════════════════
# 1단계 — 의존성 설치 + 모델 굽기
# ════════════════════════════════════════════════════════════════════
FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-api.txt /tmp/requirements-api.txt
RUN pip install -r /tmp/requirements-api.txt

# KURE-v1 을 HF 캐시에 구워 넣는다. SentenceTransformer 로 «실제로 로드»해서 받는다 —
# snapshot_download 로 받으면 onnx/openvino 변형까지 딸려와 이미지가 부푼다.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer('nlpai-lab/KURE-v1', device='cpu'); \
m.max_seq_length = 1024; \
print('baked:', m.encode(['워밍업']).shape)"

# kiwipiepy 사전(BM25 토큰화)도 첫 tokenize 에서 로딩된다. 빌드에서 한 번 깨워
# 사전 파일이 휠 안에 실제로 있는지 여기서 확인한다.
RUN python -c "from kiwipiepy import Kiwi; print('kiwi:', len(Kiwi().tokenize('워밍업')))"

# ════════════════════════════════════════════════════════════════════
# 2단계 — 런타임
# ════════════════════════════════════════════════════════════════════
FROM python:3.10-slim

ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=2 \
    SUDDOE_MOCK=0

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf /opt/hf

WORKDIR /app
# server/ 는 scripts/ 를 sys.path 에 넣고 orchestrate 를 import 한다 (main.py:59).
# 둘은 한 덩어리다 — scripts/ 를 빼면 판정 경로가 통째로 없다.
COPY server/ /app/server/
COPY scripts/ /app/scripts/
COPY 2026_Finance_DATA_FOR_RAG/_비목_어휘집.json /app/2026_Finance_DATA_FOR_RAG/

# 빌드가 「임포트는 된다」까지는 증명하게 한다. DB 접속은 하지 않는다(환경변수만 읽는다).
RUN python -c "import server.main; print('import ok')"

RUN useradd -m -u 10001 suddoe \
 && mkdir -p /app/_l3_업로드 \
 && chown -R suddoe:suddoe /app/_l3_업로드
USER suddoe

EXPOSE 8080
# Cloud Run 이 $PORT 를 준다. exec 로 넘겨야 SIGTERM 이 uvicorn 에 닿는다
# (안 그러면 배포 교체 때 진행 중인 판정이 강제 종료된다).
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
