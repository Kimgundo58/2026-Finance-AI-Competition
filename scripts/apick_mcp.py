# -*- coding: utf-8 -*-
"""APICK MCP의 검색·본문 추출만 사용하는 작은 클라이언트."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

ENDPOINT = "https://apick.app/mcp/web"
MAX_RESPONSE = 400_000


class ApickError(Exception):
    """외부 응답 내용이나 인증정보를 포함하지 않는 오류."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def public_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        if (p.scheme not in {"http", "https"} or not host or p.username or p.password
                or p.port not in (None, 80, 443)):
            return False
        # 검색 결과의 임의 query는 개인값·인증정보일 수 있다. 문서/페이지를
        # 고르는 제한된 값만 원형 보존한다. 알 수 없는 값은 삭제하지 않고 거부한다.
        if p.query:
            pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            numeric = {"id", "article", "article_id", "articleid", "doc", "doc_id", "document_id",
                       "page", "p", "post", "post_id", "postid", "no", "seq", "idx"}
            languages = {"lang", "language", "locale"}
            # 사이트별 공개 글·영상 식별자. 다른 사이트에서는 받지 않는다.
            naver_blog = host in {"blog.naver.com", "m.blog.naver.com"}
            youtube = host in {"www.youtube.com", "youtube.com", "m.youtube.com"}
            for key, value in pairs:
                key = key.lower()
                if key in numeric and re.fullmatch(r"[0-9]{1,9}", value):
                    continue
                if naver_blog and key == "blogid" and re.fullmatch(r"[A-Za-z0-9_-]{1,50}", value):
                    continue
                if naver_blog and key == "logno" and re.fullmatch(r"[0-9]{12,15}", value):
                    continue
                if youtube and key == "v" and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                    continue
                if key in languages and re.fullmatch(r"(?:en|ko|en-us|ko-kr)", value, re.I):
                    continue
                return False
        # 페이지 내 공개 위치만 허용하며 원래 URL은 그대로 유지한다.
        if p.fragment and not re.fullmatch(r"(?:top|section-?[0-9]{1,5}|article-?[0-9]{1,5})", p.fragment, re.I):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host) or any(not x or x.startswith('-') or x.endswith('-') for x in host.split('.')):
                return False
            # 숫자형 IP 대체 표기 차단
            if re.fullmatch(r"[0-9.x]+", host):
                return False
        path = unquote(p.path).lower()
        if any(x in path for x in ("login", "signin", "checkout", "payment", "account", "token", "session", "auth", "로그인", "결제")):
            return False
        return True
    except (ValueError, UnicodeError):
        return False


# 검색 결과 주소의 추적·표시용 query. 지워도 같은 페이지를 가리킨다.
_TRACKING = re.compile(r"(?:utm_[a-z_]+|srsltid|fbclid|gclid|proxyreferer|notrackingcode|categoryno|tl|ref|from)", re.I)


def strip_tracking(url: str) -> str:
    """추적용 query 를 지운 주소. 나머지 query 는 그대로 두고 public_url 이 판단한다."""
    if not isinstance(url, str):
        return url
    try:
        p = urlsplit(url)
        if not p.query:
            return url
        pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
    except ValueError:
        return url
    kept = [(k, v) for k, v in pairs if not _TRACKING.fullmatch(k)]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(kept), p.fragment))


def _key() -> str:
    file_name = os.environ.get("SUDDOE_APICK_API_KEY_FILE", "").strip()
    key = "" if file_name else os.environ.get("SUDDOE_APICK_API_KEY", "").strip()
    if not key:
        path = Path(file_name) if file_name else Path(__file__).resolve().parent.parent / ".env.apick.key"
        try:
            key = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise ApickError("API 키 파일을 읽을 수 없습니다") from None
        prefix = "SUDDOE_APICK_API_KEY="
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
    if not key or any(c.isspace() or ord(c) < 33 for c in key):
        raise ApickError("API 키가 없습니다")
    return key


def parse_response(raw: bytes, content_type: str, request_id: int = 1) -> dict:
    if len(raw) > MAX_RESPONSE:
        raise ApickError("응답 크기 제한")
    try:
        data = raw.decode("utf-8")
        if "text/event-stream" in content_type.lower():
            messages = []
            for event in re.split(r"\r?\n\r?\n", data):
                lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
                if lines:
                    messages.append(json.loads("\n".join(lines)))
            matches = [m for m in messages if isinstance(m, dict) and m.get("id") == request_id]
            if len(matches) != 1:
                raise ValueError()
            result = matches[0]
        else:
            result = json.loads(data)
        if not isinstance(result, dict) or result.get("id") != request_id or result.get("jsonrpc") != "2.0":
            raise ValueError()
        if "error" in result:
            raise ApickError("MCP 프로토콜 오류")
        result = result.get("result")
        if not isinstance(result, dict):
            raise ValueError()
        if result.get("isError") is True:
            contents = result.get("content")
            if isinstance(contents, list) and any(
                isinstance(item, dict) and isinstance(item.get("text"), str)
                and "허용되지 않은 IP주소" in item["text"] for item in contents
            ):
                raise ApickError("검색 서비스 연결 설정 오류", code="APICK_IP_DENIED")
            raise ApickError("MCP 도구 오류")
        return result
    except (ValueError, UnicodeError, TypeError):
        raise ApickError("MCP 응답 오류") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApickClient:
    def __init__(self, api_key: str | None = None):
        self._api_key = api_key

    def call(self, name: str, arguments: dict, timeout: float) -> dict:
        if name == "google_search":
            if set(arguments) != {"keyword"} or not isinstance(arguments["keyword"], str) or not 1 <= len(arguments["keyword"]) <= 200:
                raise ApickError("검색 입력 오류")
        elif name == "url_to_markdown":
            if set(arguments) != {"url", "format", "max_chars"} or not public_url(arguments["url"]) or arguments["format"] != "markdown" or arguments["max_chars"] != 30000:
                raise ApickError("본문 입력 오류")
        else:
            raise ApickError("허용되지 않은 도구")
        if timeout <= 0:
            raise ApickError("시간 제한")
        key = self._api_key or _key()
        if not key or any(c in key for c in "\r\n"):
            raise ApickError("API 키 오류")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(ENDPOINT, data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, method="POST")
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                return parse_response(raw, response.headers.get("Content-Type", ""))
        except ApickError:
            raise
        except urllib.error.HTTPError as exc:
            # 본문·헤더에는 키나 입력값이 있을 수 있다. 상태 코드만 보존한다.
            raise ApickError(f"APICK HTTP 오류 {exc.code}") from None
        except Exception:
            raise ApickError("APICK 요청 실패") from None
