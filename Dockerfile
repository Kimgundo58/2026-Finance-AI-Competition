# syntax=docker/dockerfile:1
# 써도돼요 API 컨테이너 — FastAPI + KURE-v1(CPU 임베딩).
# 빌더 단계에서 모델을 이미지에 굽고, 런타임은 HF_HUB_OFFLINE=1 로 네트워크를 막는다.
# LLM(DashScope / vLLM)은 이 이미지 밖이다.

# ── 1단계 — 의존성 설치 + 모델 굽기 ────────────────────────────────
FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-api.txt /tmp/requirements-api.txt
RUN pip install -r /tmp/requirements-api.txt

# KURE-v1 을 SentenceTransformer 로 실제 로드해 받는다(snapshot_download 는 onnx/openvino 변형까지 딸려온다).
# 캐시 마운트에 받아 두고 이미지로 복사만 한다 — BuildKit 필수(DOCKER_BUILDKIT=1).
ENV HF_HOME=/opt/hf
RUN --mount=type=cache,target=/root/.hfcache,sharing=locked \
    mkdir -p /opt/hf \
 && HF_HOME=/root/.hfcache python -c "\
from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer('nlpai-lab/KURE-v1', device='cpu'); \
m.max_seq_length = 1024; \
print('baked:', m.encode(['워밍업']).shape)" \
 && cp -a /root/.hfcache/. /opt/hf/

# kiwipiepy 사전(BM25 토큰화)이 휠 안에 있는지 빌드에서 확인한다.
RUN python -c "from kiwipiepy import Kiwi; print('kiwi:', len(Kiwi().tokenize('워밍업')))"

# ── 2단계 — 런타임 ─────────────────────────────────────────────────
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
# server/ 는 scripts/ 를 sys.path 에 넣고 import 한다. 둘은 한 덩어리다.
COPY server/ /app/server/
COPY scripts/ /app/scripts/
COPY 2026_Finance_DATA_FOR_RAG/_비목_어휘집.json /app/2026_Finance_DATA_FOR_RAG/

# import 까지는 빌드에서 증명한다. DB 접속은 하지 않는다.
RUN python -c "import server.main; print('import ok')"

RUN useradd -m -u 10001 suddoe \
 && mkdir -p /app/_l3_업로드 \
 && chown -R suddoe:suddoe /app/_l3_업로드
USER suddoe

EXPOSE 8080
# exec 로 넘겨야 SIGTERM 이 uvicorn 에 닿는다.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
