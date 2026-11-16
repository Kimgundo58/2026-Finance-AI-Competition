# -*- coding: utf-8 -*-
"""운영 서버에 붙는다: 배포 SHA 확인 → SSH 터널(DB -L, SOCKS5 -D) → 운영 api 컨테이너 env → 외부 호출을 서버 IP 로.

반드시 프로젝트 모듈(scripts/ · server/)을 import 하기 전에 연결() 을 부른다. 모듈이 import 때 env 를 읽는다.
비밀값(키 · DSN 비밀번호)은 이 프로세스 메모리에만 두고 출력 · 파일에 남기지 않는다.

터널이 끊기면(ssh 종료 · 포트 닫힘) 감시 스레드가 3초 안에 같은 포트로 다시 연다. env · DSN 은 그대로 둔다.
ssh 의 stderr 와 시작 · 끊김 · 다시 열기 기록은 debug/실행기록/터널.log 에 붙인다.
"""
from __future__ import annotations

import atexit
import ipaddress
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

작업트리 = Path(__file__).resolve().parents[1]
터널로그 = 작업트리 / "debug" / "실행기록" / "터널.log"
_터널: subprocess.Popen | None = None
_원래소켓 = socket.socket
_잠금 = threading.RLock()          # 메인 스레드의 터널_확인() 과 감시 스레드가 터널을 둘 열지 않게
_재연결: dict | None = None        # {"설정", "DB_IP", "이유", "실패중"} — 연결() 때 저장, 터널_닫기() 때 비움
_감시끝: threading.Event | None = None
_감시주기초 = 3
_atexit_등록 = False


def _ssh(설정) -> list[str]:
    return ["ssh", "-i", os.path.expanduser(설정.SSH키), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "TCPKeepAlive=yes"]


def _추가전용_열기(경로: Path):
    """쓰기 권한 없이 FILE_APPEND_DATA 로만 연다. 이 프로세스 · 동시에 도는 다른 키트가 같이 써도 항상 파일 끝에 붙는다.
    ssh 에 이 핸들을 stderr 로 직접 주지 않는다: Git 의 ssh(MSYS)는 이 핸들에 아무것도 쓰지 못했다(2026-11-12 확인)."""
    경로.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return open(경로, "ab")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # FILE_APPEND_DATA | SYNCHRONIZE, 공유 읽기 · 쓰기 · 삭제, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL
    h = k32.CreateFileW(str(경로), 0x4 | 0x100000, 0x7, None, 4, 0x80, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(h, os.O_APPEND), "ab")


def _로그(포트: int, 글: str) -> None:
    try:
        with _추가전용_열기(터널로그) as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [DB포트 {포트}] {글}\n".encode("utf-8"))
    except OSError:
        pass


def _로그끝(글자수: int = 300) -> str:
    try:
        return 터널로그.read_bytes()[-글자수:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _서버명령(설정, 명령: str, timeout=60) -> str:
    r = subprocess.run(_ssh(설정) + [설정.서버, 명령], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"SSH 실패(코드 {r.returncode}). 허용된 네트워크(핫스팟)인지 확인하세요.\n{r.stderr.strip()[:300]}")
    return r.stdout


def _포트열림(port: int) -> bool:
    with _원래소켓(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _포트_잡힘(port: int) -> bool:
    """누가 127.0.0.1:port 에서 듣고 있으면 True. 접속하지 않고 bind 로만 본다(-L 포트에 접속하면 운영 DB 까지 연결이 한 번 생긴다)."""
    with _원래소켓(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
        return False


def _디버거_제외(t: threading.Thread) -> threading.Thread:
    # VS Code(debugpy) 가 중단점에서 모든 스레드를 세워도 이 스레드는 계속 돈다(debugpy 가 자기 스레드에 붙이는 표시와 같다)
    t.pydev_do_not_trace = True
    t.is_pydev_daemon_thread = True
    return t


def _stderr_받기(p: subprocess.Popen, 포트: int) -> None:
    """ssh stderr 를 끝까지 읽어 터널.log 에 붙인다. 파이프가 차서 ssh 가 막히지 않게 늘 비운다."""
    for 줄 in iter(p.stderr.readline, b""):
        글 = 줄.decode("utf-8", "replace").rstrip()
        if 글:
            _로그(포트, f"ssh(pid {p.pid}): {글}")
    p.stderr.close()


def _끝내기(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except Exception:  # noqa: BLE001
            p.kill()


def _터널_열기(설정, DB_IP: str, 이유: str) -> subprocess.Popen:
    """ssh -L(DB) -D(SOCKS) 를 연다. 두 포트가 열릴 때까지 기다린다. stderr 는 읽는 스레드가 터널.log 에 붙인다."""
    if _포트열림(설정.DB포트) or _포트열림(설정.SOCKS포트):
        raise RuntimeError(f"포트 {설정.DB포트} 또는 {설정.SOCKS포트} 를 다른 프로그램이 씁니다. debug/설정.py 에서 바꾸거나 그 프로그램을 끄세요.")
    _로그(설정.DB포트, f"터널 시작({이유}): -L 127.0.0.1:{설정.DB포트} · -D 127.0.0.1:{설정.SOCKS포트}")
    p = subprocess.Popen(
        _ssh(설정) + ["-N", "-o", "ExitOnForwardFailure=yes",
                     "-L", f"127.0.0.1:{설정.DB포트}:{DB_IP}:5432", "-D", f"127.0.0.1:{설정.SOCKS포트}", 설정.서버],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    읽기 = _디버거_제외(threading.Thread(target=_stderr_받기, args=(p, 설정.DB포트), name="터널stderr", daemon=True))
    읽기.start()
    for _ in range(60):
        if _포트열림(설정.DB포트) and _포트열림(설정.SOCKS포트):
            _로그(설정.DB포트, f"터널 열림(ssh pid {p.pid})")
            return p
        if p.poll() is not None:
            읽기.join(2)
            _로그(설정.DB포트, f"터널이 바로 끝남(ssh pid {p.pid}, 종료 코드 {p.returncode})")
            raise RuntimeError(f"터널이 바로 끝났습니다(ssh 종료 코드 {p.returncode}). 터널.log 끝:\n{_로그끝()}")
        time.sleep(0.25)
    _끝내기(p)
    _로그(설정.DB포트, f"터널을 15초 안에 열지 못함(ssh pid {p.pid}, 종료 코드 {p.returncode})")
    raise RuntimeError("터널을 열지 못했습니다")


def 터널_확인() -> bool:
    """터널이 끊겼으면(ssh 종료 · 포트 닫힘) 같은 포트로 다시 연다. env · DSN 은 다시 받지 않는다. 다시 열었으면 True."""
    global _터널
    with _잠금:
        정보 = _재연결
        if 정보 is None:
            return False
        설정 = 정보["설정"]
        p = _터널
        살아있음 = p is not None and p.poll() is None
        if 살아있음 and _포트_잡힘(설정.DB포트) and _포트_잡힘(설정.SOCKS포트):
            return False
        if not 정보["실패중"]:
            정보["이유"] = (f"ssh 종료 코드 {p.returncode}" if p is not None and not 살아있음
                          else "ssh 는 떠 있으나 포트가 닫힘" if 살아있음 else "ssh 없음")
            _로그(설정.DB포트, f"끊김 감지: {정보['이유']} (ssh pid {p.pid if p else '-'})")
        if 살아있음:
            _끝내기(p)
        _터널 = None
        try:
            _터널 = _터널_열기(설정, 정보["DB_IP"], "다시 열기")
        except Exception as e:  # noqa: BLE001
            if not 정보["실패중"]:
                정보["실패중"] = True
                _로그(설정.DB포트, f"다시 열기 실패: {type(e).__name__} — {_감시주기초}초마다 다시 시도")
                print(f"운영 연결이 끊겼고 다시 열지 못했어요({정보['이유']}). {_감시주기초}초마다 다시 시도합니다. "
                      f"자세한 내용은 {터널로그} 에 있어요.", flush=True)
            return False
        정보["실패중"] = False
        print(f"운영 연결이 끊겨 다시 열었어요({정보['이유']}).", flush=True)
        return True


def _감시(끝: threading.Event) -> None:
    while not 끝.wait(_감시주기초):
        try:
            터널_확인()
        except Exception as e:  # noqa: BLE001  감시 스레드는 죽지 않는다
            print(f"터널 감시 오류: {type(e).__name__}: {e}", flush=True)


def _감시_시작() -> None:
    global _감시끝
    _감시끝 = threading.Event()
    _디버거_제외(threading.Thread(target=_감시, args=(_감시끝,), name="터널감시", daemon=True)).start()


def 터널_닫기() -> None:
    global _터널, _재연결
    if _감시끝 is not None:
        _감시끝.set()
    with _잠금:
        if _터널 is not None:
            _끝내기(_터널)
            _로그(_재연결["설정"].DB포트 if _재연결 else 0, f"터널 닫음(ssh pid {_터널.pid}, 종료 코드 {_터널.returncode})")
        _터널 = None
        _재연결 = None


def 서버경유(켜기: bool, SOCKS포트: int) -> None:
    """외부(공인 IP) 연결만 SOCKS5 로 보낸다. 루프백 · 사설망(터널 · DB)은 직접 연결한다."""
    import socks

    class _경유소켓(socks.socksocket):
        def connect(self, dest_pair, *a, **k):
            try:
                ip = ipaddress.ip_address(dest_pair[0])
                직접 = ip.is_loopback or ip.is_private or ip.is_link_local
            except ValueError:
                직접 = dest_pair[0] == "localhost"
            if 직접:
                self.set_proxy(None)
            return super().connect(dest_pair, *a, **k)

    if 켜기:
        socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", SOCKS포트, rdns=True)
        socket.socket = _경유소켓
    else:
        socket.socket = _원래소켓


def 연결(설정) -> dict:
    """운영에 연결하고 {운영SHA, 코드SHA, 코드고침} 을 돌려준다. 터널 감시 스레드를 띄운다."""
    global _터널, _재연결, _atexit_등록
    원본 = _서버명령(설정,
        "echo SHA=$(cat /var/lib/suddoe/deployed_sha); "
        "echo DBIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' suddoe-prod-db-1); "
        "echo APICK=$(docker exec suddoe-prod-api-1 cat /run/suddoe-secrets/apick.key); "
        "docker exec suddoe-prod-api-1 env")
    값 = dict(줄.split("=", 1) for 줄 in 원본.splitlines() if "=" in 줄)
    운영SHA, DB_IP = 값["SHA"].strip(), 값["DBIP"].strip()

    git = lambda *a: subprocess.run(["git", *a], cwd=작업트리, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    코드SHA = git("rev-parse", "HEAD")
    # 실행에 쓰는 폴더만 본다(법령 PDF 의 XML 은 체크아웃만 해도 줄끝 차이로 수정됨으로 보인다)
    코드고침 = git("status", "--short", "--", "scripts", "server", "ontology", "2026_Finance_DATA_FOR_RAG", "db")

    터널_닫기()
    with _잠금:
        _터널 = _터널_열기(설정, DB_IP, "처음")
        _재연결 = {"설정": 설정, "DB_IP": DB_IP, "이유": "", "실패중": False}
    if not _atexit_등록:
        atexit.register(터널_닫기)
        _atexit_등록 = True
    _감시_시작()

    # 운영 api 컨테이너 env → 이 프로세스. 로컬 전용 값(DSN · 키 파일 · 관리자 비밀)은 옮기지 않는다
    건너뜀 = {"SUDDOE_DSN", "SUDDOE_INGEST_DSN", "SUDDOE_APICK_API_KEY_FILE", "SUDDOE_ATTACH_DIR", "SUDDOE_ADMIN_TOKEN",
            "SUDDOE_DEMO_SECRET", "SUDDOE_OCR_SECRET"}
    for k, v in 값.items():
        if (k.startswith("SUDDOE_") or k == "DASHSCOPE_API_KEY") and k not in 건너뜀:
            os.environ[k] = v
    # 설정.기본모델 · 설정.판정모델 을 주면 운영 env 대신 그 값으로 돈다(모델 교체 · 판정 앙상블을 운영 반영 전에 본다).
    # llm_qwen 이 import 때 기본 모델을 읽으므로 여기서(모듈 import 전) 넣는다. 판정모델 "" 은 단일 모델(앙상블 끔)
    for 칸, 키 in (("기본모델", "SUDDOE_QWEN_MODEL"), ("판정모델", "SUDDOE_JUDGE_MODELS")):
        if getattr(설정, 칸, None) is not None:
            os.environ[키] = getattr(설정, 칸)
    # 기본모델을 주면 운영 env 의 단계별 모델(질의변환 · 질문 · 조사 · 분류)도 지워 모든 단계가 기본모델로 돈다
    if getattr(설정, "기본모델", None):
        for 키 in ("SUDDOE_QUERY_MODEL", "SUDDOE_QUESTION_MODEL", "SUDDOE_RESEARCH_MODEL", "SUDDOE_CLASSIFY_MODEL"):
            os.environ.pop(키, None)
    os.environ.pop("SUDDOE_APICK_API_KEY_FILE", None)
    os.environ["SUDDOE_APICK_API_KEY"] = 값["APICK"].strip().removeprefix("SUDDOE_APICK_API_KEY=")
    dsn = 값["SUDDOE_DSN"].replace("@db:5432/", f"@127.0.0.1:{설정.DB포트}/")
    dsn += ("&" if "?" in dsn else "?") + "options=-c%20default_transaction_read_only%3Don"   # 운영 DB 는 항상 읽기 전용
    os.environ["SUDDOE_DSN"] = dsn
    os.environ["SUDDOE_MOCK"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"          # KURE-v1 은 이 PC 의 HF 캐시에서 읽는다(검색 임베딩)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    서버경유(설정.서버IP_경유, 설정.SOCKS포트)
    return {"운영SHA": 운영SHA, "코드SHA": 코드SHA, "코드고침": 코드고침}


def 보이는_env() -> dict:
    """판정에 영향을 주는 운영 env (비밀값 제외)."""
    키 = ["SUDDOE_QWEN_MODEL", "SUDDOE_QWEN_FALLBACK_MODEL", "SUDDOE_QUERY_MODEL", "SUDDOE_QUESTION_MODEL",
         "SUDDOE_QUESTION_TIMEOUT", "SUDDOE_RESEARCH_MODEL", "SUDDOE_CLASSIFY_MODEL", "SUDDOE_CLASSIFY_TIMEOUT",
         "SUDDOE_JUDGE_ANSWERS", "SUDDOE_JUDGE_DEADLINE_SEC", "SUDDOE_JUDGE_MIN_BUDGET_SEC", "SUDDOE_JUDGE_MIN_LLM_CALLS",
         "SUDDOE_JUDGE_STALL_SEC", "SUDDOE_ONTOLOGY_PROGRAMS", "SUDDOE_LLM_BASE_URL",
         # 2026-11-17 판정 앙상블(비어 있으면 SUDDOE_QWEN_MODEL 하나로 판정)
         "SUDDOE_JUDGE_MODELS",
         # 판정에 쓰이는데 빠져 있던 값(v052~v062 PR 이 새로 만든 환경변수는 없다: ddbc10e..efe9653 diff)
         "SUDDOE_JUDGE_MAX_TOKENS", "SUDDOE_JUDGE_HEARTBEAT_SEC", "SUDDOE_RETRIEVAL_MIN_SIMILARITY", "SUDDOE_CATEGORY_MIN_MARGIN"]
    return {k: os.environ.get(k, "") for k in 키}
