# -*- coding: utf-8 -*-
"""요청 제한: 서비스 운영 기간, 일일 판정 한도, IP 별 시간당 한도.

/api/normalize·/api/judge 가 첫 줄에서 `제한.통과(ip)` 를 부르고, 막히면 429 를 낸다.
환경변수: SUDDOE_DAILY_CAP(기본 2000), SUDDOE_IP_HOURLY(기본 60),
SUDDOE_OPEN_FROM·SUDDOE_OPEN_UNTIL(ISO 시각, 비우면 기간 제한 없음).
카운터는 프로세스 메모리에만 있어 재시작하면 초기화된다. AnyIO 워커 스레드가 동시에 부르므로 락 안에서만 바꾼다.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from server._common import _int환경


class 요청제한:
    def __init__(self) -> None:
        self.일일캡 = _int환경("SUDDOE_DAILY_CAP", 2000)
        self.IP시간당 = _int환경("SUDDOE_IP_HOURLY", 60)
        self.운영시작 = os.environ.get("SUDDOE_OPEN_FROM", "").strip()
        self.운영종료 = os.environ.get("SUDDOE_OPEN_UNTIL", "").strip()

        self._날짜 = self._오늘()
        self.오늘_호출 = 0
        self._ip: dict[str, deque] = {}
        self.차단: dict[str, int] = {}
        self._락 = threading.Lock()

    @staticmethod
    def _오늘() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")      # 일일 한도는 UTC 자정에 초기화

    def 운영기간인가(self) -> tuple[bool, str]:
        지금 = datetime.now(timezone.utc)
        for 값, 방향 in ((self.운영시작, "전"), (self.운영종료, "후")):
            if not 값:
                continue
            try:
                t = datetime.fromisoformat(값)
            except ValueError:
                continue                      # 형식이 틀리면 제한 없음으로 본다
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if (방향 == "전" and 지금 < t) or (방향 == "후" and 지금 > t):
                return False, (f"서비스 운영 기간이 아닙니다 "
                               f"(운영 {self.운영시작 or '-'} ~ {self.운영종료 or '-'})")
        return True, ""

    def 통과(self, ip: str) -> tuple[bool, str]:
        """(통과 여부, 거부 사유). 통과하면 일일·IP 카운트를 하나 올린다."""
        열림, 사유 = self.운영기간인가()
        with self._락:
            # 차단 집계 키 '개방창' 은 /admin/cost 응답 형식이라 유지한다.
            if not 열림:
                self.차단["개방창"] = self.차단.get("개방창", 0) + 1
                return False, 사유
            if self._날짜 != self._오늘():
                self._날짜, self.오늘_호출, self._ip = self._오늘(), 0, {}
            if self.오늘_호출 >= self.일일캡:
                self.차단["일일캡"] = self.차단.get("일일캡", 0) + 1
                return False, f"오늘의 판정 한도({self.일일캡}건)를 모두 썼습니다. 내일 다시 시도해 주세요."
            지금 = time.time()
            q = self._ip.setdefault(ip, deque())
            while q and 지금 - q[0] > 3600:
                q.popleft()
            if len(q) >= self.IP시간당:
                self.차단["IP시간당"] = self.차단.get("IP시간당", 0) + 1
                return False, f"시간당 요청 한도({self.IP시간당}건)를 넘었습니다. 잠시 후 다시 시도해 주세요."
            q.append(지금)
            self.오늘_호출 += 1
            return True, ""

    def 환급(self) -> None:
        """캐시로 답한 요청은 LLM 을 부르지 않았으므로 일일 카운트를 하나 되돌린다."""
        with self._락:
            self.오늘_호출 = max(0, self.오늘_호출 - 1)

    def 상태(self) -> dict:
        열림, 사유 = self.운영기간인가()
        with self._락:
            return {"오늘": self._날짜, "오늘_호출": self.오늘_호출, "일일캡": self.일일캡,
                    "IP시간당": self.IP시간당, "추적중인_IP": len(self._ip),
                    "개방": {"열림": 열림, "사유": 사유,
                            "시작": self.운영시작 or None, "종료": self.운영종료 or None},
                    "차단": dict(self.차단)}


제한 = 요청제한()
