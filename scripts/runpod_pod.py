# -*- coding: utf-8 -*-
"""GPU 팟 열기·보기·닫기.

`runpod_session` 스킬의 실행부. 스킬이 기준 문서이고 여기는 그 절차를 코드로 굳힌 것뿐이다.

🔴 **RunPod 에는 서버측 자동종료가 없다** (2026-08-31 확인).
   `runpodctl 2.12.0` 의 `pod create` 에 `--terminate-after` / `--stop-after` 가 없고,
   REST v1 `PodCreateInput` 스펙(154KB)에도 "After" 를 포함하는 키가 0개다.
   `GPU Guideline.md` §0-b 와 스킬 ②에 적힌 `--terminate-after 1h` 는 **존재하지 않는 플래그다.**

   그래서 가드를 클라이언트로 옮겼다. 세 겹이고, 셋 다 완전하지 않다:

     1. 워치독  — `open` 이 떼어놓는 로컬 프로세스. N시간 뒤 `pod delete`.
                  🔴 PC 가 꺼지거나 절전에 들어가면 같이 죽는다. 그러면 팟은 계속 돈다
     2. 목록    — `.claude/_runpod_open.json`. `ls` 가 여기와 실물을 대조한다
     3. 잔액    — 최후의 방어선. 크레딧이 떨어지면 RunPod 이 멈춘다.
                  손실 상한 = 남은 잔액이지, 무한이 아니다

   1번을 믿지 마라. **작업이 끝나면 사람이 닫는 게 기준 문서이다** (스킬 ④).

사용:
    python scripts/runpod_pod.py ls
    python scripts/runpod_pod.py open --gpu "RTX 2000 Ada" --hours 1 --template-id <id>
    python scripts/runpod_pod.py close            # 목록의 팟 전부
    python scripts/runpod_pod.py close <pod-id>   # 하나만
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 목록 경로는 `runpod_session` 스킬 절대규칙 2 가 기준 문서이다 — 프로젝트 안에 둔다.
# 홈이 아니라 여기인 이유: 컨텍스트가 날아가도 리포를 열면 팟 id 가 보인다.
LEDGER = Path(__file__).resolve().parent.parent / ".claude" / "_runpod_open.json"


# ── runpodctl 얇은 래퍼 ──────────────────────────────────────────────

def rp(*args: str, check: bool = True) -> dict | list:
    """runpodctl 을 부르고 JSON 을 돌려준다.

    실패는 stderr 에 평평한 JSON 한 덩이 + 비0 종료다. `code` 로 갈린다 —
    `status` 로 갈리면 안 된다 (GraphQL 은 not-found 를 HTTP 200 으로 준다).
    """
    if not shutil.which("runpodctl"):
        sys.exit("runpodctl 이 없다. https://github.com/runpod/runpodctl/releases "
                 "에서 windows-amd64.exe 를 받아 ~/bin/runpodctl.exe 로 둔다.")
    p = subprocess.run(["runpodctl", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        try:
            err = json.loads(p.stderr)
        except Exception:
            if check:
                sys.exit(f"runpodctl {' '.join(args)} 실패: {p.stderr.strip()[:300]}")
            return {}
        code = err.get("code")
        if code == "no_credentials":
            sys.exit("RUNPOD_API_KEY 가 없다. 별도 터미널에서 `runpodctl doctor` "
                     "(Claude Code 의 ! 로는 안 된다 — stdin 이 TTY 가 아니다).")
        if code in ("unauthorized", "forbidden"):
            sys.exit(f"키가 있으나 거부됐다 [{code}] — 만료·권한 확인. {err.get('error')}")
        if check:
            sys.exit(f"runpodctl {' '.join(args)} 실패 [{code}]: {err.get('error')}")
        return {}
    return json.loads(p.stdout or "null")


def pods_live() -> list[dict]:
    d = rp("pod", "list")
    return d if isinstance(d, list) else (d or {}).get("pods") or []


# ── 목록 ────────────────────────────────────────────────────────────

def ledger_read() -> list[dict]:
    if not LEDGER.exists():
        return []
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return []


def ledger_write(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def ledger_add(pod_id: str, **meta) -> None:
    rows = [r for r in ledger_read() if r.get("id") != pod_id]
    rows.append({"id": pod_id, "opened_at": datetime.now(timezone.utc).isoformat(), **meta})
    ledger_write(rows)


def ledger_drop(pod_id: str) -> None:
    ledger_write([r for r in ledger_read() if r.get("id") != pod_id])


# ── GPU 이름 해석 ───────────────────────────────────────────────────

def resolve_gpu(name: str) -> str:
    """displayName 도 gpuId 도 받아서 gpuId 를 돌려준다.

    `pod create --gpu-id` 는 gpuId 만 받는다 —
    'RTX 2000 Ada'(displayName) 가 아니라 'NVIDIA RTX 2000 Ada Generation'.
    사람이 `pick_gpu.py` 표에서 눈으로 읽은 이름을 그대로 붙여넣어도 되게 한다.

    🔴 `--include-unavailable` 이 필요하다. 기본 목록은 재고 없는 GPU 를 숨기므로
    이름 해석이 재고에 따라 흔들린다 — 실제로 `pick_gpu.py` 가 추천한 지 몇 분 만에
    같은 GPU 가 목록에서 사라져 "못 찾았다" 가 났다. 이름 해석과 재고는 별개 문제다.
    재고가 없으면 `pod create` 가 알려준다.
    """
    d = rp("gpu", "list", "--include-unavailable")
    gpus = d if isinstance(d, list) else (d or {}).get("gpuTypes") or []
    for g in gpus:
        if name == g.get("gpuId"):
            return name
    for g in gpus:
        if name.lower() == str(g.get("displayName") or "").lower():
            return g["gpuId"]
    near = [g.get("displayName") for g in gpus
            if name.lower() in str(g.get("displayName") or "").lower()]
    sys.exit(f"GPU '{name}' 를 못 찾았다." + (f" 비슷한 것: {near}" if near else
             " `python scripts/pick_gpu.py --task <t>` 로 이름을 확인할 것."))


# ── 명령 ────────────────────────────────────────────────────────────

def cmd_ls(args) -> None:
    live = pods_live()
    led = {r["id"]: r for r in ledger_read()}

    if not live:
        print("도는 팟 없음.")
    else:
        print(f"{'ID':22} {'상태':14} {'GPU':30} {'$/h':>6}  연 경과")
        print("-" * 92)
    total = 0.0
    for p in live:
        pid = p.get("id", "?")
        rt = p.get("runtimeStatus") or p.get("desiredStatus") or "?"
        gpu = (p.get("machine") or {}).get("gpuDisplayName") or p.get("gpuTypeId") or "?"
        rate = float(p.get("costPerHr") or 0)
        total += rate
        r = led.get(pid)
        if r:
            dt = (datetime.now(timezone.utc)
                  - datetime.fromisoformat(r["opened_at"])).total_seconds() / 3600
            elapsed = f"{dt:.2f}h  ≈${dt * rate:.2f}"
        else:
            elapsed = "대장에 없음"
        print(f"{pid:22} {rt:14} {str(gpu)[:30]:30} {rate:6.3f}  {elapsed}")

    if live:
        print(f"\n합계 ${total:.3f}/h")
        stray = [p.get("id") for p in live if p.get("id") not in led]
        if stray:
            print(f"🔴 대장에 없는 팟 {len(stray)}개: {stray}")
            print("   이 스크립트를 거치지 않고 열렸거나, 대장이 지워졌다. 필요 없으면 닫을 것.")

    ghosts = [i for i in led if i not in {p.get("id") for p in live}]
    if ghosts:
        print(f"\n대장에만 있고 실물이 없는 항목 {len(ghosts)}개 — 정리한다: {ghosts}")
        for i in ghosts:
            ledger_drop(i)


def cmd_open(args) -> None:
    # 🔴 CPU 팟 — 볼륨에 venv 를 까는 것처럼 GPU 가 필요 없는 준비 작업용.
    #    L40 $0.82/h 로 15분짜리 pip install 을 돌리면 $0.21 이고, 버전 충돌로
    #    실패해도 그 값을 낸다 (2026-08-31 에 두 번 겪었다). CPU 는 1/5~1/10 이다.
    #    볼륨은 CPU 팟에도 똑같이 붙는다.
    if args.cpu:
        return cmd_open_cpu(args)
    if not args.gpu:
        sys.exit("--gpu 가 필요하다 (CPU 팟이면 --cpu 를 줄 것). "
                 "이름은 `python scripts/pick_gpu.py --task <t>` 로 확인한다.")
    if not args.template_id and not args.image:
        sys.exit("--template-id 또는 --image 중 하나는 필요하다. "
                 "`runpodctl template search pytorch` 로 찾는다.")

    gpu_id = resolve_gpu(args.gpu)
    cmd = ["pod", "create", "--gpu-id", gpu_id, "--wait",
           "--wait-timeout", args.wait_timeout]
    if args.template_id:
        cmd += ["--template-id", args.template_id]
    if args.image:
        cmd += ["--image", args.image]
    if args.name:
        cmd += ["--name", args.name]
    if args.dc:
        cmd += ["--data-center-ids", args.dc]
    if args.volume_id:
        # 🔴 볼륨과 SSH 키는 생성 시점에만 붙는다. 나중에 못 붙인다
        cmd += ["--network-volume-id", args.volume_id]
    if args.ports:
        cmd += ["--ports", args.ports]
    if args.container_disk:
        cmd += ["--container-disk-in-gb", str(args.container_disk)]
    if args.docker_args:
        # 🔴 RunPod REST `dockerStartCmd` — Pod 객체에 영구 저장된다(2026-09-04 openapi
        # 확인). «완전 무인」의 전제: 볼륨과 짝지으면 stop→start 뒤에도 이 커맨드가
        # 다시 실행된다(검증은 다음 GPU 창 — 이번 팟은 볼륨이 없어 여기서 못 잰다).
        # 예: --docker-args "bash /workspace/pod_setup.sh && bash /workspace/pod_serve.sh"
        cmd += ["--docker-args", args.docker_args]

    print(f"여는 중: {gpu_id}  (자동종료 {args.hours}h — 로컬 워치독, 서버 보장 아님)")
    print("  " + " ".join(["runpodctl", *cmd]))
    pod = rp(*cmd)
    if isinstance(pod, list):
        pod = pod[0] if pod else {}
    pid = (pod or {}).get("id")
    if not pid:
        sys.exit(f"팟 id 를 못 읽었다. 응답: {json.dumps(pod, ensure_ascii=False)[:300]}")

    rate = float((pod or {}).get("costPerHr") or 0)
    ledger_add(pid, gpu=gpu_id, hours=args.hours, rate=rate)
    watchdog_spawn(pid, args.hours)

    print(f"\n열렸다: {pid}  ${rate:.3f}/h")
    print(f"  접속:  runpodctl ssh info {pid}")
    print(f"  로그:  runpodctl pod logs {pid} --follow")
    print(f"  닫기:  python scripts/runpod_pod.py close {pid}")
    print(f"\n🔴 워치독이 {args.hours}h 뒤 지우도록 걸었지만 이 PC 가 꺼지면 같이 죽는다.")
    print("   작업이 끝나면 손으로 닫는 게 정본이다.")


def cmd_open_cpu(args) -> None:
    """CPU 팟. 볼륨 준비 작업 전용이라 GPU 관련 인자를 안 받는다."""
    cmd = ["pod", "create", "--compute-type", "cpu", "--wait",
           "--wait-timeout", args.wait_timeout,
           "--image", args.image or "runpod/base:0.6.2-cpu"]
    if args.name:
        cmd += ["--name", args.name]
    if args.dc:
        cmd += ["--data-center-ids", args.dc]
    if args.volume_id:
        cmd += ["--network-volume-id", args.volume_id]
    if args.container_disk:
        cmd += ["--container-disk-in-gb", str(args.container_disk)]
    if args.instance_id:
        cmd += ["--instance-id", args.instance_id]

    print(f"여는 중: CPU 팟  (자동종료 {args.hours}h — 로컬 워치독, 서버 보장 아님)")
    print("  " + " ".join(["runpodctl", *cmd]))
    pod = rp(*cmd)
    if isinstance(pod, list):
        pod = pod[0] if pod else {}
    pid = (pod or {}).get("id")
    if not pid:
        sys.exit(f"팟 id 를 못 읽었다. 응답: {json.dumps(pod, ensure_ascii=False)[:300]}")
    rate = float((pod or {}).get("costPerHr") or 0)
    ledger_add(pid, gpu="CPU", hours=args.hours, rate=rate)
    watchdog_spawn(pid, args.hours)
    print(f"\n열렸다: {pid}  ${rate:.3f}/h  (CPU)")
    print(f"  접속:  runpodctl ssh info {pid}")
    print(f"  닫기:  python scripts/runpod_pod.py close {pid}")


def cmd_close(args) -> None:
    targets = [args.pod_id] if args.pod_id else [r["id"] for r in ledger_read()]
    if not targets:
        print("대장이 비어 있다. 실물을 본다:")
        cmd_ls(args)
        return

    for pid in targets:
        rp("pod", "delete", pid, check=False)
        ledger_drop(pid)
        print(f"삭제 요청: {pid}")

    # 🔴 명령이 성공한 것과 팟이 없어진 것은 다른 말이다. 실물로 확인한다
    time.sleep(3)
    still = [p.get("id") for p in pods_live() if p.get("id") in targets]
    if still:
        print(f"\n🔴 아직 남아 있다: {still}")
        print("   몇 초 뒤 `python scripts/runpod_pod.py ls` 로 다시 볼 것. "
              "계속 남으면 콘솔에서 Terminate.")
        sys.exit(1)
    print("\n확인: 대상 팟이 목록에서 사라졌다.")
    bal = rp("user", check=False) or {}
    if "clientBalance" in bal:
        print(f"잔액 ${bal['clientBalance']} · 시간당 지출 ${bal.get('currentSpendPerHr', 0)}")


# ── roundtrip — gpu_watchdog.RunPod팟 실물 검증 (Q2, 2026-09-04) ──────

def cmd_roundtrip(args) -> None:
    """`gpu_watchdog.RunPod팟` 이 실제 팟에서도 돌아가는지 — stop→status→start→status
    를 찍고 끝난다. **판단은 안 넣는다** — 창이 열렸을 때 15분 안에 끝나야 해서
    대화형 분기가 없다. 사람/중앙이 산출 JSON 을 보고 판단한다.

    🔴 `RUNPOD_API_KEY` 가 필요하다 — `runpodctl` 이 쓰는 저장된 키와는 별개다
    (`gpu_watchdog.RunPod팟` 은 REST 를 직접 친다, `runpodctl` 을 안 거친다).
    vLLM 은 안 띄운다 — REST 응답 스키마 확인이 이 명령의 전부다 (`pod_serve.sh` 는
    별도로 켠다).

    사전 자가검토(실물 없이 한 것)는 `tests/test_gpu_watchdog.py`
    `test_상태해석_실물_스키마_커버리지` 참고 — RunPod 공식 OpenAPI 스키마 기준
    분기는 다 섰다. 이 명령이 확인하는 건 그 다음 단계: **타이밍** —
    `desiredStatus` 가 "expected"(목표) 라 실제 부팅 전에 RUNNING 을 줄 수도 있다는
    가설을 실물로 본다(`--settle` 대기 전/후를 같이 찍는 이유).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from server.gpu_watchdog import RunPod팟          # noqa: E402  (여기서만 쓴다)

    키 = os.environ.get("RUNPOD_API_KEY", "")
    if not 키:
        sys.exit("RUNPOD_API_KEY 가 없다. gpu_watchdog.RunPod팟 은 REST 를 직접 치므로 "
                  "runpodctl 의 저장된 키와 별개로 이 env 가 있어야 한다.")

    팟 = RunPod팟(키, args.pod_id)
    단계들: list[dict] = []

    def 찍기(이름: str, 값) -> None:
        단계들.append({"단계": 이름, "값": 값,
                     "시각": datetime.now(timezone.utc).isoformat()})
        print(f"[{이름}] {값}")

    찍기("초기상태", 팟.상태())
    찍기("정지호출결과", 팟.정지())
    찍기("정지직후상태", 팟.상태())
    if args.settle:
        print(f"  {args.settle}초 대기 (상태 반영 지연 관찰용)...")
        time.sleep(args.settle)
        찍기(f"정지후_{args.settle}s_상태", 팟.상태())
    찍기("시작호출결과", 팟.시작())
    찍기("시작직후상태", 팟.상태())
    if args.settle:
        time.sleep(args.settle)
        찍기(f"시작후_{args.settle}s_상태", 팟.상태())

    out = {"pod_id": args.pod_id, "단계": 단계들, "결론": None}   # 결론은 사람이 채운다
    산출 = Path(__file__).resolve().parent.parent / "scratchpad" / "Q2_roundtrip_result.json"
    산출.parent.mkdir(parents=True, exist_ok=True)
    산출.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n산출: {산출}")
    print("🔴 판단은 안 넣었다 — 정지직후상태가 실제로 «중지» 인지, 시작직후상태가 "
          "settle 전/후로 다른지(타이밍 갭)를 사람이 본다.")


# ── 워치독 ──────────────────────────────────────────────────────────

def watchdog_spawn(pod_id: str, hours: float) -> None:
    """N시간 뒤 팟을 지우는 로컬 프로세스를 떼어놓는다. 최선노력일 뿐이다."""
    args = [sys.executable, os.path.abspath(__file__), "_watchdog", pod_id, str(hours)]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def cmd_watchdog(args) -> None:
    time.sleep(float(args.hours) * 3600)
    if any(p.get("id") == args.pod_id for p in pods_live()):
        rp("pod", "delete", args.pod_id, check=False)
    ledger_drop(args.pod_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="RunPod 팟 열기·보기·닫기")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ls", help="도는 팟 + 대장 대조").set_defaults(fn=cmd_ls)

    o = sub.add_parser("open", help="팟 생성 (ssh 붙을 때까지 기다린다)")
    o.add_argument("--gpu", help="displayName 또는 gpuId. pick_gpu.py 출력 그대로. --cpu 면 불필요")
    o.add_argument("--hours", type=float, default=1.0, help="워치독 자동종료. 예상 시간의 2배")
    o.add_argument("--template-id")
    o.add_argument("--image")
    o.add_argument("--name")
    o.add_argument("--dc", help="데이터센터 한정 (볼륨이 있으면 그 DC 여야 한다)")
    o.add_argument("--volume-id", help="네트워크 볼륨. 생성 시점에만 붙는다")
    o.add_argument("--ports", help="예: '8888/http,22/tcp'. 나중에 못 바꾼다")
    o.add_argument("--container-disk", type=int)
    o.add_argument("--docker-args",
                   help="완전 무인용: 팟 (재)기동마다 자동 실행할 커맨드. "
                        "볼륨과 짝지어야 의미있다(볼륨 없으면 재시작마다 디스크가 지워진다)")
    o.add_argument("--wait-timeout", default="10m")
    o.add_argument("--cpu", action="store_true",
                   help="CPU 팟. 볼륨에 venv 를 까는 등 GPU 가 필요 없는 준비 작업용")
    o.add_argument("--instance-id", help="CPU 인스턴스 유형 (예: cpu3g-2-8)")
    o.set_defaults(fn=cmd_open)

    c = sub.add_parser("close", help="팟 삭제 후 실물 확인")
    c.add_argument("pod_id", nargs="?")
    c.set_defaults(fn=cmd_close)

    rt = sub.add_parser("roundtrip",
                         help="REST start/stop 실물 검증 (Q2 전용) — 상태만 찍는다, 판단 없음")
    rt.add_argument("pod_id")
    rt.add_argument("--settle", type=int, default=5,
                     help="각 호출 뒤 상태 재확인 전 대기초 (기본 5, 0=끔)")
    rt.set_defaults(fn=cmd_roundtrip)

    w = sub.add_parser("_watchdog", help=argparse.SUPPRESS)
    w.add_argument("pod_id")
    w.add_argument("hours")
    w.set_defaults(fn=cmd_watchdog)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
