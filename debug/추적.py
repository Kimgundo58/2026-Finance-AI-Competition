# -*- coding: utf-8 -*-
"""판정 디버깅 기록기.

한 단계(① 정규화, ③ 확인 질문, ④ 판정 …)가 끝날 때마다 debug/실행기록/<시각>/<NN_단계>/ 에 남긴다.
  화면이벤트.jsonl   서버가 화면으로 보낸 SSE 이벤트(진행 문장 포함) 순서대로
  LLM.md            LLM 요청(프롬프트 · 스키마 · 모델) · 출력 · 추론 · 지연 · 토큰
  SQL.md            SQL · Cypher 원문 · 인자 · 행 수 · ms · 부른 함수
  호출트리.txt       scripts/ · server/ 의 모든 함수 호출: 인자 · 반환 · ms (스레드별 들여쓰기)
  호출요약.txt       함수별 호출 수 · 누적 ms
  판정내부/          orchestrate 판정 단계(입력확인 · 규정조회 · …)마다 바뀐 _판정상태 값

운영 보호: DB 는 읽기 전용 연결이고, 서버가 DB 에 쓰는 곳(응답 캐시 · 판정 기록)은 메모리로 돌린다. 판정 기록은
「저장될_판정」 에 남겨 무엇이 저장됐을지 본다.

서버 방식(설정.실행방식 = "서버", 서버_실행):
  흐름기록      ASGI 미들웨어. /api/normalize · /api/questions · /api/judge 한 요청 = 단계 폴더 하나(잠금으로 하나씩).
                요청마다 핫 리로드를 먼저 하고, 같은 요청 · 같은 의존 파일이면 녹화한 응답을 그대로 보낸다(응답 재생, 판정은 늘 실행)
  메모리저장소   프론트가 하는 DB 쓰기(계획 저장 · 판정 연결 · 할 일 · 평가)를 이 프로세스 메모리에 두고 GET 응답에 합친다
  핫리로드      설정.핫리로드_대상 모듈 파일이 바뀌면 importlib.reload → from 으로 가져간 참조 바꾸기 → 키트 패치 다시 걸기
검색_다시_실행: 녹화한 판정 단계의 판정_인자.json 으로 규정조회 · 규정검색 단계만 다시 돌려 녹화와 비교한다(LLM 없음).
"""
from __future__ import annotations

import ast
import asyncio
import copy
import dataclasses
import hashlib
import importlib
import json
import os
import re
import reprlib
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

작업트리 = Path(__file__).resolve().parents[1]
_뿌리 = tuple(os.path.normcase(str(작업트리 / d)) for d in ("scripts", "server"))
_내포 = {"<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"}
_CO_GENERATOR = 0x20


def 스크립트_모드() -> bool:
    """VS Code 디버그 · 터미널 실행이면 True, Interactive 창(ipykernel)이면 False."""
    return "ipykernel" not in sys.modules


def 디버거_붙음() -> bool:
    try:
        import debugpy
        if debugpy.is_client_connected():
            return True
    except Exception:  # noqa: BLE001
        pass
    return sys.gettrace() is not None or "pydevd" in sys.modules


def _json(v, 들여쓰기=2) -> str:
    return json.dumps(v, ensure_ascii=False, indent=들여쓰기,
                      default=lambda o: sorted(o) if isinstance(o, set) else str(o))


class _짧은repr(reprlib.Repr):
    """호출 트리용 짧은 표시. 큰 객체는 전체 repr 을 만들지 않고 타입 이름만 쓴다."""

    def __init__(self, n: int):
        super().__init__()
        self.maxstring = self.maxother = n
        self.maxlist = self.maxtuple = self.maxdict = self.maxset = 5
        self.maxlevel = 2
        self.maxlong = 40

    def repr_instance(self, x, level):
        if x is None or isinstance(x, (bool, int, float, complex)):
            return repr(x)
        if hasattr(x, "model_dump"):                          # pydantic 요청 모델
            try:
                return f"{type(x).__name__}{self.repr1(x.model_dump(exclude_defaults=True), level - 1)}"
            except Exception:  # noqa: BLE001
                pass
        if hasattr(x, "isoformat"):
            return x.isoformat()
        return f"<{type(x).__name__}>"


class 기록장:
    def __init__(self, 설정):
        self.설정 = 설정
        self.폴더 = 작업트리 / "debug" / "실행기록" / time.strftime("%Y%m%d_%H%M%S")
        self.폴더.mkdir(parents=True, exist_ok=True)
        self.단계번호 = 0
        self.현재단계 = "00_준비"
        self.단계폴더 = self.폴더 / self.현재단계
        self.LLM: list[dict] = []
        self.SQL: list[dict] = []
        self.이벤트: list[dict] = []
        self.판정내부: list[dict] = []
        self.호출: list[dict] = []
        self.저장될_판정: list[dict] = []
        self.막힌_쓰기: list[dict] = []
        self.판정_인자: dict | None = None
        self.판정_결과: dict | None = None                  # orchestrate.판정() 반환(처리단계 · 실패단계 · 검증코드)
        self.재시도: list[dict] = []                         # 시스템 오류 재시도(v053, research_runtime.재시도_기록) 전부
        self.분류디버그: list[dict] = []                     # ③ routes_questions._분류 의 디버그(변형 A · B · C · 불일치 · 조정)
        # 실험 훅. 프롬프트_고치기(호출위치, 프롬프트) -> 프롬프트 를 넣으면 다음 LLM 호출부터 바뀐 프롬프트가 나간다
        self.프롬프트_고치기 = None
        self.모델_바꾸기: dict[str, str] = {}               # {"호출위치에 든 글": "모델명"}. 판정 앙상블 모델은 설정.판정모델 로 바꾼다
        self.LLM잠금 = threading.Lock()                     # 판정 앙상블은 모델마다 스레드에서 동시에 부른다. LLM 번호가 겹치지 않게
        self.캐시저장: list[dict] = []                       # response_cache.저장 호출(응답 재생 때 다시 넣는다)
        self.메모리: 메모리저장소 | None = None              # 서버 방식에서만: 계획 · 판정 · 할 일 메모리 저장소
        self.추적기 = 호출추적기(self, 설정.호출추적_인자길이)

    # ── 단계 ──────────────────────────────────────────────
    @contextmanager
    def 단계(self, 이름: str):
        self.단계번호 += 1
        self.현재단계 = f"{self.단계번호:02d}_{이름}"
        self.단계폴더 = self.폴더 / self.현재단계
        self.단계폴더.mkdir(parents=True, exist_ok=True)
        시작 = (len(self.LLM), len(self.SQL), len(self.이벤트), len(self.판정내부), len(self.재시도))
        self.호출 = []
        self.추적기.시작(self.설정.호출추적)
        print(f"\n━━━━━━━━━━━━ {self.현재단계} ━━━━━━━━━━━━")
        t = time.time()
        try:
            yield self
        finally:
            self.추적기.멈춤()
            self._단계_저장(시작, time.time() - t)

    def _단계_저장(self, 시작, 초: float):
        llm, sql, ev, 내부 = (self.LLM[시작[0]:], self.SQL[시작[1]:], self.이벤트[시작[2]:], self.판정내부[시작[3]:])
        재시도 = self.재시도[시작[4]:]
        d = self.단계폴더
        if 재시도:
            (d / "재시도.json").write_text(_json(재시도), encoding="utf-8")
        (d / "화면이벤트.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in ev), encoding="utf-8")
        if llm:
            (d / "LLM.md").write_text("\n\n".join(_LLM_md(r) for r in llm), encoding="utf-8")
        if sql:
            (d / "SQL.md").write_text(_SQL_md(sql), encoding="utf-8")
        if self.호출:
            (d / "호출트리.txt").write_text(_호출트리(self.호출), encoding="utf-8")
            (d / "호출요약.txt").write_text(_호출요약(self.호출), encoding="utf-8")
        llm_ms = sum(r.get("지연ms") or 0 for r in llm)
        실패 = sum(1 for r in llm if r.get("오류"))
        print(f"── 기록: LLM {len(llm)}회({llm_ms:,}ms{f', 실패 {실패}' if 실패 else ''}) · SQL {len(sql)}개 · "
              f"함수 호출 {len(self.호출):,}개 · 화면 이벤트 {len(ev)}개{f' · 재시도 {len(재시도)}번' if 재시도 else ''} · {초:.1f}초")
        print(f"   {d}")

    # ── 화면 이벤트(SSE) ───────────────────────────────────
    def 이벤트_받음(self, 이름: str, 값):
        self.이벤트.append({"단계": self.현재단계, "이벤트": 이름, "값": 값, "t": round(time.time(), 3)})
        if 이름 == "진행" and isinstance(값, dict):
            상태 = 값.get("상태") or ""
            표시 = {"진행중": "…", "완료": "✓", "실패": "✗"}.get(상태, 상태)
            print(f"  [화면] {표시} {값.get('문장') or ''}   ({값.get('단계') or 값.get('id') or ''})")
        elif 이름 == "필드":
            print(f"  [화면 필드] {_json(값, None)[:200]}")
        elif 이름 in ("오류", "완료", "저장", "판정", "문의초안"):
            print(f"  [화면 {이름}] {_json(값, None)[:300]}")
        else:
            print(f"  [화면 {이름}] ({len(_json(값, None)):,}자)")


# ── 호출 추적 ───────────────────────────────────────────────
class 호출추적기:
    """sys.setprofile 로 scripts/ · server/ 함수의 call · return 을 기록한다. 라이브러리 함수는 건너뛴다."""

    def __init__(self, 기록: 기록장, 인자길이: int):
        self.기록 = 기록
        self.켜짐 = False
        self._스택: dict[int, list] = {}
        self._판별: dict = {}
        self.코드: set = set()            # 이번 단계에서 실행된 scripts/ · server/ 코드 객체(응답 재생의 의존 정의)
        self._repr = _짧은repr(인자길이)
        self._최대 = 인자길이 * 4

    def 설치(self):
        """스레드가 만들어지기 전에 한 번 부른다(threading.setprofile 은 이후 스레드에만 걸린다)."""
        sys.setprofile(self._p)
        threading.setprofile(self._p)

    def 시작(self, 켜기: bool):
        self._스택 = {}
        self.코드 = set()
        self.켜짐 = 켜기

    def 멈춤(self):
        self.켜짐 = False

    def _짧게(self, v) -> str:
        try:
            s = self._repr.repr(v)
        except Exception as e:  # noqa: BLE001
            s = f"<repr 실패 {type(e).__name__}>"
        return s if len(s) <= self._최대 else s[:self._최대] + "…"

    def _p(self, frame, event, arg):
        if not self.켜짐 or (event != "call" and event != "return"):
            return
        try:
            code = frame.f_code
            ok = self._판별.get(code)
            if ok is None:
                ok = os.path.normcase(code.co_filename).startswith(_뿌리) and code.co_name not in _내포
                self._판별[code] = ok
            if not ok:
                return
            tid = threading.get_ident()
            st = self._스택.setdefault(tid, [])
            if event == "call":
                self.코드.add(code)
                n = code.co_argcount + code.co_kwonlyargcount
                이름들 = list(code.co_varnames[:n])
                if code.co_flags & 0x04:
                    이름들.append(code.co_varnames[n]); n += 1
                if code.co_flags & 0x08:
                    이름들.append(code.co_varnames[n])
                loc = frame.f_locals
                인자 = ", ".join(f"{k}={self._짧게(loc[k])}" for k in 이름들 if k in loc and k != "self")
                파일 = Path(code.co_filename)
                rec = {"번호": len(self.기록.호출) + 1, "스레드": threading.current_thread().name, "깊이": len(st),
                       "함수": f"{'server.' if 파일.parent.name == 'server' else ''}{파일.stem}.{code.co_name}",
                       "줄": frame.f_lineno, "인자": 인자[:self._최대 * 2],
                       "제너레이터": bool(code.co_flags & _CO_GENERATOR)}
                self.기록.호출.append(rec)
                st.append((code, time.perf_counter(), rec))
            else:
                while st:
                    c, t0, rec = st.pop()
                    if c is code:
                        rec["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                        rec["반환"] = self._짧게(arg)
                        break
        except Exception:  # noqa: BLE001  (프로파일 함수가 예외를 내면 파이썬이 추적을 꺼 버린다)
            return


def _호출트리(호출: list[dict]) -> str:
    줄 = []
    for 스레드 in dict.fromkeys(r["스레드"] for r in 호출):
        줄.append(f"\n===== 스레드 {스레드} =====")
        for r in 호출:
            if r["스레드"] != 스레드:
                continue
            표시 = "↻" if r["제너레이터"] else ""
            반환 = f" → {r['반환']}" if "반환" in r else " → (끝나지 않음/예외)"
            ms = f"  [{r['ms']}ms]" if "ms" in r else ""
            줄.append(f"{'│ ' * r['깊이']}{표시}{r['함수']}:{r['줄']}({r['인자']}){반환}{ms}")
    return "\n".join(줄)


def _호출요약(호출: list[dict]) -> str:
    합: dict[str, list] = {}
    for r in 호출:
        s = 합.setdefault(r["함수"], [0, 0.0])
        s[0] += 1
        s[1] += r.get("ms") or 0
    행 = sorted(합.items(), key=lambda kv: -kv[1][1])
    return "누적ms(하위 호출 포함)  호출수  함수\n" + "\n".join(f"{ms:>12,.1f}  {n:>6}  {f}" for f, (n, ms) in 행)


# ── LLM · SQL 기록 ──────────────────────────────────────────
def _호출위치(건너뛸=("llm_qwen.py",), 최대=4) -> str:
    체인, f = [], sys._getframe(2)
    while f is not None and len(체인) < 최대:
        fn = f.f_code.co_filename
        if os.path.normcase(fn).startswith(_뿌리) and not fn.endswith(건너뛸):
            체인.append(f"{Path(fn).stem}.{f.f_code.co_name}")
        f = f.f_back
    return " ← ".join(체인) or "?"


class _스트림_기록:
    """stream=True 응답을 감싼다. llm_qwen 이 조각을 읽는 동안 내용 · 추론 · 토큰을 모으고, 끝나거나 끊기면 rec 에 남긴다."""

    def __init__(self, s, rec: dict, t: float):
        self._s, self._rec, self._t = s, rec, t
        self._내용, self._추론, self._u, self._종료, self._첫 = [], [], None, None, None

    def __iter__(self):
        try:
            for ch in self._s:
                if self._첫 is None:
                    self._첫 = int((time.time() - self._t) * 1000)
                if getattr(ch, "usage", None):
                    self._u = ch.usage
                for c in ch.choices or ():
                    d = c.delta
                    if d is not None:
                        if d.content:
                            self._내용.append(d.content)
                        조각 = getattr(d, "reasoning_content", None) or (getattr(d, "model_extra", None) or {}).get("reasoning")
                        if 조각:
                            self._추론.append(str(조각))
                    if c.finish_reason:
                        self._종료 = c.finish_reason
                yield ch
        except Exception as e:
            self._rec["오류"] = f"{type(e).__name__}: {str(e)[:2000]}"
            raise
        finally:
            self._끝()

    def _끝(self):
        rec = self._rec
        if "지연ms" in rec:
            return
        u = self._u
        rec.update(출력="".join(self._내용), 추론="".join(self._추론) or None, 종료이유=self._종료,
                   지연ms=int((time.time() - self._t) * 1000), 첫조각ms=self._첫,
                   토큰={"입력": getattr(u, "prompt_tokens", None), "출력": getattr(u, "completion_tokens", None)})
        상태 = f"실패 {rec['오류'][:160]}" if rec.get("오류") else f"토큰 {rec['토큰']} · {rec['종료이유']}"
        print(f"  [LLM #{rec['번호']} 스트림 응답] {rec['지연ms']:,}ms · 첫 조각 {self._첫}ms · {상태}")

    def close(self):
        try:
            self._s.close()
        finally:
            self._끝()


def LLM_기록_설치(기록: 기록장):
    import llm_qwen
    if getattr(llm_qwen._client_얻기, "_디버그", False):
        return

    def 감싸기(client):
        if getattr(client, "_디버그기록", False):
            return client
        원래 = client.chat.completions.create

        def create(**kw):
            위치 = _호출위치()
            msgs = list(kw.get("messages") or [])
            if 기록.프롬프트_고치기 and msgs:
                새글 = 기록.프롬프트_고치기(위치, msgs[0]["content"])
                if 새글 != msgs[0]["content"]:
                    msgs[0] = {**msgs[0], "content": 새글}
                    kw["messages"] = msgs
                    위치 += " [프롬프트 수정됨]"
            for 조각, 모델 in 기록.모델_바꾸기.items():
                if 조각 in 위치:
                    kw["model"] = 모델
            rf = (kw.get("response_format") or {}).get("json_schema") or {}
            with 기록.LLM잠금:
                rec = {"번호": len(기록.LLM) + 1, "단계": 기록.현재단계, "호출위치": 위치, "모델": kw.get("model"),
                       "메시지": msgs, "스키마": rf.get("schema"), "max_tokens": kw.get("max_tokens"),
                       "timeout": kw.get("timeout"), "extra_body": kw.get("extra_body")}
                기록.LLM.append(rec)
            print(f"  [LLM #{rec['번호']} 요청] {위치} · {rec['모델']}")
            t = time.time()
            try:
                r = 원래(**kw)
            except Exception as e:
                rec.update(오류=f"{type(e).__name__}: {str(e)[:2000]}", 지연ms=int((time.time() - t) * 1000))
                print(f"  [LLM #{rec['번호']} 실패] {rec['지연ms']}ms · {rec['오류'][:160]}")
                raise
            if kw.get("stream"):                     # 판정 호출(llm_qwen._스트림_받기): 조각을 다 읽을 때 기록한다
                rec["스트림"] = True
                return _스트림_기록(r, rec, t)
            msg = r.choices[0].message
            추론 = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning")
            u = r.usage
            rec.update(출력=msg.content, 추론=추론, 종료이유=r.choices[0].finish_reason,
                       지연ms=int((time.time() - t) * 1000),
                       토큰={"입력": getattr(u, "prompt_tokens", None), "출력": getattr(u, "completion_tokens", None)})
            print(f"  [LLM #{rec['번호']} 응답] {rec['지연ms']:,}ms · 토큰 {rec['토큰']} · {rec['종료이유']}")
            return r

        client.chat.completions.create = create
        client._디버그기록 = True
        return client

    원래_얻기 = llm_qwen._client_얻기

    def 얻기():
        return 감싸기(원래_얻기())
    얻기._디버그 = True
    llm_qwen._client_얻기 = 얻기


def 앙상블_보기(모델칸: dict | None, 실패사유=None) -> list[dict]:
    """④ 판정 앙상블(orchestrate._판정_앙상블)의 모델별 판정을 찍고 [{모델, 판정, 상태}] 를 돌려준다.
    모델칸 = 판정() 반환의 `모델`(decisions."모델" 과 같은 값). 판정실패면 모델칸이 없어 실패 사유(검증사유) 글을 찍는다."""
    앙 = (모델칸 or {}).get("앙상블")
    if not 앙:
        사유 = next((x for x in 실패사유 or [] if "앙상블" in str(x)), None)
        print(f"  [판정 앙상블] {사유}" if 사유 else
              f"  [판정 앙상블] 없음(단일 모델 {(모델칸 or {}).get('판정') or '-'} · SUDDOE_JUDGE_MODELS 비었거나 mock · 판정 전 종료)")
        return []
    print(f"  [판정 앙상블] {앙.get('방식')} → {앙.get('결론') or '판정실패'} · 대표 출력 {앙.get('대표') or '-'} · 표 {앙.get('표')}")
    for x in 앙.get("모델별") or []:
        토큰 = x.get("토큰") or {}
        print(f"    · {x.get('모델')}: {x.get('판정') or '-'} ({x.get('상태')}"
              f"{' · ' + x['오류'] if x.get('오류') else ''}{' · 형식문제 ' + str(x['형식문제']) if x.get('형식문제') else ''})"
              f" · 토큰 {토큰.get('prompt_tokens')}/{토큰.get('completion_tokens')} · {x.get('지연ms')}ms · 전체 {x.get('전체ms')}ms"
              f" · 시도 {x.get('시도')} · 재요청 {x.get('재요청')}")
    return [{"모델": x.get("모델"), "판정": x.get("판정"), "상태": x.get("상태")} for x in 앙.get("모델별") or []]


def _LLM_md(r: dict) -> str:
    머리 = (f"## LLM #{r['번호']} · {r['호출위치']} · {r['모델']} · {r.get('지연ms')}ms · 토큰 {r.get('토큰')} · "
          f"종료 {r.get('종료이유')}\n\nmax_tokens={r.get('max_tokens')} timeout={r.get('timeout')} extra_body={r.get('extra_body')}")
    조각 = [머리]
    for m in r["메시지"]:
        조각.append(f"### 요청 메시지 ({m.get('role')})\n\n```text\n{m.get('content')}\n```")
    if r.get("스키마"):
        조각.append(f"### 응답 스키마\n\n```json\n{_json(r['스키마'])}\n```")
    if r.get("추론"):
        조각.append(f"### 모델 추론\n\n```text\n{r['추론'] if isinstance(r['추론'], str) else _json(r['추론'])}\n```")
    if r.get("오류"):
        조각.append(f"### 오류\n\n```text\n{r['오류']}\n```")
    else:
        출력 = r.get("출력") or ""
        try:
            출력 = _json(json.loads(출력))
        except Exception:  # noqa: BLE001
            pass
        조각.append(f"### 출력\n\n```json\n{출력}\n```")
    return "\n\n".join(조각)


def _예산에서_빼기(초: float):
    """지금 스레드의 조사 예산이 쓴 시간에서 `초` 를 뺀다(터널 지연 보정). 병렬 SQL 이 겹치면 조금 더 뺄 수 있다."""
    try:
        import research_runtime
        b = research_runtime.current_budget()
        root = getattr(b, "_parent", b)
        if root is not None and hasattr(root, "_lock"):
            with root._lock:
                if root._started is not None:
                    root._started += 초
    except Exception:  # noqa: BLE001
        pass


def 예산_요약(b) -> str:
    if b is None:
        return "예산 없음"
    return (f"예산: LLM {b.llm_used}/{b._llm_limit} · 도구 {b.tools_used}/{b._tools_limit} · "
            f"근거 {b.evidence_used:,}/{b._evidence_limit:,}자 · 남은 시간 {b.remaining_seconds():.0f}/{b._seconds:.0f}초")


def 재시도_기록_설치(기록: 기록장):
    """시스템 오류 재시도(v053) 한 번마다 단계 · 원인 · 시도 · 대기를 출력하고 기록한다. 서버 쪽 기록(요청 예산 `.재시도` →
    판정 행 모델 칸 `재시도`)은 원래 함수가 그대로 남긴다. llm_qwen · _lib.db 는 import 때 이 함수를 이름으로 가져가 그 이름도 바꾼다."""
    import research_runtime
    import llm_qwen
    from _lib import db as _db
    원래 = research_runtime.재시도_기록
    if getattr(원래, "_디버그", False):
        return

    def 재시도_기록(단계: str, 원인: str, 시도: int, 대기초: float = 0.0) -> None:
        기록.재시도.append({"키트단계": 기록.현재단계, "단계": 단계, "원인": str(원인)[:200], "시도": 시도,
                         "대기초": round(대기초, 1), "t": round(time.time(), 3)})
        print(f"  [재시도] {단계} · 원인 {str(원인)[:100]} · {시도}번째 실패 → {대기초:.1f}초 뒤 다시")
        return 원래(단계, 원인, 시도, 대기초)
    재시도_기록._디버그 = True
    research_runtime.재시도_기록 = 재시도_기록
    llm_qwen.재시도_기록 = 재시도_기록
    if hasattr(_db, "_재시도_기록"):
        _db._재시도_기록 = 재시도_기록


def 재시도_보기(목록: list[dict], 머리: str = "재시도") -> None:
    """재시도 기록 목록을 「단계 · 원인 · 횟수 · 대기」 로 묶어 출력한다(요청 예산 `.재시도` · 판정 행 모델.재시도 · 기록장.재시도)."""
    if not 목록:
        print(f"  [{머리}] 없음")
        return
    묶음: dict[tuple, list] = {}
    for x in 목록:
        묶음.setdefault((x.get("단계"), str(x.get("원인") or "").split(":")[0][:60]), []).append(x)
    print(f"  [{머리}] {len(목록)}번")
    for (단계, 원인), xs in 묶음.items():
        print(f"    {단계} · 원인 {원인} · {len(xs)}번 · 대기 {sum(float(x.get('대기초') or 0) for x in xs):.1f}초"
              f" (시도 {[x.get('시도') for x in xs]})")


def SQL_기록_설치(기록: 기록장):
    import psycopg
    if getattr(psycopg.Cursor.execute, "_디버그", False):
        return
    원래 = psycopg.Cursor.execute

    def execute(self, query, params=None, **kw):
        t = time.time()
        오류 = None
        try:
            return 원래(self, query, params, **kw)
        except Exception as e:
            오류 = f"{type(e).__name__}: {str(e)[:500]}"
            raise
        finally:
            if 기록.설정.DB시간_예산에서_빼기:
                _예산에서_빼기(time.time() - t)
            try:
                q = query.as_string(self) if hasattr(query, "as_string") else (
                    query.decode() if isinstance(query, bytes) else str(query))
            except Exception:  # noqa: BLE001
                q = str(query)
            종류 = ("Cypher" if "cypher(" in q else
                  "준비" if q.lstrip().upper().startswith("LOAD") or "set_config(" in q else "SQL")
            try:
                행수 = self.rowcount
            except Exception:  # noqa: BLE001
                행수 = None
            기록.SQL.append({"번호": len(기록.SQL) + 1, "단계": 기록.현재단계, "호출": _호출위치(("_lib\\db.py", "_lib/db.py"), 3),
                            "종류": 종류, "질의": q, "인자": params, "행수": 행수,
                            "ms": round((time.time() - t) * 1000, 1), "오류": 오류})
    execute._디버그 = True
    psycopg.Cursor.execute = execute


def _SQL_md(rows: list[dict]) -> str:
    머리 = ["| # | 종류 | 부른 곳 | 행 | ms | 오류 |", "|---|---|---|---|---|---|"]
    머리 += [f"| {r['번호']} | {r['종류']} | {r['호출']} | {r['행수']} | {r['ms']} | {r['오류'] or ''} |" for r in rows]
    본문 = [f"### #{r['번호']} {r['종류']} · {r['호출']}\n\n```sql\n{r['질의'].strip()}\n```\n\n인자: `{_json(r['인자'], None)[:3000]}`"
          for r in rows if r["종류"] != "준비"]
    return "\n".join(머리) + "\n\n" + "\n\n".join(본문)


# ── 판정 내부 단계(orchestrate) ─────────────────────────────
판정단계_함수 = ["_정규화단계", "_온톨로지단계", "_새_검색단계", "_새_프롬프트단계", "_새_판정준비단계", "_판정호출", "_새_검증단계",
            "_룰조회", "_검색단계", "_프롬프트구성단계", "_검증단계"]
_상태_제외 = {"conn", "cur", "t0"}


def _상태값(상태) -> dict:
    return {f.name: getattr(상태, f.name) for f in dataclasses.fields(상태) if f.name not in _상태_제외}


@contextmanager
def _예산시계_멈춤():
    """멈춘 동안 조사 예산의 벽시계 시간이 흐르지 않게 한다(운영은 사람이 중간에 멈추지 않는다)."""
    import research_runtime
    b = research_runtime.current_budget()
    root = getattr(b, "_parent", b)
    얼림 = False
    if root is not None and hasattr(root, "_lock"):
        with root._lock:
            if root._started is not None:
                root._elapsed += time.monotonic() - root._started
                root._started = None
                얼림 = True
    try:
        yield
    finally:
        if 얼림:
            with root._lock:
                if root._active > 0:
                    root._started = time.monotonic()


def 멈춤(설정, 설명: str, 상태=None):
    """단계 사이에서 멈춘다. 디버거: 중단점(변수 창에서 `상태` 확인), 터미널: Enter. Interactive 창: 멈추지 않음.
    서버 방식: 입력을 기다리지 않는다. 디버거가 붙어 있고 설정.서버_판정내부_멈춤 이 켜졌을 때만 중단점에서 멈춘다."""
    if getattr(설정, "실행방식", "단계별") == "서버":
        if getattr(설정, "서버_판정내부_멈춤", False) and 디버거_붙음():
            with _예산시계_멈춤():
                print(f"  ⏸ {설명} — 변수 창에서 `상태` 를 보고 F5(계속)로 진행")
                breakpoint()  # noqa: T100
        return
    if not 설정.단계마다_멈춤 or not 스크립트_모드():
        return
    with _예산시계_멈춤():
        if 디버거_붙음():
            print(f"  ⏸ {설명} — 변수 창에서 `상태` 를 보고 F5(계속)로 진행")
            breakpoint()  # noqa: T100  ← 여기서 멈춥니다. 한 단계 위(F6/Shift+F11)로 가면 운영 코드 줄입니다
        elif sys.stdin is not None and sys.stdin.isatty():
            input(f"  ⏸ {설명} — Enter 로 계속 ")


def 판정단계_감싸기(기록: 기록장, orchestrate):
    """orchestrate.판정() 이 부르는 단계 함수를 감싼다. 판정() 은 모듈 전역 이름으로 부르므로 그대로 운영 순서로 돈다."""
    for 이름 in 판정단계_함수:
        원래 = getattr(orchestrate, 이름, None)
        if 원래 is None or getattr(원래, "_디버그", False):
            continue

        def 감싼(*a, _원래=원래, _이름=이름, **kw):
            상태 = a[0] if a else None
            전 = {k: _json(v, None) for k, v in _상태값(상태).items()} if dataclasses.is_dataclass(상태) else {}
            t = time.time()
            반환 = _원래(*a, **kw)
            ms = int((time.time() - t) * 1000)
            후 = _상태값(상태) if 전 else {}
            바뀜 = {k: v for k, v in 후.items() if _json(v, None) != 전.get(k)}
            순번 = len(기록.판정내부) + 1
            끝 = isinstance(반환, dict) and 반환.get("판정") is not None
            rec = {"순번": 순번, "단계": 기록.현재단계, "함수": _이름, "ms": ms, "바뀐_상태": 바뀜,
                   "반환": 반환 if isinstance(반환, dict) else repr(반환)}
            기록.판정내부.append(rec)
            폴더 = 기록.단계폴더 / "판정내부"
            폴더.mkdir(exist_ok=True)
            (폴더 / f"{순번:02d}{_이름}.json").write_text(_json(rec), encoding="utf-8")
            결말 = (f"여기서 끝: 판정={반환.get('판정')} 게이트={반환.get('게이트')} · {str(반환.get('요약'))[:80]}"
                  if 끝 else "다음 단계로")
            print(f"  ◆ 판정 내부 {순번}. {_이름} · {ms:,}ms · 바뀐 값: {', '.join(바뀜) or '없음'} · {결말}")
            멈춤(기록.설정, f"{_이름} 끝", 상태)
            return 반환
        감싼._디버그 = True
        감싼.__wrapped__ = 원래
        setattr(orchestrate, 이름, 감싼)


# ── 운영 보호 · 서버 연결 ─────────────────────────────────────
def 운영보호(기록: 기록장, 설정, 주체):
    """앱을 만들기 전에 부른다. DB 쓰기 경로를 메모리로 돌리고, 로그인 주체를 고정한다."""
    from server import _common, auth, response_cache, routes_judge, routes_normalize, routes_questions
    from server.rate_limit import 제한
    import orchestrate
    import research_runtime

    # 로그인: Supabase 토큰 대신 이메일로 찾은 주체(운영 로그인과 같은 tenant.로그인주체 결과).
    # 서버 방식 + 서버_로그인 "화면": 프론트가 보낸 Supabase 토큰을 운영처럼 검증한다(JWKS · tenant.로그인주체 읽기).
    # 키트의 화면API 가 보내는 「Bearer debug」 만 설정.이메일 주체로 바꾼다(127.0.0.1 에서만 듣는다)
    원래토큰해석 = getattr(auth.토큰해석, "__wrapped__", auth.토큰해석)
    if getattr(설정, "실행방식", "단계별") == "서버" and getattr(설정, "서버_로그인", "화면") == "화면":
        def 토큰해석(authorization):
            if (authorization or "").strip().lower() == "bearer debug":
                return 주체
            return 원래토큰해석(authorization)
    else:
        def 토큰해석(authorization):
            return 주체
    토큰해석.__wrapped__ = 원래토큰해석
    auth.토큰해석 = 토큰해석

    # 응답 캐시: 운영 judge_cache 를 읽지도 쓰지도 않는다. 이 프로세스 메모리에만 둔다
    _메모리조회 = response_cache._메모리_조회
    # v053: 서버가 질문세트 저장 · 조회에 예외전파 인자를 넘긴다(없으면 /api/questions 가 TypeError → 상태 실패)
    response_cache._db_조회 = lambda k, org, account=None, 예외전파=False: _메모리조회(k)
    원래저장 = response_cache.저장

    def 저장(k, 값, *, 종류, org, account=None, 데이터버전_해시=None, ttl초=None, 예외전파=False):
        # 응답 재생: ③ 을 재생하면 그때 넣은 질문세트를 다시 넣는다(메모리 캐시 TTL 이 지났어도 판정이 질문을 찾게)
        기록.캐시저장.append({"단계": 기록.현재단계, "k": k, "값": 값, "종류": 종류, "데이터버전_해시": 데이터버전_해시, "ttl초": ttl초})
        return 원래저장(k, 값, 종류=종류, org=None, account=None, 데이터버전_해시=데이터버전_해시, ttl초=ttl초,
                    예외전파=예외전파)
    response_cache.저장 = 저장
    if not 설정.응답캐시_재사용:
        response_cache.조회 = lambda k, org, account=None: None       # 질문세트_조회 는 그대로(판정이 질문을 찾아야 한다)

    # 그 밖의 DB 쓰기: 읽기 전용 연결이라 실패한다. 무엇이 쓰려 했는지 남긴다
    원래실행 = _common._실행

    def _실행(sql, 인자=(), *, 예외전파=False):
        기록.막힌_쓰기.append({"단계": 기록.현재단계, "sql": " ".join(str(sql).split())[:300]})
        print(f"  [쓰기 막힘] {' '.join(str(sql).split())[:120]}")
        return 원래실행(sql, 인자, 예외전파=예외전파)
    _common._실행 = _실행

    if 설정.요청한도_끄기:
        제한.통과 = lambda ip: (True, "")

    # 예산 시간 · 판정 타임아웃
    원래init = research_runtime.Budget.__init__

    def init(self, *, seconds=None, **kw):
        원래init(self, seconds=설정.예산초 if seconds is None else seconds, **kw)
    research_runtime.Budget.__init__ = init
    운영예산초 = getattr(research_runtime, "세션_예산초", 300)   # research_runtime.Budget 기본값(운영, v061 부터 600초 · 그 전 300초)
    if 설정.단계마다_멈춤 or 설정.예산초 > 운영예산초:
        routes_judge._판정_타임아웃초 = max(routes_judge._판정_타임아웃초, 24 * 3600)
    if 설정.예산초 > 운영예산초:
        routes_questions.재조사_최대초 = 설정.예산초
        print(f"  [디버그] 예산 {설정.예산초:,}초: 웹 조사 벽시계 상한(② · ④-2 · ③-R) · 판정 타임아웃도 늘림(운영은 {운영예산초:.0f}초)")
    # 핫 리로드 대상 모듈에 거는 패치는 모듈별 함수로 둔다. 핫 리로드가 모듈을 다시 읽은 뒤 같은 함수를 다시 부른다
    _모듈패치.clear()
    _모듈패치["web_research"] = lambda: _web_research_패치(설정)
    _모듈패치["orchestrate"] = lambda: _orchestrate_패치(기록)
    for 패치 in _모듈패치.values():
        패치()

    # 화면 이벤트: 서버가 SSE 를 만드는 순간 출력하고 기록한다
    원래sse = _common._sse

    def _sse(이름, 값):
        기록.이벤트_받음(이름, 값)
        return 원래sse(이름, 값)
    for m in (_common, routes_normalize, routes_questions, routes_judge):
        m._sse = _sse

    # ③ 분류 디버그(v056 프롬프트 변형 A · B · C · 불일치 · 조정 · 거래 유형 확인 질문). 서버는 로그에 개수만 남기고 응답에 싣지 않는다
    원래분류 = routes_questions._분류

    def _분류(*a, **kw):
        r = 원래분류(*a, **kw)
        기록.분류디버그.append({"단계": 기록.현재단계, "디버그": (r or {}).get("_디버그"), "다음행동": (r or {}).get("다음행동"),
                            "실패": (r or {}).get("실패"), "분류": (r or {}).get("분류")})
        return r
    _분류.__wrapped__ = 원래분류
    routes_questions._분류 = _분류


# 핫 리로드가 모듈을 다시 읽은 뒤 다시 거는 패치 {모듈 이름: 패치 함수}. 운영보호 가 채운다. 패치 함수는 여러 번 불러도 같다(멱등)
_모듈패치: dict[str, object] = {}


def _web_research_패치(설정):
    """웹 조사는 서버 예산과 별도로 자기 벽시계 상한이 있다(② 전체 120초 · 웹 60초, ④-2 120초, ③-R 재조사 최대 60초).
    디버깅 중에는 예산초를 따라 늘린다. 계획 · 도구 횟수 상한(② 6 · 10, ④-2 15 · 30)은 그대로다"""
    import research_runtime
    import web_research
    if 설정.예산초 > getattr(research_runtime, "세션_예산초", 300):
        web_research._전체_제한초 = 설정.예산초
        web_research._웹_제한초 = 설정.예산초
        web_research._요건_웹_제한초 = 설정.예산초


def _orchestrate_패치(기록: 기록장):
    """판정 기록(tenant.decisions)을 쓰지 않고 남기기 · 판정() 인자 기록 · 판정 내부 단계 감싸기."""
    import orchestrate

    if not getattr(orchestrate.decisions_저장, "_디버그", False):
        def decisions_저장(cur, 행):
            기록.저장될_판정.append(행)
            (기록.단계폴더 / "저장될_판정행.json").write_text(_json(행), encoding="utf-8")
            if 기록.메모리 is not None:          # 서버 방식: 메모리 판정 번호를 돌려줘 계획에 연결한다(persist 대신 메모리저장소)
                번호 = 기록.메모리.판정행_저장(행)
                print(f"  [저장 생략] tenant.decisions 대신 메모리 판정 {번호} (저장될_판정행.json)")
                return 번호
            print("  [저장 생략] tenant.decisions 에 쓸 행을 저장될_판정행.json 에 남겼습니다")
            return None
        decisions_저장._디버그 = True
        orchestrate.decisions_저장 = decisions_저장

    # orchestrate.판정() 인자: 판정만 다시 돌리는 데 쓴다(판정_다시 · 검색_다시)
    if not getattr(orchestrate.판정, "_디버그", False):
        원래판정 = orchestrate.판정

        def 판정(질문, **kw):
            기록.판정_인자 = {"질문": 질문, **kw}
            (기록.단계폴더 / "판정_인자.json").write_text(_json(기록.판정_인자), encoding="utf-8")
            기록.판정_결과 = 원래판정(질문, **kw)
            return 기록.판정_결과
        판정._디버그 = True
        판정.__wrapped__ = 원래판정
        orchestrate.판정 = 판정
    판정단계_감싸기(기록, orchestrate)


def SSE_읽기(본문: str) -> list[tuple[str, object]]:
    out = []
    for 덩어리 in 본문.replace("\r\n", "\n").split("\n\n"):
        이름, 데이터 = None, []
        for 줄 in 덩어리.split("\n"):
            if 줄.startswith("event:"):
                이름 = 줄[6:].strip()
            elif 줄.startswith("data:"):
                데이터.append(줄[5:].strip())
        if 이름:
            try:
                out.append((이름, json.loads("\n".join(데이터))))
            except Exception:  # noqa: BLE001
                out.append((이름, "\n".join(데이터)))
    return out


def _응답값(상태: int, 형식: str, 본문: str) -> dict:
    """HTTP 응답 → 응답.json 모양 {HTTP, 이벤트, 결과, 완료} (SSE) | {HTTP, 값} (JSON)."""
    if "text/event-stream" not in (형식 or ""):
        try:
            값 = json.loads(본문)
        except Exception:  # noqa: BLE001
            값 = 본문
        return {"HTTP": 상태, "값": 값}
    이벤트 = SSE_읽기(본문)
    결과 = {"HTTP": 상태, "이벤트": 이벤트}
    for 이름 in ("결과", "완료"):
        v = next((v for n, v in reversed(이벤트) if n == 이름), None)
        if v is not None:
            결과[이름] = v
    return 결과


class 화면API:
    """프론트처럼 API 를 부른다. 기본은 같은 프로세스 안에서 FastAPI 앱을 띄운다(중단점은 서버 코드 어디에나 걸린다).
    주소(예: http://127.0.0.1:8000)를 주면 이미 떠 있는 디버그 서버(설정.실행방식 = "서버")에 HTTP 로 보낸다."""

    def __init__(self, 기록: 기록장, 주소: str | None = None):
        self.기록 = 기록
        self.주소 = 주소
        if 주소:
            import httpx
            self.c = httpx.Client(base_url=주소.rstrip("/"), timeout=None)
            return
        from fastapi.testclient import TestClient
        from server.main import app
        self.c = TestClient(app)
        self.c.__enter__()                                   # 기동 워밍업(KURE-v1 · 비목 후보 모델) 포함

    def post(self, 경로: str, 본문: dict) -> dict:
        본문 = {k: v for k, v in 본문.items() if v is not None}
        (self.기록.단계폴더 / "요청.json").write_text(_json({"경로": 경로, "본문": 본문}), encoding="utf-8")
        print(f"  [요청] POST {경로} {_json(본문, None)[:300]}")
        r = self.c.post(경로, json=본문, headers={"accept": "text/event-stream", "authorization": "Bearer debug"})
        결과 = _응답값(r.status_code, r.headers.get("content-type", ""), r.text)
        if "값" in 결과:
            print(f"  [응답 {r.status_code}] {_json(결과['값'], None)[:500]}")
        (self.기록.단계폴더 / "응답.json").write_text(_json(결과), encoding="utf-8")
        return 결과

    def get(self, 경로: str) -> dict:
        r = self.c.get(경로, headers={"authorization": "Bearer debug"})
        return _응답값(r.status_code, r.headers.get("content-type", ""), r.text)


# ── Interactive 창에서 쓰는 보기 함수 ──────────────────────────
def LLM_보기(기록: 기록장, 번호: int, 최대: int | None = 20000):
    s = _LLM_md(기록.LLM[번호 - 1])
    print(s if 최대 is None or len(s) <= 최대 else s[:최대] + f"\n… (전체 {len(s):,}자, LLM.md 파일 참고)")


def SQL_보기(기록: 기록장, 번호: int, 다시실행: bool = False, 행: int = 20):
    r = 기록.SQL[번호 - 1]
    print(f"#{번호} [{r['단계']}] {r['호출']} · {r['종류']} · 행 {r['행수']} · {r['ms']}ms\n{r['질의'].strip()}\n인자: {r['인자']}")
    if 다시실행 and r["종류"] != "준비":
        from _lib import db
        import ontology_graph
        with db.connect(connect_timeout=5) as c:
            cur = c.cursor()
            if r["종류"] == "Cypher":
                ontology_graph.준비(cur)
            rows = cur.execute(r["질의"], r["인자"]).fetchall()
            cols = [d.name for d in cur.description or []]
        print(f"── 결과 {len(rows)}행 (앞 {행}행)")
        for x in rows[:행]:
            print(_json(dict(zip(cols, x)), None)[:400])


def 호출_찾기(기록: 기록장, 글: str, 최대: int = 50):
    """이번 단계 호출 트리에서 함수 이름에 `글` 이 든 호출의 인자 · 반환 · ms."""
    n = 0
    for r in 기록.호출:
        if 글 in r["함수"]:
            print(f"#{r['번호']} {r['함수']}:{r['줄']} [{r.get('ms')}ms]\n   인자: {r['인자']}\n   반환: {r.get('반환')}")
            n += 1
            if n >= 최대:
                break


def 분류디버그_보기(x: dict):
    """③ 분류 디버그(v056): 프롬프트 변형 A · B · C 결과와 이유 · 불일치 · 조정 근거 · 거래 유형 확인 질문. 화면에는 안 나온다."""
    d = x.get("디버그") or {}
    if not d:
        print(f"  [디버그 ③] 분류 디버그 없음(mock · 분류 전 장애) · 다음행동 {x.get('다음행동')}")
        return
    print(f"  [디버그 ③] 확인된 거래 유형 {d.get('확인거래유형')} · 후보 {d.get('후보수')}개 · 발췌 후보 {d.get('발췌후보수')}개 · "
          f"{d.get('ms')}ms")
    for v in d.get("변형") or []:
        print(f"    변형 {v.get('변형')}: {v.get('표시문') or '맞는 분류 없음'} · 품목 {v.get('품목')} · 거래유형 {v.get('거래유형')} · "
              f"확신 {v.get('확신')} · {v.get('ms')}ms" + (f" · 실패 {v['실패']}" if v.get("실패") else ""))
        if v.get("이유"):
            print(f"      이유: {str(v['이유'])[:300]}")
    if d.get("변형"):
        print(f"    불일치: {d.get('불일치') or '없음(세 변형 일치 또는 모두 맞는 분류 없음)'}")
    j = d.get("조정")
    if j:
        print(f"    조정: 번호 {j.get('번호')} · 확신 {j.get('확신')} · 거래유형 {j.get('거래유형')} · 후보 {j.get('후보')} · "
              f"발췌 {j.get('발췌수')}개 · 규범 {j.get('규범수')}개 · 프롬프트 {j.get('프롬프트글자')}자 · {j.get('ms')}ms")
        print(f"      이유: {str(j.get('이유'))[:400]}")
    if d.get("거래유형질문"):
        print(f"    거래 유형 확인 질문: {d['거래유형질문']}")
    mb = d.get("모든비목")                  # v058: 모든 비목(비목:창업/*) 아래에만 있는 노드 → 규정 위치의 비목 | 모든 비목 공통
    if mb:
        print(f"    모든 비목 노드: 방법 {mb.get('방법')} · 사유 {mb.get('사유') or '-'} · 근거 {str(mb.get('근거'))[:300]}")
    if d.get("LLM실패"):
        print(f"    LLM 실패: {d['LLM실패']}")


def _깊은사본(값):
    try:
        return copy.deepcopy(값)
    except Exception:  # noqa: BLE001  복사할 수 없는 값(잠금 · 외부 객체)은 얕은 사본
        return copy.copy(값)


# ── 핫 리로드 ─────────────────────────────────────────────────
# 프롬프트 · 검색 코드가 든 모듈. 프로세스 상태를 가진 모듈(research_runtime · response_cache · rate_limit · llm_qwen · _lib.db ·
# server.*)은 넣지 않는다. 순서: 다른 대상이 가져다 쓰는 모듈을 앞에 둔다(뒤 모듈이 다시 읽힐 때 새 함수를 가져간다)
핫리로드_기본 = ["assemble_context", "retrieve", "ontology_lookup", "ontology_context", "normalize_run", "expense_input_check",
             "web_research", "item_classify", "question_generate", "orchestrate"]
# 다시 읽어도 살려 둘 모듈 값. KURE-v1 모델 · 토크나이저 · 그 잠금(다시 올리면 수십 초). 캐시(별칭 · 코퍼스 버전 · 스키마)는 비운다
_보존값 = {"retrieve": ("_모델", "_토큰화", "_모델_잠금")}
_해시캐시: dict[str, tuple] = {}


def _파일해시(경로) -> str | None:
    """sha1. 수정 시각 · 크기가 같으면 다시 읽지 않는다."""
    try:
        st = os.stat(경로)
    except OSError:
        return None
    키 = (st.st_mtime_ns, st.st_size)
    c = _해시캐시.get(str(경로))
    if c and c[0] == 키:
        return c[1]
    h = hashlib.sha1(Path(경로).read_bytes()).hexdigest()
    _해시캐시[str(경로)] = (키, h)
    return h


def _모듈경로(이름: str) -> Path:
    m = sys.modules.get(이름)
    f = getattr(m, "__file__", None) if m else None
    return Path(f) if f else 작업트리 / "scripts" / f"{이름.replace('.', '/')}.py"


def _참조_바꾸기(m: types.ModuleType, 옛: dict) -> int:
    """다른 모듈이 `from m import Y` 로 가져간 옛 객체를 새 객체로 바꾼다. 바꾼 곳 수."""
    없음 = object()
    옛id: dict[int, tuple] = {}
    for k, v in 옛.items():
        if k.startswith("__") or isinstance(v, types.ModuleType):
            continue
        새v = m.__dict__.get(k, 없음)
        if 새v is 없음 or 새v is v:
            continue
        옛id[id(v)] = (k, v, 새v)
    n = 0
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if mod is m or not f or not os.path.normcase(f).startswith(_뿌리):
            continue
        d = mod.__dict__
        for a, val in list(d.items()):
            x = 옛id.get(id(val))
            if x is None or x[1] is not val:
                continue
            k, _, 새v = x
            # 글 · 숫자는 같은 값이 우연히 같은 객체일 수 있어 이름이 같을 때만 바꾼다
            if isinstance(val, (str, bytes, int, float, bool, tuple, frozenset)) and a != k:
                continue
            d[a] = 새v
            n += 1
    return n


class 핫리로드:
    """요청마다 확인() 을 부른다. 바뀐 대상 모듈만 다시 읽는다. 다시 읽기가 실패하면(편집 중 문법 오류 등) 옛 모듈로 계속 돈다."""

    def __init__(self, 설정):
        self.켜짐 = getattr(설정, "핫리로드", True)
        self.대상 = list(getattr(설정, "핫리로드_대상", None) or 핫리로드_기본)
        self.기준 = {이름: _파일해시(_모듈경로(이름)) for 이름 in self.대상}
        self._잠금 = threading.Lock()

    def 확인(self) -> list[str]:
        if not self.켜짐:
            return []
        with self._잠금:
            return self._확인()

    def _확인(self) -> list[str]:
        다시읽음 = []
        for 이름 in self.대상:
            경로 = _모듈경로(이름)
            h = _파일해시(경로)
            if h == self.기준.get(이름):
                continue
            m = sys.modules.get(이름)
            if m is None:                    # 아직 안 불러온 모듈: 처음 import 때 새 내용을 읽는다
                self.기준[이름] = h
                continue
            t = time.time()
            옛 = dict(m.__dict__)
            try:
                importlib.reload(m)
            except BaseException as e:  # noqa: BLE001
                m.__dict__.clear()
                m.__dict__.update(옛)
                print(f"[핫 리로드 실패] {경로.name}: {type(e).__name__}: {str(e)[:300]} — 이전 코드로 계속 돈다"
                      "(고치고 저장하면 다음 요청에서 다시 읽는다)", flush=True)
                continue
            for k in _보존값.get(이름, ()):
                if 옛.get(k) is not None:
                    setattr(m, k, 옛[k])
            n = _참조_바꾸기(m, 옛)
            if 이름 in _모듈패치:
                _모듈패치[이름]()
            self.기준[이름] = h
            다시읽음.append(이름)
            print(f"[핫 리로드] {경로.name} 다시 읽음 · 다른 모듈의 참조 {n}곳 바꿈 · {int((time.time() - t) * 1000)}ms", flush=True)
        return 다시읽음


# ── 서버 방식: 메모리 저장소(계획 · 판정 · 할 일 · 평가) ─────────────────
def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _지금() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class 메모리저장소:
    """운영 DB 는 읽기 전용이라 프론트가 하는 쓰기를 여기 둔다. 번호는 900,000,001 부터(운영 번호와 겹치지 않게).
    목록 · 상세 · 할 일 GET 은 운영 행에 이 저장소 행을 합쳐 돌려준다. 프로세스가 끝나면 사라진다."""

    def __init__(self, 기록: 기록장):
        self.기록 = 기록
        self.계획: dict[int, dict] = {}
        self.판정: dict[int, dict] = {}          # {"행": decisions 행, "out": 화면 결과}
        self.할일: dict[int, dict] = {}
        self.피드백: dict[int, dict] = {}
        self._번호 = 900_000_000
        self._잠금 = threading.Lock()

    def 새번호(self) -> int:
        with self._잠금:
            self._번호 += 1
            return self._번호

    def _쓰기(self, 글: str, 경로: str = ""):
        self.기록.막힌_쓰기.append({"단계": self.기록.현재단계, "sql": f"[메모리] {글}"})
        print(f"  [메모리 저장] {글} (운영 DB 에는 쓰지 않음){f' · {경로}' if 경로 else ''}", flush=True)

    @staticmethod
    def _내것(p: dict, org, account) -> bool:
        if p["org_id"] != (str(org) if org else None):
            return False
        return not org or p["account_id"] == (str(account) if account else None)

    # orchestrate.decisions_저장 이 부른다(판정 워커 스레드)
    def 판정행_저장(self, 행: dict) -> int:
        번호 = self.새번호()
        self.판정[번호] = {"행": dict(행), "out": None}
        return 번호

    # ── 계획 ─────────────────────────────
    def 계획_생성(self, body, org_id, account_id):
        from server import routes_plans
        from server.models import 계획상세
        질문원문 = body.질문원문 or routes_plans._합성(body)
        정규화 = body.정규화 or {"품목": body.품목, "용도": body.용도, "금액": body.금액, "거래처": body.거래처 or "",
                             "질문원문": 질문원문}
        번호, 지금 = self.새번호(), _지금()
        p = {"plan_id": 번호, "org_id": str(org_id) if org_id else None,
             "account_id": str(account_id) if org_id and account_id else None,
             "제목": body.제목 or body.품목, "확정비목": body.확정비목, "금액": float(body.금액), "집행예정일": body.집행예정일,
             "updated_at": 지금, "created_at": 지금, "사업명": body.사업명, "상태": "draft",
             "질문원문": 질문원문, "거래처": body.거래처, "추가설명": body.추가설명,
             "정규화": 정규화, "latest_decision_id": None, "실제지출일": None, "실제지출액": None}
        self.계획[번호] = p
        if body.집행예정일:
            self._집행줄(p)
        self._쓰기(f"계획 {번호} 생성 「{p['제목']}」")
        return 계획상세(**{k: p[k] for k in ("plan_id", "제목", "확정비목", "금액", "집행예정일", "updated_at", "created_at",
                                             "사업명", "상태", "질문원문", "거래처", "추가설명", "정규화", "latest_decision_id")},
                        판정=None, 용도=body.용도, 판정상세=None, 할일=[])

    def _집행줄(self, p: dict):
        연동 = next((t for t in self.할일.values() if t["plan_id"] == p["plan_id"] and t.get("집행예정일_연동")), None)
        if not p.get("집행예정일"):
            if 연동 and 연동["상태"] != "완료":
                del self.할일[연동["task_id"]]
            return
        if 연동:
            if 연동["상태"] != "완료":
                연동["due_date"] = p["집행예정일"]
            return
        번호 = self.새번호()
        self.할일[번호] = {"task_id": 번호, "plan_id": p["plan_id"], "출처": "user", "code": None, "구분": "집행",
                         "항목": f"{p['제목']} 집행", "설명": None, "due_date": p["집행예정일"], "유형": "기타",
                         "날짜_사용자수정": True, "상태": "집행예정", "계획제목": p["제목"], "집행예정일_연동": True,
                         "작성자": None}

    def _계획_할일(self, plan_id: int) -> list[dict]:
        return [dict(t) for t in self.할일.values() if t["plan_id"] == plan_id]

    def 단계요약(self, plan_id: int) -> dict:
        from server import routes_tasks
        return routes_tasks.단계요약_계산([(t["구분"], t["유형"], t["상태"]) for t in self._계획_할일(plan_id)])

    def _판정값(self, p: dict) -> str | None:
        d = self.판정.get(p.get("latest_decision_id"))
        return (d.get("행") or {}).get("판정") if d else None

    def 목록행(self, org_id) -> list[dict]:
        from server import _common
        acc = _common.현재_account.get()
        return [{**{k: p[k] for k in ("plan_id", "제목", "확정비목", "금액", "집행예정일", "updated_at", "사업명", "상태",
                                      "실제지출일", "실제지출액")}, "판정": self._판정값(p)}
                for p in self.계획.values() if self._내것(p, org_id, acc)]

    def 상세(self, plan_id: int):
        from server.inquiry import 문의초안
        from server.judge_response import 구_판정키_포함, 사용자답변_공개, 할일_공개
        from server.models import 계획상세
        import orchestrate
        p = self.계획[plan_id]
        d = self.판정.get(p.get("latest_decision_id"))
        행 = {k: _j(v) for k, v in ((d or {}).get("행") or {}).items()}
        판정상세 = None
        if d:
            온톨 = 행.get("온톨로지결과") if isinstance(행.get("온톨로지결과"), dict) else {}
            판정상세 = {
                "판정": 행.get("판정"), "요약": 행.get("요약"), "해야할일": 할일_공개(행.get("해야할일")),
                "인용": 행.get("인용") or [], "전제": 행.get("전제") or [], "신뢰등급": 행.get("신뢰등급"),
                "규정버전": 행.get("규정버전"), "참조관계": 행.get("참조관계") or [], "검증사유": 행.get("검증사유") or [],
                "기준문서": 행.get("기준문서"),
                "분류": None, "근거경로": [], "코드판정": None, "조건": [], "기한조건": [], "금지조건": [], "해당시조건": [],
                "공통사항": [], "확인한조건": [], "용어풀이": [], "문의처": None,
                **{k: v for k, v in 온톨.items() if k in orchestrate.온톨로지결과_칸}}
            판정상세 = 구_판정키_포함(판정상세)
            try:
                판정상세["문의초안"] = 문의초안(정규화=p["정규화"] or {},
                                         결과={k: 행.get(k) for k in ("판정", "인용", "검증코드", "처리단계", "실패단계")},
                                         사업명=p["사업명"], 비목=p["확정비목"])
            except Exception:  # noqa: BLE001
                판정상세["문의초안"] = None
        할일 = self._계획_할일(plan_id)
        for t in 할일:
            t["계획제목"] = p["제목"]
        return 계획상세(
            plan_id=plan_id, 제목=p["제목"], 확정비목=p["확정비목"], 금액=p["금액"], 판정=행.get("판정") if d else None,
            집행예정일=p["집행예정일"], updated_at=p["updated_at"], created_at=p["created_at"], 사업명=p["사업명"],
            상태=p["상태"], 질문원문=p["질문원문"], 용도=(p["정규화"] or {}).get("용도"), 거래처=p["거래처"],
            추가설명=p["추가설명"], 정규화=p["정규화"] or {}, latest_decision_id=p["latest_decision_id"], 판정상세=판정상세,
            사용자답변=사용자답변_공개(행.get("사용자답변")) if d else [], 할일=할일,
            내피드백=self.피드백.get(p.get("latest_decision_id")), 팀평가=[], 첨부=[],
            실제지출일=p["실제지출일"], 실제지출액=p["실제지출액"], **self.단계요약(plan_id))

    # routes_judge.판정_저장 대신(판정을 계획에 연결 + 할 일). persist._계획에_연결 과 같은 순서
    def 판정_연결(self, plan_id: int, body, out: dict, org_id, decision_id, account_id, 캐시적중: bool, 실패: bool) -> dict:
        from server import persist
        from server.models import 할일동기화응답
        if 실패:
            return {"저장": False, "사유": "판정에 실패해 계획에 연결하지 않았습니다"}
        p = self.계획[plan_id]
        if decision_id is None or decision_id not in self.판정:
            if not 캐시적중:
                return {"저장": False, "사유": "판정 기록이 없어 계획에 연결하지 않았습니다"}
            decision_id = self.새번호()
            self.판정[decision_id] = {"행": persist._캐시판정_행(body, out, org_id, account_id, plan_id), "out": None}
        d = self.판정[decision_id]
        d["out"] = dict(out)
        if out.get("조사") is not None:      # persist: 온톨로지결과 에 조사를 합친다
            온톨 = _j(d["행"].get("온톨로지결과")) or {}
            d["행"]["온톨로지결과"] = {**(온톨 if isinstance(온톨, dict) else {}), "조사": out["조사"]}
        d["행"]["plan_id"] = plan_id
        p.update(latest_decision_id=decision_id, 상태="judged", updated_at=_지금(),
                 확정비목=p["확정비목"] or getattr(body, "확정비목", None) or None)
        생성, 코드 = self._할일_동기화(p, decision_id, persist._할일_목록(out))
        self._쓰기(f"계획 {plan_id} 에 판정 {decision_id} 연결 · 할 일 {생성}건")
        return {"저장": True, "decision_id": decision_id, "plan_id": plan_id,
                "할일": 할일동기화응답(생성=생성, 코드매칭=코드, 코드미매칭=생성 - 코드).model_dump()}

    def _할일_동기화(self, p: dict, decision_id: int, 해야할일: list[dict]) -> tuple[int, int]:
        """routes_tasks._동기화_준비 로 코드 · 구분 · 기한을 정한다(check_items · L3 조문은 운영 DB 에서 읽기만). ai 행은 새로 만든다."""
        from server import l3_deadline, routes_tasks
        from server._common import 트랜잭션
        from server.models import 할일동기화
        집행예정일 = date.fromisoformat(p["집행예정일"][:10]) if p.get("집행예정일") else None
        try:
            with 트랜잭션(p["org_id"], p["account_id"]) as cur:
                체크 = routes_tasks._체크항목_표(cur)
                l3 = l3_deadline.조문_조회(cur, p["org_id"]) if 집행예정일 and p["org_id"] else []
            행들 = routes_tasks._동기화_준비(할일동기화(decision_id=decision_id, 해야할일=해야할일), 체크, l3, 집행예정일)
        except Exception as e:  # noqa: BLE001
            print(f"  [메모리 저장] 할 일 코드 매칭 실패({type(e).__name__}) — 코드 없이 넣는다")
            행들 = [{"항목": h.get("항목") or "", "설명": h.get("설명"), "코드": None, "구분": h.get("구분") or "결제전",
                   "유형": "기타", "due_date": None} for h in 해야할일]
        for k in [k for k, t in self.할일.items() if t["plan_id"] == p["plan_id"] and t["출처"] == "ai"]:
            del self.할일[k]
        for 행 in 행들:
            번호 = self.새번호()
            dd = 행.get("due_date")
            self.할일[번호] = {"task_id": 번호, "plan_id": p["plan_id"], "출처": "ai", "code": 행["코드"], "구분": 행["구분"],
                             "항목": 행["항목"], "설명": 행["설명"], "due_date": dd.isoformat() if hasattr(dd, "isoformat") else dd,
                             "유형": 행["유형"], "날짜_사용자수정": False, "상태": "준비필요", "계획제목": p["제목"],
                             "집행예정일_연동": False, "작성자": None}
        return len(행들), sum(1 for 행 in 행들 if 행["코드"])

    def 할일_목록행(self, org_id) -> list[dict]:
        from server import _common
        acc = _common.현재_account.get()
        내계획 = {i for i, p in self.계획.items() if self._내것(p, org_id, acc)}
        return [dict(t) for t in self.할일.values() if t["plan_id"] in 내계획]

    # ── 흐름기록 이 처리하는 쓰기(메모리 번호만) ────────────
    def 쓰기_처리(self, 방법: str, 경로: str, 본문: bytes):
        """(HTTP 상태, 응답 값) 또는 None(이 저장소 번호가 아님)."""
        from server.models import 계획수정, 판정피드백, 피드백입력, 할일, 할일생성, 할일수정
        값 = json.loads(본문 or b"{}") if 방법 in ("POST", "PUT", "PATCH") else {}
        m = re.fullmatch(r"/api/plans/(\d+)", 경로)
        if m and int(m[1]) in self.계획:
            p = self.계획[int(m[1])]
            if 방법 == "DELETE":
                할일수 = sum(1 for t in self.할일.values() if t["plan_id"] == p["plan_id"])
                self.할일 = {k: t for k, t in self.할일.items() if t["plan_id"] != p["plan_id"]}
                del self.계획[p["plan_id"]]
                self._쓰기(f"계획 {p['plan_id']} 삭제", 경로)
                return 200, {"삭제": True, "plan_id": p["plan_id"], "제목": p["제목"], "할일삭제": 할일수}
            if 방법 == "PATCH":
                b = 계획수정(**값)
                if "집행예정일" in b.model_fields_set:
                    p["집행예정일"] = b.집행예정일.isoformat() if b.집행예정일 else None
                    self._집행줄(p)
                if b.실제지출일:
                    p["실제지출일"], p["실제지출액"] = b.실제지출일.isoformat(), b.실제지출액
                    for t in self.할일.values():
                        if t["plan_id"] == p["plan_id"] and t.get("집행예정일_연동"):
                            t["상태"], t["due_date"] = "완료", p["실제지출일"]
                p["updated_at"] = _지금()
                self._쓰기(f"계획 {p['plan_id']} 수정", 경로)
                return 200, self.상세(p["plan_id"])
        m = re.fullmatch(r"/api/decisions/(\d+)/feedback", 경로)
        if m and int(m[1]) in self.판정 and 방법 == "PUT":
            b = 피드백입력(**값)
            self.피드백[int(m[1])] = 판정피드백(평가=b.평가, 이유=b.이유, 의견=b.의견).model_dump()
            self._쓰기(f"판정 {m[1]} 평가 {b.평가}", 경로)
            return 200, self.피드백[int(m[1])]
        m = re.fullmatch(r"/api/(?:plans/\d+/)?tasks/(\d+)", 경로)
        if m and int(m[1]) in self.할일:
            t = self.할일[int(m[1])]
            if 방법 == "DELETE":
                del self.할일[t["task_id"]]
                self._쓰기(f"할 일 {t['task_id']} 삭제", 경로)
                return 200, {"삭제": True, "task_id": t["task_id"]}
            if 방법 == "PATCH":
                b = 할일수정(**값)
                for k in b.model_fields_set:
                    v = getattr(b, k)
                    t[k] = v.isoformat() if hasattr(v, "isoformat") else v
                if "due_date" in b.model_fields_set:
                    t["날짜_사용자수정"] = True
                self._쓰기(f"할 일 {t['task_id']} 수정", 경로)
                return 200, 할일(**t)
        m = re.fullmatch(r"/api/plans/(\d+)/tasks", 경로)
        if m and int(m[1]) in self.계획 and 방법 == "POST":
            b = 할일생성(**값)
            번호, p = self.새번호(), self.계획[int(m[1])]
            self.할일[번호] = {"task_id": 번호, "plan_id": p["plan_id"], "출처": "user", "code": None, "구분": b.구분,
                             "항목": b.항목, "설명": b.설명, "due_date": b.due_date.isoformat() if b.due_date else None,
                             "유형": b.유형, "날짜_사용자수정": bool(b.due_date), "상태": b.상태, "계획제목": p["제목"],
                             "집행예정일_연동": False, "작성자": None}
            self._쓰기(f"계획 {p['plan_id']} 할 일 추가", 경로)
            return 201, 할일(**self.할일[번호])
        return None

    def 설치(self):
        """routes_plans · routes_tasks · routes_judge 의 DB 함수를 감싼다. 저장소 번호면 메모리, 아니면 운영(읽기)."""
        from server import routes_judge, routes_plans, routes_tasks
        저장소 = self
        원래 = {"목록": routes_plans._live_목록, "통계": routes_plans._live_통계, "상세": routes_plans._live_상세,
              "단계": routes_plans._단계요약_붙이기, "할일": routes_tasks._live_목록, "연결": routes_judge.판정_저장}

        def _live_생성(body, *, org_id, account_id=None):
            return 저장소.계획_생성(body, org_id, account_id)

        def _live_목록(org_id):
            return [*원래["목록"](org_id), *저장소.목록행(org_id)]

        def _live_통계(org_id):
            s = dict(원래["통계"](org_id))
            for r in 저장소.목록행(org_id):
                s["전체"] += 1
                s["금액합계"] += r["금액"] or 0
                s[{"확인 필요": "확인필요", "불가": "위험", "가능": "특이사항없음"}.get(r["판정"], "점검전")] += 1
            return s

        def _live_상세(plan_id, org_id):
            return 저장소.상세(plan_id) if plan_id in 저장소.계획 else 원래["상세"](plan_id, org_id)

        def _단계요약_붙이기(행, org_id):
            원래["단계"](행, org_id)
            for r in 행:
                if r["plan_id"] in 저장소.계획:
                    r.update(저장소.단계요약(r["plan_id"]))

        def _할일목록(org_id):
            return [*원래["할일"](org_id), *저장소.할일_목록행(org_id)]

        def 판정_저장(plan_id, body, out, org_id=None, decision_id=None, *, account_id=None, 캐시적중=False, 실패=False):
            if plan_id in 저장소.계획:
                return 저장소.판정_연결(plan_id, body, out, org_id, decision_id, account_id, 캐시적중, 실패)
            return 원래["연결"](plan_id, body, out, org_id, decision_id=decision_id, account_id=account_id,
                             캐시적중=캐시적중, 실패=실패)

        routes_plans._live_생성, routes_plans._live_목록, routes_plans._live_통계 = _live_생성, _live_목록, _live_통계
        routes_plans._live_상세, routes_plans._단계요약_붙이기 = _live_상세, _단계요약_붙이기
        routes_tasks._live_목록 = _할일목록
        routes_judge.판정_저장 = 판정_저장


# ── 서버 방식: 흐름 요청 기록 · 응답 재생 ──────────────────────────
흐름경로 = ("/api/normalize", "/api/questions", "/api/judge")
_통과_쓰기 = ("/api/gpu/wake",)                 # DB 를 쓰지 않는 POST
_재생_제외 = {"멱등키"}                        # 응답 재생 키에서 빼는 요청 값(매번 바뀐다)
_비교_제외 = {"멱등키", "plan_id", "조사ID", "질문세트", "조사답변"}   # 「같은 입력의 이전 판정」 키에서 빼는 값
_세션_제외 = {"lock", "budget", "owner", "expires"}
_예산_제외 = {"_lock", "emit", "_started", "_active", "_parent"}


def _예산_사본(b) -> dict:
    d = {}
    for k, v in vars(b).items():
        if k in _예산_제외:
            continue
        if k == "ontology_state" and isinstance(v, dict):
            d[k] = {kk: (vv if kk == "budget" else _깊은사본(vv)) for kk, vv in v.items()}
        else:
            d[k] = _깊은사본(v)
    return d


def _세션_사본(s) -> dict:
    return {"세션": {k: _깊은사본(v) for k, v in vars(s).items() if k not in _세션_제외}, "예산": _예산_사본(s.budget)}


def _되돌리기(obj, 값: dict, 제외: set):
    for k in [k for k in vars(obj) if k not in 제외 and k not in 값]:
        delattr(obj, k)
    for k, v in 값.items():
        if k == "ontology_state" and isinstance(v, dict):
            setattr(obj, k, {kk: (vv if kk == "budget" else _깊은사본(vv)) for kk, vv in v.items()})
        else:
            setattr(obj, k, _깊은사본(v))


def _세션_되돌리기(s, 사본: dict):
    _되돌리기(s, 사본["세션"], _세션_제외)
    _되돌리기(s.budget, 사본["예산"], _예산_제외)


def _실패_응답인가(r: dict) -> bool:
    """다시 시도 안내 · 오류 · 실패면 True(화면의 「다시 시도」 는 같은 요청을 다시 보내므로 재생하지 않는다)."""
    if r.get("HTTP") != 200:
        return True
    if any(n == "오류" for n, _ in r.get("이벤트") or []):
        return True
    값들 = [v for n, v in r.get("이벤트") or [] if n in ("결과", "완료")] + [r.get("값")]
    return any(isinstance(v, dict) and (v.get("다음행동") == "다시시도" or v.get("상태") == "실패" or v.get("판정실패"))
               for v in 값들)


def _스크립트_파일(경로: str) -> bool:
    return os.path.normcase(경로).startswith(_뿌리)


# 응답 재생의 의존 = 그 요청이 실행한 최상위 정의(함수 · 클래스 · 모듈 변수) 단위. 파일 단위면 assemble_context.이스케이프 를 쓴 ① 이
# 시스템_프롬프트 만 고쳐도 다시 돈다. 정의 이름으로 원문을 잘라 해시하므로 위쪽 줄이 늘어도 그대로다
_정의캐시: dict[str, tuple] = {}


def _정의표(경로: str) -> dict | None:
    """파일의 최상위 정의 {해시: {이름: 원문 sha1}, 범위: [(시작줄, 끝줄, 이름들)], 가져옴: {이름: (모듈, 원래 이름)}, 모듈: {이름: 모듈}}."""
    h = _파일해시(경로)
    c = _정의캐시.get(경로)
    if c and c[0] == h:
        return c[1]
    try:
        원문 = Path(경로).read_text(encoding="utf-8")
        나무 = ast.parse(원문)
    except (OSError, SyntaxError, ValueError):
        return None
    줄 = 원문.splitlines(keepends=True)
    표 = {"해시": {}, "범위": [], "가져옴": {}, "모듈": {}}

    def 이름들(t) -> list[str]:
        if isinstance(t, ast.Name):
            return [t.id]
        if isinstance(t, (ast.Tuple, ast.List)):
            return [n for e in t.elts for n in 이름들(e)]
        return []
    for 노드 in 나무.body:
        if isinstance(노드, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            이름 = [노드.name]
        elif isinstance(노드, ast.Assign):
            이름 = [n for t in 노드.targets for n in 이름들(t)]
        elif isinstance(노드, (ast.AnnAssign, ast.AugAssign)):
            이름 = 이름들(노드.target)
        elif isinstance(노드, ast.ImportFrom):
            이름 = [a.asname or a.name for a in 노드.names]
            for a in 노드.names:
                표["가져옴"][a.asname or a.name] = (노드.module or "", a.name)
        elif isinstance(노드, ast.Import):
            이름 = [a.asname or a.name.split(".")[0] for a in 노드.names]
            for a in 노드.names:
                표["모듈"][a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        else:                                  # if · try 블록: 안에서 정의 · 대입한 이름 전부
            이름 = sorted({x.name for x in ast.walk(노드) if isinstance(x, (ast.FunctionDef, ast.ClassDef))}
                        | {x.id for x in ast.walk(노드) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)})
        시작 = min([노드.lineno, *[d.lineno for d in getattr(노드, "decorator_list", [])]])
        조각 = "".join(줄[시작 - 1:노드.end_lineno])
        for n in 이름:
            표["해시"][n] = hashlib.sha1((표["해시"].get(n, "") + 조각).encode("utf-8")).hexdigest()
        표["범위"].append((시작, 노드.end_lineno, 이름))
    _정의캐시[경로] = (h, 표)
    return 표


def _모듈파일(이름: str) -> str | None:
    m = sys.modules.get(이름)
    f = getattr(m, "__file__", None)
    return f if f and _스크립트_파일(f) else None


def _정의해시(파일: str, 이름: str) -> str | None:
    if 이름 == "*":
        return _파일해시(파일)
    표 = _정의표(파일)
    return None if 표 is None else 표["해시"].get(이름, "없음")


def _의존_정의(코드들) -> dict[tuple[str, str], str | None]:
    """실행된 코드 객체 → {(파일, 최상위 정의 이름): 원문 해시}. 코드가 읽은 모듈 이름(co_names)도 넣는다
    (`from X import Y` 의 Y 는 X 파일의 Y, `import X` 뒤 X.Y 는 X 파일의 Y)."""
    필요: set[tuple[str, str]] = set()
    for code in 코드들:
        f = code.co_filename
        if not _스크립트_파일(f):
            continue
        표 = _정의표(f)
        if 표 is None:
            필요.add((f, "*"))
            continue
        위 = next((ns for a, b, ns in 표["범위"] if a <= code.co_firstlineno <= b), None)
        if 위:
            필요.update((f, n) for n in 위)
        이름 = set(code.co_names)
        for n in 이름:
            if n in 표["해시"]:
                필요.add((f, n))
            if n in 표["가져옴"]:
                m, 원래 = 표["가져옴"][n]
                if (mf := _모듈파일(m)):
                    필요.add((mf, 원래))
            if n in 표["모듈"] and (mf := _모듈파일(표["모듈"][n])) and (mt := _정의표(mf)):
                필요.update((mf, x) for x in 이름 if x in mt["해시"])
    return {k: _정의해시(*k) for k in 필요}


class 흐름기록:
    """ASGI 미들웨어(OrgId주입 안쪽). 흐름 POST 는 단계 폴더로 기록 · 응답 재생, 그 밖의 쓰기는 메모리 저장소 또는 막기, 나머지는 통과."""

    def __init__(self, app, 기록: 기록장, 설정, 리로드: 핫리로드):
        self.app, self.기록, self.설정, self.리로드 = app, 기록, 설정, 리로드
        self.잠금 = asyncio.Lock()
        self.재생: dict[str, dict] = {}
        self.비교파일 = 기록.폴더.parent / "판정비교.json"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        경로, 방법 = scope.get("path", ""), scope.get("method", "GET")
        if 방법 == "POST" and 경로 in 흐름경로:
            return await self._흐름(scope, receive, send, 경로)
        if 방법 in ("POST", "PUT", "PATCH", "DELETE") and not self._mock():
            if 경로 == "/api/plans" or 경로 in _통과_쓰기:
                return await self.app(scope, receive, send)   # 계획 생성은 routes_plans._live_생성(메모리)으로 간다
            본문 = await _본문_읽기(receive)
            try:
                r = self.기록.메모리.쓰기_처리(방법, 경로, 본문) if self.기록.메모리 else None
            except Exception as e:  # noqa: BLE001  (pydantic 검증 오류 등)
                return await _json_보내기(send, 422, {"오류": f"{type(e).__name__}: {str(e)[:300]}", "상태": 422})
            if r is not None:
                return await _json_보내기(send, r[0], r[1])
            self.기록.막힌_쓰기.append({"단계": self.기록.현재단계, "sql": f"{방법} {경로}"})
            print(f"  [쓰기 막힘] {방법} {경로} — 디버그 서버는 운영 DB 에 쓰지 않는다", flush=True)
            return await _json_보내기(send, 403, {"오류": "디버그 서버는 운영 DB 에 쓰지 않습니다(읽기 전용).", "상태": 403})
        return await self.app(scope, receive, send)

    @staticmethod
    def _mock() -> bool:
        from server._common import MOCK
        return MOCK

    async def _흐름(self, scope, receive, send, 경로: str):
        import anyio
        from server import research_sessions
        본문b = await _본문_읽기(receive)
        try:
            본문 = json.loads(본문b or b"{}")
        except ValueError:
            본문 = {}
        주 = scope.get("suddoe_주체")
        org = str(주.org_id) if 주 is not None and 주.검증됨 and 주.org_id else None
        acc = str(주.account_id) if org and 주.account_id else None
        이름 = 경로.rsplit("/", 1)[-1]
        async with self.잠금:
            await anyio.to_thread.run_sync(self.리로드.확인)
            키 = json.dumps([경로, {k: v for k, v in 본문.items() if k not in _재생_제외}, org, acc],
                           ensure_ascii=False, sort_keys=True, default=str)
            옛 = self.재생.get(키)
            바뀐 = sorted({k for k, h in (옛 or {}).get("의존", {}).items() if _정의해시(*k) != h})
            재생 = (옛 is not None and 경로 != "/api/judge" and getattr(self.설정, "응답재생", True) and not 바뀐
                  and (옛["조사ID"] is None or research_sessions.조회(옛["조사ID"], org, acc) is not None))
            with self.기록.단계(이름):
                (self.기록.단계폴더 / "요청.json").write_text(_json({"경로": 경로, "본문": 본문}), encoding="utf-8")
                t = time.time()
                if 재생:
                    await self._재생_보내기(옛, send, org, acc)
                    print(f"[재생] {self.기록.현재단계} {time.time() - t:.1f}s (바뀐 파일 없음 · 실행한 정의 {len(옛['의존'])}개 그대로) ← "
                          f"{옛['폴더'].parent.name}/{옛['폴더'].name}", flush=True)
                    return
                if 바뀐:
                    대상 = {os.path.normcase(str(_모듈경로(m))) for m in self.리로드.대상}
                    파일별: dict[str, list[str]] = {}
                    for f, n in 바뀐:
                        파일별.setdefault(f, []).append(n)
                    밖 = sorted({Path(f).name for f, _ in 바뀐 if os.path.normcase(f) not in 대상})
                    print("[새로 실행] 바뀐 파일: " + ", ".join(
                        f"{Path(f).name}" + ("" if ns == ["*"] else f"({', '.join(ns[:5])}{' …' if len(ns) > 5 else ''})")
                        for f, ns in 파일별.items()), flush=True)
                    if 밖:
                        print(f"  ⚠ 핫 리로드 대상이 아닌 파일이 바뀌었다: {', '.join(밖)} — 서버를 다시 시작해야 반영된다", flush=True)
                elif 경로 == "/api/judge":
                    print("[새로 실행] 판정은 늘 다시 돈다(응답 재생 없음)", flush=True)
                else:
                    print(f"[새로 실행] {'처음 보는 요청' if 옛 is None else '재생할 수 없음(조사 세션 없음 · 응답 재생 꺼짐)'}", flush=True)
                if 경로 == "/api/judge":
                    self.기록.판정_결과 = None
                    s = research_sessions.조회(본문.get("조사ID"), org, acc) if 본문.get("조사ID") else None
                    if s is not None:
                        s.판정결과 = None        # 세션에 둔 지난 판정을 쓰지 않고 판정을 다시 돌린다
                캐시시작 = len(self.기록.캐시저장)
                받음 = {"상태": 500, "헤더": [], "본문": bytearray()}
                보냄 = False

                async def receive2():
                    nonlocal 보냄
                    if not 보냄:
                        보냄 = True
                        return {"type": "http.request", "body": 본문b, "more_body": False}
                    return await receive()

                async def send2(msg):
                    if msg["type"] == "http.response.start":
                        받음["상태"], 받음["헤더"] = msg["status"], list(msg.get("headers") or [])
                    elif msg["type"] == "http.response.body":
                        받음["본문"] += msg.get("body", b"")
                    await send(msg)
                try:
                    await self.app(scope, receive2, send2)
                finally:
                    형식 = next((v.decode("latin-1") for k, v in 받음["헤더"] if k.lower() == b"content-type"), "")
                    결과 = _응답값(받음["상태"], 형식, 받음["본문"].decode("utf-8", "replace"))
                    (self.기록.단계폴더 / "응답.json").write_text(_json(결과), encoding="utf-8")
                    조사ID = 본문.get("조사ID") or next((v.get("조사ID") for n, v in 결과.get("이벤트") or []
                                                     if isinstance(v, dict) and v.get("조사ID")), None)
                    s = research_sessions.조회(조사ID, org, acc) if 조사ID else None
                    # 의존: 호출 추적이 본 코드의 최상위 정의. 호출 추적을 껐으면 핫 리로드 대상 파일 전체
                    의존 = (await anyio.to_thread.run_sync(_의존_정의, set(self.기록.추적기.코드)) if self.설정.호출추적
                          else {(str(_모듈경로(m)), "*"): _파일해시(_모듈경로(m)) for m in self.리로드.대상})
                    if 경로 != "/api/judge" and not _실패_응답인가(결과) and (조사ID is None or s is not None):
                        self.재생[키] = {"상태": 받음["상태"], "헤더": 받음["헤더"], "본문": bytes(받음["본문"]),
                                       "의존": 의존,
                                       "조사ID": 조사ID, "세션": _세션_사본(s) if s is not None else None,
                                       "캐시저장": self.기록.캐시저장[캐시시작:], "결과": 결과, "폴더": self.기록.단계폴더}
                    self._요약(경로, 본문, 결과)

    async def _재생_보내기(self, 옛: dict, send, org, acc):
        from server import research_sessions, response_cache
        if 옛["조사ID"] and 옛["세션"] is not None:
            s = research_sessions.조회(옛["조사ID"], org, acc)
            if s is not None:
                _세션_되돌리기(s, 옛["세션"])      # 녹화한 요청이 끝났을 때의 조사 세션으로(뒤 단계가 바꾼 값을 지운다)
        for c in 옛["캐시저장"]:
            response_cache.저장(c["k"], c["값"], 종류=c["종류"], org=None, 데이터버전_해시=c["데이터버전_해시"], ttl초=c["ttl초"])
        for n, v in 옛["결과"].get("이벤트") or []:
            self.기록.이벤트.append({"단계": self.기록.현재단계, "이벤트": n, "값": v, "t": round(time.time(), 3), "재생": True})
        (self.기록.단계폴더 / "응답.json").write_text(_json(옛["결과"]), encoding="utf-8")
        (self.기록.단계폴더 / "재생.txt").write_text(f"녹화한 응답을 그대로 보냈다: {옛['폴더']}\n", encoding="utf-8")
        await send({"type": "http.response.start", "status": 옛["상태"], "headers": 옛["헤더"]})
        await send({"type": "http.response.body", "body": 옛["본문"], "more_body": False})

    def _요약(self, 경로: str, 본문: dict, 결과: dict):
        try:
            if 경로 == "/api/normalize":
                v = 결과.get("결과") or {}
                검 = v.get("입력검증") or {}
                print(f"  [① 결과] 품목 {v.get('품목')} · 금액 {v.get('금액')} · 입력 검증 {검.get('판단')} · 다음행동 {검.get('다음행동')}")
            elif 경로 == "/api/questions":
                v = 결과.get("완료") or 결과.get("값") or {}
                분류 = v.get("분류") or {}
                print(f"  [② · ③ 결과] 상태 {v.get('상태')} · 다음행동 {v.get('다음행동')} · 분류 {분류.get('표시문') or '-'} · "
                      f"질문 {len(v.get('질문') or [])}개")
                for x in [x for x in self.기록.분류디버그 if x["단계"] == self.기록.현재단계]:
                    분류디버그_보기(x)
            elif 경로 == "/api/judge":
                self._판정_요약(본문, 결과)
        except Exception as e:  # noqa: BLE001  요약 출력이 응답을 망치지 않게
            print(f"  [요약 실패] {type(e).__name__}: {e}")

    def _판정_요약(self, 본문: dict, 결과: dict):
        화면 = 결과.get("결과") or {}
        내부 = self.기록.판정_결과 or {}
        지금 = {"판정": 화면.get("판정"), "판정실패": bool(화면.get("판정실패")),
              "인용": [f"{c.get('doc_id')} {c.get('조번호')}" for c in 화면.get("인용") or []],
              "검증코드": list(내부.get("검증코드") or []), "폴더": str(self.기록.단계폴더), "시각": _지금()}
        print(f"  [판정 결과] 판정 {지금['판정']}{' (판정실패)' if 지금['판정실패'] else ''} · 검증코드 {지금['검증코드'] or '없음'} · "
              f"인용 {len(지금['인용'])}건")
        for c in 지금["인용"]:
            print(f"    · {c}")
        지금["모델별"] = 앙상블_보기(내부.get("모델"), 내부.get("검증사유"))
        비교키 = hashlib.sha1(json.dumps({k: v for k, v in 본문.items() if k not in _비교_제외},
                                        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        try:
            기록들 = json.loads(self.비교파일.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            기록들 = {}
        앞 = 기록들.get(비교키)
        if 앞:
            print(f"  [같은 입력의 이전 판정과 비교] 이전: {Path(앞['폴더']).parent.name}/{Path(앞['폴더']).name}")
            print(f"    판정: {앞['판정']} → {지금['판정']}" if 앞["판정"] != 지금["판정"] else f"    판정: 같음({지금['판정']})")
            for 칸 in ("인용", "검증코드"):
                더, 빠 = [x for x in 지금[칸] if x not in 앞[칸]], [x for x in 앞[칸] if x not in 지금[칸]]
                print(f"    {칸}: 같음" if not (더 or 빠) else f"    {칸}: + {더 or '-'} / - {빠 or '-'}")
            if 앞.get("모델별") or 지금["모델별"]:          # 판정 앙상블의 모델별 판정(이전 기록에 없으면 「-」)
                옛 = {x["모델"]: x["판정"] for x in 앞.get("모델별") or []}
                print("    모델별 판정: " + " · ".join(f"{x['모델']} {옛.get(x['모델'], '-')} → {x['판정']}" for x in 지금["모델별"])
                      if 지금["모델별"] else f"    모델별 판정: 이전 {옛} → 이번 없음")
        else:
            print("  [같은 입력의 이전 판정과 비교] 이전 판정 없음(처음)")
        if not 지금["판정실패"]:
            기록들[비교키] = 지금
            self.비교파일.write_text(_json(기록들), encoding="utf-8")


async def _본문_읽기(receive) -> bytes:
    조각 = []
    while True:
        m = await receive()
        if m["type"] != "http.request":
            break
        조각.append(m.get("body", b""))
        if not m.get("more_body"):
            break
    return b"".join(조각)


async def _json_보내기(send, 상태: int, 값):
    from fastapi.encoders import jsonable_encoder
    b = json.dumps(jsonable_encoder(값), ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": 상태,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(b)).encode())]})
    await send({"type": "http.response.body", "body": b})


# ── 서버 방식: 실행 ───────────────────────────────────────────
def _포트_사용중(포트: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 포트))
        except OSError:
            return True
    return False


def _작업묶기(p: subprocess.Popen):
    """Windows Job(KILL_ON_JOB_CLOSE)에 넣는다. 이 파이썬이 어떻게 끝나든(디버그 중지 포함) npm · next 가 같이 끝난다."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _기본(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class _확장(ctypes.Structure):
            _fields_ = [("기본", _기본), ("io", ctypes.c_uint64 * 6), ("x", ctypes.c_size_t * 4)]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        정보 = _확장()
        정보.기본.LimitFlags = 0x2000                                      # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(정보), ctypes.sizeof(정보)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(p._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        return job                                                          # 핸들을 들고 있어야 한다(닫히면 끝난다)
    except Exception as e:  # noqa: BLE001
        print(f"  (Job 묶기 실패 {type(e).__name__}: 키트를 강제로 끄면 npm 이 남을 수 있다)")
        return None


def _프론트_시작(설정) -> tuple | None:
    폴더 = Path(설정.프론트폴더)
    포트 = getattr(설정, "프론트포트", 3000)
    if not (폴더 / "package.json").exists():
        print(f"⚠ 프론트 폴더가 없다: {폴더} — 설정.프론트폴더 를 고치거나 프론트_자동실행 = False")
        return None
    if _포트_사용중(포트):
        print(f"⚠ 포트 {포트} 를 다른 프로그램이 쓴다. 프론트를 띄우지 않는다(이미 띄운 프론트가 있으면 그대로 쓴다)")
        return None
    if not (폴더 / ".env.local").exists():
        print("⚠ 프론트 .env.local 이 없다: Supabase 로그인 없이(게스트) 동작한다")
    # 키 · DSN 같은 운영 값은 넘기지 않는다
    env = {k: v for k, v in os.environ.items() if not (k.startswith("SUDDOE_") or k.startswith("DASHSCOPE") or k.startswith("HF_"))}
    env["NEXT_PUBLIC_API_BASE"] = f"http://localhost:{설정.서버포트}"
    로그 = 작업트리 / "debug" / "실행기록" / "프론트.log"
    p = subprocess.Popen(["npm.cmd" if os.name == "nt" else "npm", "run", "dev", "--", "-p", str(포트)], cwd=폴더, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    job = _작업묶기(p)

    def 읽기():
        with open(로그, "ab") as f:
            for 줄 in iter(p.stdout.readline, b""):
                f.write(줄)
                f.flush()
                글 = 줄.decode("utf-8", "replace").strip()
                if "Ready" in 글 or "rror" in 글 or "Local:" in 글:
                    print(f"  [프론트] {글}", flush=True)
    t = threading.Thread(target=읽기, name="프론트로그", daemon=True)
    t.pydev_do_not_trace = True
    t.is_pydev_daemon_thread = True
    t.start()
    print(f"프론트: npm run dev ({폴더.name}, http://localhost:{포트}, NEXT_PUBLIC_API_BASE={env['NEXT_PUBLIC_API_BASE']}) · 로그 {로그}")
    return p, job


def _프론트_끝내기(프론트):
    if not 프론트:
        return
    p = 프론트[0]
    if p.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.terminate()


def 서버_실행(기록: 기록장, 설정, 리로드: 핫리로드):
    """uvicorn 을 이 프로세스(메인 스레드)에서 띄운다. --reload 는 쓰지 않는다(터널 · KURE · 조사 세션이 사라진다). 끝날 때까지 돌아오지 않는다."""
    import atexit
    import uvicorn
    from starlette.middleware import Middleware
    from server._common import MOCK
    from server.main import app
    기록.메모리 = 메모리저장소(기록)
    if not MOCK:
        기록.메모리.설치()
    # OrgId주입(로그인 주체) 안쪽, 라우터 바로 바깥. 앱이 첫 요청을 받기 전이라 미들웨어 목록에 더할 수 있다
    app.user_middleware.append(Middleware(흐름기록, 기록=기록, 설정=설정, 리로드=리로드))
    if _포트_사용중(설정.서버포트):
        raise RuntimeError(f"포트 {설정.서버포트} 를 다른 프로그램이 쓴다. 설정.서버포트 를 바꾸세요.")
    프론트 = _프론트_시작(설정) if getattr(설정, "프론트_자동실행", True) else None
    atexit.register(_프론트_끝내기, 프론트)
    print(f"\n디버그 서버: http://127.0.0.1:{설정.서버포트} · 기록 폴더 {기록.폴더}")
    print(f"  흐름 요청(①②③ · 판정)마다 단계 폴더를 남긴다 · 핫 리로드 {'켬' if 리로드.켜짐 else '끔'}({', '.join(리로드.대상)}) · "
          f"응답 재생 {'켬' if getattr(설정, '응답재생', True) else '끔'} · 로그인 {getattr(설정, '서버_로그인', '화면')}")
    print(f"  브라우저: http://localhost:{getattr(설정, '프론트포트', 3000)} 에서 로그인해 점검을 시작한다. 끄기: Ctrl+C (또는 디버그 중지)\n",
          flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=설정.서버포트, log_level="info", access_log=False,
                                      lifespan="on", timeout_keep_alive=75)).run()
    finally:
        _프론트_끝내기(프론트)


# ── 검색만 다시(규정조회 · 규정검색 단계, LLM 없음) ───────────────────
_검색_단계함수 = ("_온톨로지단계", "_새_검색단계", "_룰조회", "_검색단계")
_검색_값 = ("온톨로지", "검색결과", "l3본문", "초기조회상태", "문맥")


class _검색끝(Exception):
    pass


def _녹화_검색값(폴더: Path) -> dict:
    값 = {}
    for f in sorted((폴더 / "판정내부").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("함수") in _검색_단계함수:
            for k in _검색_값:
                if k in (d.get("바뀐_상태") or {}):
                    값[k] = d["바뀐_상태"][k]
    return 값


def _조문_표시(종류: str, ids: list) -> dict:
    """청크 · 조문 번호 → 「doc_id 조번호」(운영 DB 읽기)."""
    ids = sorted({i for i in ids if isinstance(i, int)})
    if not ids:
        return {}
    from _lib import db
    sql = ("SELECT chunk_id, doc_id, 조번호 FROM corpus.chunks WHERE chunk_id = ANY(%s)" if 종류 == "청크" else
           "SELECT article_id, doc_id, 조번호 FROM corpus.doc_articles WHERE article_id = ANY(%s)")
    try:
        with db.connect(connect_timeout=5, autocommit=True) as c:
            return {r[0]: f"{r[1]} {r[2]}" for r in c.execute(sql, (ids,)).fetchall()}
    except Exception:  # noqa: BLE001
        return {}


def _순위_비교(이름: str, 옛: list, 새: list, 표시=lambda x: str(x)) -> int:
    옛순 = {x: i + 1 for i, x in reversed(list(enumerate(옛)))}
    새순 = {x: i + 1 for i, x in reversed(list(enumerate(새)))}
    추가 = [x for x in 새순 if x not in 옛순]
    빠짐 = [x for x in 옛순 if x not in 새순]
    이동 = [(x, 옛순[x], 새순[x]) for x in 새순 if x in 옛순 and 옛순[x] != 새순[x]]
    print(f"  {이름}: 녹화 {len(옛순)}건 → 지금 {len(새순)}건" + (" · 같음" if not (추가 or 빠짐 or 이동) else ""))
    for x in 추가:
        print(f"    + {새순[x]}위 {표시(x)}")
    for x in 빠짐:
        print(f"    - (녹화 {옛순[x]}위) {표시(x)}")
    for x, a, b in 이동:
        print(f"    ↕ {a}위 → {b}위 {표시(x)}")
    return len(추가) + len(빠짐) + len(이동)


def 검색_다시_실행(기록: 기록장, 설정, 리로드: 핫리로드 | None, 단계폴더) -> dict:
    """녹화한 판정 단계(판정_인자.json)로 orchestrate.판정() 을 돌리되 근거구성 단계 앞에서 멈춘다(입력확인 · 규정조회 · 규정검색만).
    LLM 은 막는다(추가 규정 조회는 예산이 없어 건너뛴다). 녹화한 판정내부/*.json 과 규범 · 근거 조문 · 검색 청크 · 참조 확장 · L3 를 비교한다."""
    import llm_qwen
    import orchestrate
    폴더 = Path(단계폴더)
    if not (폴더 / "판정_인자.json").exists():
        raise FileNotFoundError(f"판정_인자.json 이 없다: {폴더} (판정 단계 폴더를 주세요)")
    if 리로드 is not None:
        리로드.확인()
    인자 = json.loads((폴더 / "판정_인자.json").read_text(encoding="utf-8"))
    질문 = 인자.pop("질문")
    인자["기록"] = False
    잡음: dict = {}

    def 멈추기(상태, *a, **kw):
        잡음["상태"] = 상태
        raise _검색끝

    원래 = {n: getattr(orchestrate, n) for n in ("_새_프롬프트단계", "_프롬프트구성단계")}
    원래LLM = llm_qwen._client_얻기
    원래멈춤 = 설정.단계마다_멈춤

    def LLM_막음():
        raise RuntimeError("검색_다시 는 LLM 을 부르지 않는다")
    원래판정 = getattr(orchestrate.판정, "__wrapped__", orchestrate.판정)
    r = None
    try:
        for n in 원래:
            setattr(orchestrate, n, 멈추기)
        llm_qwen._client_얻기 = LLM_막음
        설정.단계마다_멈춤 = False
        with 기록.단계("검색다시"):
            (기록.단계폴더 / "판정_인자.json").write_text(_json({"질문": 질문, **인자}), encoding="utf-8")
            r = 원래판정(질문, **인자)
    finally:
        for n, f in 원래.items():
            setattr(orchestrate, n, f)
        llm_qwen._client_얻기 = 원래LLM
        설정.단계마다_멈춤 = 원래멈춤
    상태 = 잡음.get("상태")
    if 상태 is None:
        r = r or {}
        print(f"\n검색 단계 전에 판정이 끝났다: 판정 {r.get('판정')} · 게이트 {r.get('게이트')} · 실패단계 {r.get('실패단계')} · "
              f"{str(r.get('검증사유') or r.get('요약'))[:200]}")
        return {"끝": r}
    새 = json.loads(_json({k: getattr(상태, k, None) for k in _검색_값}))
    옛 = _녹화_검색값(폴더)
    print(f"\n━━━ 검색만 다시: 녹화 {폴더.parent.name}/{폴더.name} ↔ 지금 코드 ━━━")
    for 쪽, 라벨 in ((옛, "녹화"), (새, "지금")):
        추가 = ((쪽.get("온톨로지") or {}).get("추가조사상태") or {})
        if 추가.get("추가규범수") or 추가.get("추가근거수"):
            print(f"  ※ {라벨} 실행은 추가 규정 조회로 규범 {추가.get('추가규범수')} · 근거 {추가.get('추가근거수')}건을 더했다"
                  f"{'(검색_다시 는 LLM 을 부르지 않아 추가 조회를 건너뛴다 — 아래 차이 일부는 여기서 온다)' if 라벨 == '녹화' else ''}")
    on, nn = 옛.get("온톨로지") or {}, 새.get("온톨로지") or {}
    차이 = _순위_비교("적용 규범", [x.get("norm_id") for x in (on.get("적용") or {}).get("규범") or []],
                   [x.get("norm_id") for x in (nn.get("적용") or {}).get("규범") or []])
    조문 = lambda o: [f"{x.get('doc_id')} {x.get('조번호')}{' ' + x['항호'] if x.get('항호') else ''}" for x in o.get("근거행") or []]
    차이 += _순위_비교("근거 조문(온톨로지 근거행)", 조문(on), 조문(nn))
    os_, ns = 옛.get("검색결과") or {}, 새.get("검색결과") or {}
    청크 = _조문_표시("청크", [*(os_.get("top5") or []), *(ns.get("top5") or [])])
    차이 += _순위_비교("검색 청크(상위)", os_.get("top5") or [], ns.get("top5") or [], lambda x: f"청크 {x} {청크.get(x, '')}")
    확장 = _조문_표시("조문", [*(os_.get("참조확장") or []), *(ns.get("참조확장") or [])])
    차이 += _순위_비교("참조 확장 조문", os_.get("참조확장") or [], ns.get("참조확장") or [], lambda x: f"조문 {x} {확장.get(x, '')}")
    l3 = lambda v: [f"{x.get('doc_id') or ''} {x.get('조번호') or x.get('article_id') or ''}".strip() for x in v or [] if isinstance(x, dict)]
    차이 += _순위_비교("기관 규정(L3) 조문", l3(옛.get("l3본문")), l3(새.get("l3본문")))
    print(f"  최고 유사도: {os_.get('최고유사도')} → {ns.get('최고유사도')} · 문맥 항목 수: "
          f"{(옛.get('문맥') or {}).get('항목수')} → {(새.get('문맥') or {}).get('항목수')}")
    print(f"  차이 {차이}건 · 기록 {기록.단계폴더}")
    return {"녹화": 옛, "지금": 새, "차이": 차이}
