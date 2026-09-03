# -*- coding: utf-8 -*-
"""vLLM 앞에 세우는 계수 프록시 — W-GPU 레인 전용 계측기.

왜 코드를 안 고치고 프록시인가:
  `server/**`·`scripts/**` 는 이 레인에서 읽기 전용이다. 그리고 호출 횟수를
  «앱 안에서» 세면 앱이 스스로를 증명하는 꼴이다. 닻이 달라야 한다
  (CLAUDE.md 「교차검증은 닻이 달라야 한다」). 이 프록시는 앱 밖에서
  HTTP 왕복 자체를 센다 — 앱이 무슨 함수를 부르든 상관없다.

쓰는 법:
    VLLM_TARGET=https://<pod>-8000.proxy.runpod.net python scratchpad/vllm_probe.py
    # 그리고 앱에는  VLLM_URL=http://127.0.0.1:8011  을 준다

    curl -s http://127.0.0.1:8011/__probe/reset     # 카운터 0 으로
    curl -s http://127.0.0.1:8011/__probe/stats     # 지금까지 센 것

기록은 scratchpad/_probe_calls.jsonl 에 한 줄 = 한 왕복.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TARGET = os.environ.get("VLLM_TARGET", "http://127.0.0.1:8000").rstrip("/")
PORT = int(os.environ.get("PROBE_PORT", "8011"))
LOG = Path(__file__).resolve().parent / "_probe_calls.jsonl"

_락 = threading.Lock()
_수 = {"전체": 0, "chat": 0}


def _기록(행: dict) -> None:
    with _락:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(행, ensure_ascii=False) + "\n")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # 기본 stderr 로그를 끈다 — 우리가 찍는다
        pass

    # ── 내부 제어 경로 ────────────────────────────────────────────────
    def _제어(self) -> bool:
        if self.path == "/__probe/stats":
            self._보냄(200, json.dumps(_수).encode())
            return True
        if self.path == "/__probe/reset":
            with _락:
                _수["전체"] = 0
                _수["chat"] = 0
            self._보냄(200, b'{"reset":true}')
            return True
        return False

    def _보냄(self, 코드: int, 몸: bytes, 종류: str = "application/json") -> None:
        self.send_response(코드)
        self.send_header("Content-Type", 종류)
        self.send_header("Content-Length", str(len(몸)))
        self.end_headers()
        self.wfile.write(몸)

    def do_GET(self):
        if self._제어():
            return
        self._중계(b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._중계(self.rfile.read(n) if n else b"")

    def _중계(self, 본문: bytes) -> None:
        t = time.time()
        chat = self.path.endswith("/chat/completions")
        with _락:
            _수["전체"] += 1
            if chat:
                _수["chat"] += 1
            번호 = _수["chat"] if chat else None

        요청 = {}
        if 본문:
            try:
                요청 = json.loads(본문)
            except Exception:
                요청 = {"_파싱실패": 본문[:200].decode("utf-8", "replace")}

        req = urllib.request.Request(
            TARGET + self.path, data=본문 or None,
            headers={"Content-Type": "application/json",
                     "User-Agent": self.headers.get("User-Agent", "suddoe-probe/1.0")},
            method=self.command)
        코드, 몸 = 502, b'{"error":"probe upstream fail"}'
        오류 = None
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                코드, 몸 = r.status, r.read()
        except urllib.error.HTTPError as e:
            코드, 몸 = e.code, e.read()
            오류 = f"HTTP {e.code}"
        except Exception as e:                                # noqa: BLE001
            오류 = f"{type(e).__name__}: {str(e)[:200]}"

        지연 = int((time.time() - t) * 1000)
        응답 = {}
        try:
            응답 = json.loads(몸)
        except Exception:
            pass

        내용 = ""
        try:
            내용 = 응답["choices"][0]["message"]["content"]
        except Exception:
            pass

        if chat:
            프롬 = ""
            try:
                프롬 = 요청["messages"][0]["content"]
            except Exception:
                pass
            _기록({
                "시각": time.strftime("%H:%M:%S"), "번호": 번호, "경로": self.path,
                "모델": 요청.get("model"), "온도": 요청.get("temperature"),
                "최대토큰": 요청.get("max_tokens"),
                "guided_json": bool(요청.get("guided_json")),
                "guided_키": sorted((요청.get("guided_json") or {})
                                   .get("properties", {}).keys()) or None,
                "프롬프트_길이": len(프롬), "프롬프트_앞": 프롬[:400],
                "프롬프트_뒤": 프롬[-400:] if len(프롬) > 400 else "",
                "HTTP": 코드, "지연ms": 지연, "오류": 오류,
                "usage": 응답.get("usage"),
                "종료이유": (응답.get("choices") or [{}])[0].get("finish_reason"),
                # 🔴 전문을 남긴다. 재실행된 정규화가 원본과 «다른가» 를 재려면
                #    잘린 응답으로는 못 센다 (중앙 지시 2026-09-03).
                "응답_전문": 내용,
            })
            print(f"  [{번호}] {self.path}  {코드}  {지연}ms  "
                  f"prompt={len(프롬)}자  guided={bool(요청.get('guided_json'))}",
                  flush=True)
        else:
            print(f"  (·) {self.path}  {코드}  {지연}ms", flush=True)

        self._보냄(코드, 몸)


if __name__ == "__main__":
    print(f"프록시 :{PORT}  →  {TARGET}", flush=True)
    print(f"기록 {LOG}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        print(json.dumps(_수, ensure_ascii=False))
        sys.exit(0)
