# -*- coding: utf-8 -*-
"""작업에 맞는 최저가 GPU 고르기.

가격·재고는 계속 변한다. 그래서 표로 박지 않고 **실행 시점에 조회**한다
(`GPU Guideline.md` §0 — "GPU 재고와 가격은 변하니 착수 전 다시 본다").

🔴 제일 싼 게 항상 정답이 아니다. A40 에서 한 번 데였다 — 싼데 네트워크 볼륨을
지원하는 DC 에 없어서 못 썼다. 그래서 가격만 보지 않고 제약을 같이 건다.

사용:
    export RUNPOD_API_KEY=...
    python scripts/pick_gpu.py --task embed          # 작업 프리셋
    python scripts/pick_gpu.py --min-vram 24         # 직접 지정
    python scripts/pick_gpu.py --task judge --need-volume

전제:  runpodctl >= 2.8.0  (그 아래는 가격 필드가 없다)
       Windows 는 WSL 또는 https://github.com/runpod/runpodctl/releases 의 바이너리
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 작업별 요구 VRAM. 여기 숫자는 **모델 크기에서 나온다** — 가격이 아니라.
#   가중치(fp16) + 활성값(batch·seq) + 여유
TASKS = {
    "embed": dict(
        min_vram=8,
        why="KURE-v1 = bge-m3 계열 568M. fp16 가중치 1.1GB + 활성값 2~4GB. "
            "batch 를 줄이면 더 내려간다. A100 은 완전한 과잉이다.",
        note="계산이 2~4분이라 GPU 등급이 비용에 거의 영향을 주지 않는다. 재고가 더 중요하다.",
    ),
    "llm8b": dict(
        min_vram=24,
        why="Qwen3 8B bf16 ≈ 16GB + KV 캐시. AWQ 4bit 면 12GB 로도 된다.",
        note="① 정규화·⑤ 문장생성 슬롯.",
    ),
    "judge": dict(
        min_vram=48,
        why="Qwen3 32B AWQ 4bit ≈ 20GB + 투표 N=3~5 의 동시 시퀀스 KV 캐시. "
            "bf16 원본은 66GB 라 80GB 카드가 필요하다.",
        note="④-b 판정 조립. `GPU Guideline.md` 가 정본이다.",
    ),
}


def gpu_list(include_unavailable: bool = False) -> list[dict]:
    if not shutil.which("runpodctl"):
        sys.exit("runpodctl 이 없다.\n"
                 "  설치: curl -sSL https://cli.runpod.net | bash   (Linux/macOS/WSL)\n"
                 "  Windows 바이너리: https://github.com/runpod/runpodctl/releases")
    cmd = ["runpodctl", "gpu", "list"]
    if include_unavailable:
        cmd.append("--include-unavailable")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        try:
            err = json.loads(p.stderr)
            code = err.get("code")
        except Exception:
            sys.exit(f"runpodctl 실패: {p.stderr.strip()[:300]}")
        if code == "no_credentials":
            sys.exit("RUNPOD_API_KEY 가 없다. https://console.runpod.io/user/settings 에서 발급.")
        sys.exit(f"runpodctl 실패 [{code}]: {err.get('error')}")
    data = json.loads(p.stdout)
    return data if isinstance(data, list) else data.get("gpuTypes") or data.get("data") or []


def vram_of(g: dict) -> int:
    for k in ("memoryInGb", "vram", "memoryInGB", "memory"):
        v = g.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _api_key() -> str:
    k = os.environ.get("RUNPOD_API_KEY")
    if k:
        return k
    cfg = Path.home() / ".runpod" / "config.toml"
    if cfg.exists():
        m = re.search(r"apikey\s*=\s*['\"]([^'\"]+)['\"]", cfg.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    sys.exit("RUNPOD_API_KEY 가 없다. https://console.runpod.io/user/settings 에서 발급 후 "
             "`runpodctl doctor` (별도 터미널) 또는 ~/.runpod/config.toml 에 기입.")


def 볼륨되는_DC() -> set[str]:
    """네트워크 볼륨을 지원하는 DC 집합.

    🔴 runpodctl 은 이 정보를 어떤 명령으로도 주지 않는다 (2026-08-30, v2.12.0 확인).
    `gpu list` 의 dataCenterAvailability 는 dataCenterId·stockStatus 뿐이고
    `datacenter list` 는 id·name·location·gpuAvailability 뿐이다.
    그래서 GraphQL 의 dataCenters.storageSupport 로 직접 간다.

    실패하면 조용히 빈 집합을 주지 않는다 — 그러면 `--need-volume` 이 "조건에 맞는
    GPU 가 없다" 로 보여서 재고 문제와 구분이 안 된다.
    """
    req = urllib.request.Request(
        "https://api.runpod.io/graphql?api_key=" + _api_key(),
        data=json.dumps({"query": "query{dataCenters{id storageSupport}}"}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"볼륨 DC 조회 실패 (HTTP {e.code}): {e.read()[:200]!r}")
    except Exception as e:
        sys.exit(f"볼륨 DC 조회 실패: {e}")
    if d.get("errors"):
        sys.exit(f"볼륨 DC 조회 실패: {d['errors'][0].get('message')}")
    dcs = {x["id"] for x in (d.get("data") or {}).get("dataCenters") or [] if x.get("storageSupport")}
    if not dcs:
        sys.exit("볼륨 지원 DC 가 하나도 없다고 나왔다 — 스키마가 바뀐 것 같다. 확인 필요.")
    return dcs


def 재고있는_DC(g: dict, vol_dcs: set[str] | None, dc_filter: str | None) -> list[str]:
    out = []
    for d in g.get("dataCenterAvailability") or []:
        dc = d.get("dataCenterId") or d.get("id") or ""
        stock = str(d.get("stockStatus") or d.get("stock") or "").lower()
        if stock in ("", "none", "unavailable", "out_of_stock"):
            continue
        if dc_filter and dc_filter.upper() not in dc.upper():
            continue
        if vol_dcs is not None and dc not in vol_dcs:
            continue
        out.append(f"{dc}({stock})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=sorted(TASKS))
    ap.add_argument("--min-vram", type=int, help="GB. --task 를 덮어쓴다")
    ap.add_argument("--need-volume", action="store_true",
                    help="네트워크 볼륨을 지원하는 DC 만. 가중치를 재사용할 때만 켠다")
    ap.add_argument("--dc", help="특정 DC 로 한정 (예: EUR-IS-1)")
    ap.add_argument("--community", action="store_true",
                    help="커뮤니티 클라우드 가격도 후보에 넣는다 (더 싸지만 안정성이 낮다)")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    if args.min_vram:
        need, why, note = args.min_vram, "직접 지정", ""
    elif args.task:
        t = TASKS[args.task]
        need, why, note = t["min_vram"], t["why"], t["note"]
    else:
        ap.error("--task 또는 --min-vram 중 하나는 필요하다")

    print(f"요구 VRAM ≥ {need}GB")
    if why:
        print(f"  근거: {why}")
    if note:
        print(f"  참고: {note}")
    if args.need_volume:
        print("  제약: 네트워크 볼륨 지원 DC 만 (A40 함정 — 싼데 볼륨 DC 가 없었다)")
    print()

    vol_dcs = 볼륨되는_DC() if args.need_volume else None

    rows = []
    for g in gpu_list():
        v = vram_of(g)
        if v < need:
            continue
        dcs = 재고있는_DC(g, vol_dcs, args.dc)
        if not dcs:
            continue
        secure = g.get("securePricePerHr")
        comm = g.get("communityPricePerHr")
        cands = [("secure", secure)] + ([("community", comm)] if args.community else [])
        for cloud, price in cands:
            if price is None:
                continue
            # 표에는 displayName("RTX 2000 Ada"), 생성 명령에는 gpuId
            # ("NVIDIA RTX 2000 Ada Generation") — pod create --gpu-id 는 gpuId 만 받는다
            rows.append((float(price), cloud,
                         g.get("displayName") or g.get("gpuId") or "?",
                         g.get("gpuId") or g.get("displayName") or "?", v, dcs))

    if not rows:
        sys.exit("조건에 맞는 GPU 가 없다. --min-vram 을 낮추거나 --dc/--need-volume 을 풀어볼 것.")

    rows.sort()
    print(f"{'$/h':>7}  {'클라우드':9} {'GPU':34} {'VRAM':>5}  재고 DC")
    print("-" * 100)
    for price, cloud, name, gpu_id, v, dcs in rows[:args.top]:
        print(f"{price:7.3f}  {cloud:9} {str(name)[:34]:34} {v:4d}GB  {', '.join(dcs[:3])}")

    p, cloud, name, gpu_id, v, dcs = rows[0]
    est = p * 0.5
    print(f"\n추천: {name}  ({v}GB · {cloud} · ${p}/h)")
    print(f"  30분 기준 약 ${est:.2f}")
    print(f"  생성:  runpodctl pod create --template-id <pytorch> --gpu-id \"{gpu_id}\" --wait")
    print("  🔴 자동종료 플래그는 없다. --terminate-after / --stop-after 는 runpodctl 2.12.0 에")
    print("     존재하지 않고 REST v1 PodCreateInput 스펙에도 해당 필드가 없다 (2026-08-31 확인).")
    print("     팟은 반드시 손으로 닫는다 — `runpodctl pod delete <id>`.")


if __name__ == "__main__":
    main()
