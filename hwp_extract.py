# -*- coding: utf-8 -*-
"""HWP v5 본문 텍스트 추출기 (olefile + zlib만 사용)."""
import olefile, zlib, struct, sys, io

HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51  # 67

# 인라인/확장 컨트롤 문자 (다음 14바이트가 컨트롤 데이터)
INLINE = {4, 5, 6, 7, 8, 9, 19, 20}
EXTENDED = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}


def get_sections(ole):
    names = ['/'.join(s) for s in ole.listdir()]
    secs = sorted([n for n in names if n.startswith('BodyText/Section')],
                  key=lambda n: int(n.replace('BodyText/Section', '')))
    return secs


def is_compressed(ole):
    header = ole.openstream('FileHeader').read()
    # 36번째 바이트부터 4바이트가 속성 플래그, bit0 = 압축 여부
    flags = struct.unpack('<I', header[36:40])[0]
    return bool(flags & 1)


def parse_records(data):
    pos, n = 0, len(data)
    while pos < n - 3:
        (h,) = struct.unpack('<I', data[pos:pos + 4])
        tag = h & 0x3FF
        size = (h >> 20) & 0xFFF
        pos += 4
        if size == 0xFFF:
            (size,) = struct.unpack('<I', data[pos:pos + 4])
            pos += 4
        yield tag, data[pos:pos + size]
        pos += size


def decode_para_text(payload):
    out = []
    i, n = 0, len(payload)
    while i + 1 < n:
        (code,) = struct.unpack('<H', payload[i:i + 2])
        if code in INLINE:
            i += 16
        elif code in EXTENDED:
            i += 16
        elif code == 10:      # 줄바꿈
            out.append('\n'); i += 2
        elif code == 13:      # 문단 끝
            out.append('\n'); i += 2
        elif code < 32:
            i += 2
        else:
            out.append(chr(code)); i += 2
    return ''.join(out)


def extract(path):
    ole = olefile.OleFileIO(path)
    comp = is_compressed(ole)
    chunks = []
    for sec in get_sections(ole):
        raw = ole.openstream(sec).read()
        data = zlib.decompress(raw, -15) if comp else raw
        for tag, payload in parse_records(data):
            if tag == HWPTAG_PARA_TEXT:
                chunks.append(decode_para_text(payload))
    ole.close()
    return '\n'.join(chunks)


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    text = extract(sys.argv[1])
    if len(sys.argv) > 2:
        # 짝 없는 서로게이트 제거 후 저장
        text = ''.join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))
        open(sys.argv[2], 'w', encoding='utf-8').write(text)
        print(f'wrote {len(text)} chars -> {sys.argv[2]}')
    else:
        print(text)
