# -*- coding: utf-8 -*-
"""업로드 파일의 실제 형식 판별. L3 규정 업로드(routes_l3)와 계획 첨부(routes_attachments)가 같이 쓴다."""
from __future__ import annotations

import io
import zipfile


def 실제형식(본문: bytes) -> str:
    """파일 내용으로 실제 형식을 판별한다. 확장자는 신뢰하지 않는다.

    ZIP 계열(HWPX·XLSX·DOCX·PPTX)은 매직바이트가 같아 내부 항목 이름으로 구분한다.
    OLE 헤더는 구형 HWP·DOC·XLS 가 공유하므로 'hwp' 판정이 확정은 아니다.
    """
    if 본문[:5] == b"%PDF-":
        return "pdf"
    if 본문[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "hwp"                       # OLE 복합문서 (구형 HWP·DOC·XLS 공통)
    if 본문[:3] == b"\xff\xd8\xff":
        return "jpg"
    if 본문[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if 본문[:4] == b"PK\x03\x04":
        try:
            이름들 = zipfile.ZipFile(io.BytesIO(본문)).namelist()
        except (zipfile.BadZipFile, OSError):
            return "손상된zip"
        if any(n.startswith("Contents/") for n in 이름들):
            return "hwpx"
        if any(n.startswith("xl/") for n in 이름들):
            return "xlsx"
        if any(n.startswith("word/") for n in 이름들):
            return "docx"
        if any(n.startswith("ppt/") for n in 이름들):
            return "pptx"
        return "zip"
    return "알수없음"
