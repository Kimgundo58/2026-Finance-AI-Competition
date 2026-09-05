# -*- coding: utf-8 -*-
"""Stage 2-d : KURE-v1 임베딩. **팟 위에서 계산하고, 로컬에서 적재한다.**

두 갈래를 한 파일에 둔 이유는 순서 계약이 하나이기 때문이다 —
`_stage2_chunks.jsonl` 의 줄 순서 = npy 의 행 순서 = chunk_id 순서.
파일이 나뉘면 이 계약이 두 곳에 적히고, 어긋나도 아무 에러 없이 **엉뚱한 조문에
엉뚱한 벡터가 붙는다**. 그래서 `load` 가 jsonl 의 chunk_id 로 직접 UPDATE 한다.

    pod   팟 위에서 실행. jsonl -> npy (fp16)         GPU 2~4분
    load  로컬에서 실행. npy  -> corpus.chunks.embedding

절차 (`.claude/skills/runpod_session/SKILL.md` ②③④):
    1. pick_gpu.py --task embed 로 고른다
    2. runpod_pod.py open --gpu <이름> --hours 1 --template-id <pytorch>
    3. runpodctl send scripts/_work/_stage2_chunks.jsonl scripts/archive/indexing/stage2_embed.py
       팟에서: runpodctl receive <코드>
       팟에서: pip install -q --break-system-packages sentence-transformers
               python stage2_embed.py pod --in _stage2_chunks.jsonl --out emb.npy
    4. 로컬로 emb.npy 회수 -> python scripts/archive/indexing/stage2_embed.py load --npy emb.npy
    5. 🔴 사용자에게 묻고 runpod_pod.py close

실행:
    python scripts/archive/indexing/stage2_embed.py pod  --in _stage2_chunks.jsonl --out emb.npy
    PYTHONIOENCODING=utf-8 python scripts/archive/indexing/stage2_embed.py load --npy emb.npy
    PYTHONIOENCODING=utf-8 python scripts/archive/indexing/stage2_embed.py load --npy emb.npy --cpu-fallback
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import json
import os
import sys
import time
from pathlib import Path

MODEL = "nlpai-lab/KURE-v1"
DIM = 1024
MAX_SEQ = 1024          # §3-4 분할 임계와 짝. 8192 로 두면 긴 청크에서 매우 느리다


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.open(encoding="utf-8")]


# ── 팟 위에서 ────────────────────────────────────────────────────────────────
def cmd_pod(a) -> None:
    """GPU 팟에서 실행. 로컬 저장소 의존이 없다 — 이 파일 하나만 보내면 된다."""
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    rows = _jsonl(Path(a.inp))
    texts = [r["text"] for r in rows]
    print(f"입력 {len(texts):,}건", flush=True)

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cpu":
        print("⚠️  GPU 가 안 보인다. CPU 로 돌리면 4시간이다 — 팟 설정을 확인할 것.")
    print(f"모델 로딩 {MODEL} on {dev} ...", flush=True)
    model = SentenceTransformer(MODEL, device=dev)
    model.max_seq_length = MAX_SEQ
    if dev == "cuda":
        model = model.half()          # fp16. 568M 이라 8GB 카드에서도 남는다

    t = time.time()
    vecs = model.encode(texts, batch_size=a.batch, normalize_embeddings=True,
                        show_progress_bar=True, convert_to_numpy=True)
    print(f"임베딩 {time.time() - t:.0f}초  shape={vecs.shape}", flush=True)
    assert vecs.shape == (len(texts), DIM), f"모양 불일치 {vecs.shape}"

    np.save(a.out, vecs.astype("float16"))
    mb = Path(a.out).stat().st_size / 1e6
    print(f"저장 {a.out}  ({mb:.0f}MB)")
    print("\n🔴 이 파일을 로컬로 회수한 뒤에 팟을 닫는다. 컨테이너 디스크는 같이 사라진다.")


# ── 로컬에서 ─────────────────────────────────────────────────────────────────
def cmd_load(a) -> None:
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
    import numpy as np
    import psycopg

    root = Path(__file__).resolve().parent.parent
    jsonl = Path(a.jsonl) if a.jsonl else root / "scripts" / "_work" / "_stage2_chunks.jsonl"
    dsn = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

    rows = _jsonl(jsonl)
    vecs = np.load(a.npy).astype("float32")
    if vecs.shape != (len(rows), DIM):
        sys.exit(f"🔴 모양 불일치 — npy {vecs.shape} vs jsonl {len(rows)}행.\n"
                 "   같은 jsonl 로 만든 npy 가 맞는지 확인할 것. 어긋나면 조문마다 "
                 "엉뚱한 벡터가 붙는다.")

    # L2 정규화 재확인. 코사인을 내적으로 계산하는 전제라 여기가 무너지면 순위가 흔들린다.
    norms = np.linalg.norm(vecs, axis=1)
    if abs(norms.mean() - 1.0) > 0.01:
        print(f"  정규화 재적용 (평균 노름 {norms.mean():.4f})")
        vecs = vecs / np.clip(norms, 1e-9, None)[:, None]

    print(f"적재 {len(rows):,}건 ...", flush=True)
    t = time.time()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _emb (chunk_id BIGINT PRIMARY KEY, v TEXT);")
            with cur.copy("COPY _emb (chunk_id, v) FROM STDIN") as cp:
                for r, v in zip(rows, vecs):
                    cp.write_row((r["chunk_id"],
                                  "[" + ",".join(f"{x:.6f}" for x in v) + "]"))
            cur.execute("""
                UPDATE corpus.chunks c
                   SET embedding = _emb.v::extensions.vector(1024)
                  FROM _emb WHERE _emb.chunk_id = c.chunk_id
            """)
            갱신 = cur.rowcount
        conn.commit()
        빈칸 = conn.execute(
            "SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL").fetchone()[0]
    print(f"  UPDATE {갱신:,}행 · 남은 NULL {빈칸}건 · {time.time() - t:.0f}초")
    if 빈칸:
        print("⚠️  임베딩이 빈 청크가 있다. 검색에서 조용히 빠진다 — 원인을 잡을 것.")
    else:
        print("완료.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pod", help="GPU 팟에서 계산 -> npy")
    p.add_argument("--in", dest="inp", default="_stage2_chunks.jsonl")
    p.add_argument("--out", default="emb.npy")
    p.add_argument("--batch", type=int, default=64)
    p.set_defaults(func=cmd_pod)

    l = sub.add_parser("load", help="npy -> corpus.chunks.embedding")
    l.add_argument("--npy", required=True)
    l.add_argument("--jsonl", help="기본값 scripts/_work/_stage2_chunks.jsonl")
    l.set_defaults(func=cmd_load)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
