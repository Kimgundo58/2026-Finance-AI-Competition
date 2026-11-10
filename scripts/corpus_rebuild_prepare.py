# -*- coding: utf-8 -*-
"""권위 원문에서 새 검색 자료를 준비한다. DB에는 접근하거나 쓰지 않는다.

기존 청크의 범위·대상 태그는 같은 권위 원문 안에서 정확히 식별된 구간에만
붙인다. 설명되지 않거나 여러 정책이 걸리는 구간은 기본값으로 추정하지 않는다.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from importlib import metadata
from pathlib import Path

import corpus_rebuild_export as export
import corpus_rebuild_manifest as 표범위


_정책칸 = ('layer', '기관id', 'version', '사업명', '적용대상', 'retrieval_scope', 'parse_quality', '페이지')


def 모델_파일해시():
    from huggingface_hub import snapshot_download
    캐시 = Path(snapshot_download('nlpai-lab/KURE-v1', local_files_only=True))
    가중치 = sorted([*캐시.glob('*.safetensors'), *캐시.glob('*.bin')])
    설정파일 = sorted(p for p in 캐시.rglob('*') if p.is_file() and
                  (p.suffix == '.json' or p.name in ('tokenizer.model', 'vocab.txt', 'merges.txt')))
    if not 가중치 or not 설정파일:
        raise ValueError('오프라인 모델 파일 확인 실패')
    파일해시 = {}
    for 파일 in [*가중치, *설정파일]:
        digest = hashlib.sha256()
        with 파일.open('rb') as f:
            while block := f.read(1024 * 1024):
                digest.update(block)
        파일해시[파일.relative_to(캐시).as_posix()] = digest.hexdigest()
    return 파일해시


def _증명_구간(본문: str, 글: str) -> list[tuple[int, int]]:
    if not isinstance(글, str) or not 글:
        raise ValueError('빈 청크의 원문 위치')
    시작 = 본문.find(글)
    if 시작 >= 0:
        if 본문.find(글, 시작 + 1) >= 0:
            raise ValueError('청크의 원문 위치가 없거나 중복됨')
        return [(시작, 시작 + len(글))]
    # 기존 발췌 검사와 같은 정규화만 허용하고 원문 문자 좌표로 되돌린다.
    from ontology_context import _정규화_원문위치
    from ontology_norm import 발췌_정규화
    정규화, 위치 = _정규화_원문위치(본문)
    대상 = 발췌_정규화(글)
    if not 대상:
        raise ValueError('빈 청크의 원문 위치')
    시작 = 정규화.find(대상)
    if 시작 >= 0:
        if 정규화.find(대상, 시작 + 1) >= 0:
            raise ValueError('청크의 원문 위치가 중복됨')
        대응 = list(zip(대상, 위치[시작:시작 + len(대상)]))
    else:
        # 공백 외 문자의 순서와 값이 전부 같고 위치가 유일할 때만 좌표를 찾는다.
        비교 = [(c, 좌표) for c, 좌표 in zip(정규화, 위치) if not c.isspace()]
        비교문 = ''.join(c for c, _ in 비교)
        대상문 = ''.join(c for c in 대상 if not c.isspace())
        시작 = 비교문.find(대상문) if 대상문 else -1
        if 시작 < 0 or 비교문.find(대상문, 시작 + 1) >= 0:
            raise ValueError('청크의 원문 위치가 없거나 중복됨')
        대응 = 비교[시작:시작 + len(대상문)]
    a, b = 대응[0][1][0], 대응[-1][1][1]
    if ''.join(발췌_정규화(본문[a:b]).split()) != ''.join(대상.split()):
        raise ValueError('청크의 원문 좌표 복원 실패')
    # 정규화에서 제거된 페이지 번호 등은 원래 청크에 없던 문자다. 매칭 범위
    # 사이에 있어도 정책을 부여하지 않는다. 공백 역시 확인 대상에서 뺀다.
    ranges = []
    for 문자, (left, right) in 대응:
        if 문자.isspace():
            continue
        if ranges and left <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], right))
        else:
            ranges.append((left, right))
    if not ranges:
        raise ValueError('청크의 원문 좌표 복원 실패')
    return ranges


def _고유_구간(본문: str, 글: str) -> tuple[int, int]:
    구간 = _증명_구간(본문, 글)
    return 구간[0][0], 구간[-1][1]


def 정책_구간(본문: str, 기존청크: list[dict]) -> list[tuple[int, int, dict]]:
    """같은 권위 원문에 있는 원래 청크의 유일한 원문 좌표와 태그."""
    구간 = []
    for chunk in 기존청크:
        좌표들 = _증명_구간(본문, chunk.get('text'))
        정책 = {k: chunk.get(k) for k in _정책칸}
        if (정책['layer'] not in ('L1', 'L2') or 정책['적용대상'] not in ('공통', '창업기업', '주관기관')
                or 정책['retrieval_scope'] not in ('검색대상', '참조확장전용') or 정책['parse_quality'] != 'high'):
            raise ValueError('청크 정책 누락 또는 지원 범위 밖')
        구간.extend((시작, 끝, 정책) for 시작, 끝 in 좌표들)
    구간.sort(key=lambda x: (x[0], x[1]))
    for i, (시작, 끝, 정책) in enumerate(구간):
        for 다른시작, 다른끝, 다른정책 in 구간[i + 1:]:
            if 다른시작 >= 끝:
                break
            if 다른끝 > 시작 and 정책 != 다른정책:
                raise ValueError('서로 다른 청크 정책의 원문 위치가 겹침')
    return 구간


def 새_청크_정책(본문: str, 새글: str, 구간: list[tuple[int, int, dict]]) -> dict:
    """새 청크의 모든 비공백 글자가 같은 정책으로 증명돼야 한다."""
    후보 = []
    for 위치 in (i for 시작, 끝 in _증명_구간(본문, 새글) for i in range(시작, 끝)):
        if 본문[위치].isspace():
            continue
        정책들 = [정책 for a, b, 정책 in 구간 if a <= 위치 < b]
        if not 정책들 or any(x != 정책들[0] for x in 정책들):
            raise ValueError('새 청크에 정책을 확인하지 못한 원문 구간이 있음')
        if not 후보:
            후보.append(정책들[0])
        elif 후보[0] != 정책들[0]:
            raise ValueError('새 청크에 서로 다른 검색 정책이 섞임')
    if not 후보:
        raise ValueError('청크 정책 없음')
    return 후보[0]


def 기존_정책_보존(본문: str, 기존: list[tuple[int, int, dict]], 새청크: list[tuple[str, dict]]) -> None:
    """기존 청크의 모든 비공백 글자도 같은 정책으로 새 청크에 남아야 한다."""
    새구간 = [(a, b, 정책) for 글, 정책 in 새청크 for a, b in _증명_구간(본문, 글)]
    for 시작, 끝, 정책 in 기존:
        for 위치 in range(시작, 끝):
            if 본문[위치].isspace():
                continue
            if not any(a <= 위치 < b and 태그 == 정책 for a, b, 태그 in 새구간):
                raise ValueError('기존 검색 정책의 원문 구간이 새 청크에서 누락됨')


def 원천_검증(value: dict) -> dict:
    data = value.get('내용') if isinstance(value, dict) else None
    if not isinstance(data, dict) or value.get('sha256') != export.해시(data):
        raise ValueError('원천 내보내기 해시 오류')
    if data.get('형식') != export.FORMAT or data.get('문서키') != export.대상_문서():
        raise ValueError('원천 문서 범위 또는 형식 변경')
    docs = data.get('문서')
    if not isinstance(docs, list) or [d.get('doc_id') for d in docs] != data['문서키']:
        raise ValueError('원천 문서 목록 오류')
    for doc in docs:
        if doc.get('status') != 'active' or not doc.get('조문'):
            raise ValueError('기준 문서 비활성 또는 조문 누락')
        seen = set()
        for a in doc['조문']:
            key, body = a.get('조번호'), a.get('본문')
            if (not isinstance(key, str) or key in seen or a.get('doc_id') != doc['doc_id']
                    or not isinstance(body, str) or a.get('원문해시') != hashlib.sha256(body.encode('utf-8')).hexdigest()):
                raise ValueError('원천 조문 논리키 또는 해시 오류')
            seen.add(key)
    return data


def 정책_진단(value: dict, 매니페스트=None) -> dict:
    """모델 실행 전 기존 청크의 원문 위치·정책을 집계한다. 원문은 출력하지 않는다."""
    원천 = 원천_검증(value)
    표들 = 표범위.검증(value, 매니페스트) if 매니페스트 is not None else {}
    result = {'문서수': len(원천['문서']), '조문수': 0, '기존청크수': 0,
              '청크없는조문': 0, '위치불명청크': 0,
              '정책확인실패조문': 0, '정책혼합조문': 0, '표정책별도조문': 0}
    for doc in 원천['문서']:
        for a in doc['조문']:
            result['조문수'] += 1
            chunks = a.get('기존청크') or []
            result['기존청크수'] += len(chunks)
            if (a['doc_id'], a['조번호']) in 표들:
                result['표정책별도조문'] += 1
                continue
            if not chunks:
                result['청크없는조문'] += 1
                continue
            body = a['본문']
            for c in chunks:
                try:
                    _고유_구간(body, c.get('text'))
                except ValueError:
                    result['위치불명청크'] += 1
            정책들 = {export.해시({k: c.get(k) for k in _정책칸}) for c in chunks}
            if len(정책들) > 1:
                result['정책혼합조문'] += 1
            try:
                정책_구간(body, chunks)
            except ValueError:
                result['정책확인실패조문'] += 1
    return result


def 준비(value: dict, 매니페스트: dict) -> dict:
    """기존 청킹·임베딩·BM25 함수로 원천에 일치하는 검색 자료를 만든다."""
    # 외부 모델 다운로드나 네트워크 폴백 없이 로컬에 있는 CPU 모델만 사용한다.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    import bm25_index
    import corpus_ingest

    원천 = 원천_검증(value)
    표들 = 표범위.검증(value, 매니페스트)
    out = []
    for doc in 원천['문서']:
        조문들 = doc['조문']
        장들 = bm25_index._장_매핑(조문들)
        작업조문 = [{**a, '적용대상': ('공통' if a.get('기존청크') else None)
                   if (a['doc_id'], a['조번호']) not in 표들 else None} for a in 조문들]
        청크들, _ = corpus_ingest._청크_만들기(작업조문, layer=doc['layer'], 사업=None, doc_id=doc['doc_id'])
        건수 = Counter(c['조'] for c in 청크들)
        for i, a in enumerate(조문들):
            if a.get('기존청크') and not 건수[i] and (a['doc_id'], a['조번호']) not in 표들:
                raise ValueError('기존 청크가 있는 조문이 새 청킹에서 제외됨')
        구간표 = {i: 정책_구간(a['본문'], a.get('기존청크') or []) for i, a in enumerate(조문들)
               if (a['doc_id'], a['조번호']) not in 표들}
        새청크표 = {i: [] for i in range(len(조문들))}
        for c in 청크들:
            article = 조문들[c['조']]
            정책 = 새_청크_정책(article['본문'], c['text'], 구간표[c['조']])
            새청크표[c['조']].append((c['text'], 정책))
            header = bm25_index._헤더(정책['layer'], 정책['사업명'], doc['doc_id'],
                                  장들.get(article['조번호'], ''), article['조번호'], article['조제목'])
            out.append({'doc_id': doc['doc_id'], '조번호': article['조번호'], '항호': c['항호'],
                        '조제목': article['조제목'], 'text': c['text'], **정책,
                        '입력': f"{header}\n{c['text']}"})
        for i, article in enumerate(조문들):
            key = (article['doc_id'], article['조번호'])
            if key not in 표들:
                기존_정책_보존(article['본문'], 구간표[i], 새청크표[i])
                continue
            기존 = article.get('기존청크') or []
            if not 기존:
                raise ValueError('표의 출처 메타데이터 없음')
            고정키 = ('layer', '기관id', 'version', '사업명', 'parse_quality')
            고정 = {k: 기존[0].get(k) for k in 고정키}
            if any(any(c.get(k) != 고정[k] for k in 고정키) for c in 기존):
                raise ValueError('표의 문서 메타데이터 충돌')
            header = bm25_index._헤더(고정['layer'], 고정['사업명'], doc['doc_id'],
                                  장들.get(article['조번호'], ''), article['조번호'], article['조제목'])
            for span in 표들[key]['구간']:
                pieces = 표범위.원문_분할(article['본문'], span['시작'], span['끝'],
                                    lambda s: corpus_ingest._토큰수([s])[0], corpus_ingest.청크_최대토큰)
                for left, right, text in pieces:
                    out.append({'doc_id': doc['doc_id'], '조번호': article['조번호'], '항호': None,
                                '조제목': article['조제목'], 'text': text, **고정, '페이지': None,
                                '적용대상': span['적용대상'], 'retrieval_scope': span['검색범위'],
                                '표시작': left, '표끝': right, '입력': f'{header}\n{text}'})
    if not out:
        raise ValueError('검색 자료가 비어 있음')
    # 임베딩과 BM25는 완전히 동일한 헤더+본문을 쓴다.
    for i in range(0, len(out), 16):
        batch = out[i:i + 16]
        inputs = [x['입력'] for x in batch]
        import retrieve
        if any(n > retrieve.임베딩_모델().max_seq_length for n in corpus_ingest._토큰수(inputs)):
            raise ValueError('헤더를 포함한 임베딩 입력 토큰 한도 초과')
        vectors = corpus_ingest._임베딩(inputs)
        terms = bm25_index.토큰화(inputs)
        if len(vectors) != len(batch) or len(terms) != len(batch):
            raise ValueError('검색 자료 생성 수 불일치')
        for row, vector, tokens in zip(batch, vectors, terms):
            row['embedding'] = vector
            row['길이'] = len(tokens)
            row['용어'] = dict(Counter(tokens))
            del row['입력']
    import retrieve
    모델 = retrieve.임베딩_모델()
    토크나이저 = 모델.tokenizer
    if str(모델.device) != 'cpu' or 토크나이저.name_or_path != 'nlpai-lab/KURE-v1':
        raise ValueError('승인된 CPU 임베딩 모델 확인 실패')
    코드 = {}
    for module in (corpus_ingest, bm25_index, retrieve, 표범위, export):
        코드[Path(module.__file__).name] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    코드[Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    메타 = {'모델': 'nlpai-lab/KURE-v1', '장치': 'cpu',
          '모델리비전': 토크나이저.init_kwargs.get('_commit_hash'), '모델파일해시': 모델_파일해시(),
          '어휘해시': export.해시(토크나이저.get_vocab()), '코드해시': 코드,
          '패키지': {name: metadata.version(name) for name in ('sentence-transformers', 'transformers', 'kiwipiepy')}}
    결과 = {'형식': 1, '원천해시': value['sha256'], '표매니페스트해시': export.해시(매니페스트),
          '문서키': 원천['문서키'], '청크': out, '생성정보': 메타}
    return {'내용': 결과, 'sha256': export.해시(결과)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, type=Path)
    ap.add_argument('--output', type=Path)
    ap.add_argument('--manifest', type=Path)
    ap.add_argument('--policy-only', action='store_true')
    args = ap.parse_args()
    원천 = json.loads(args.source.read_text(encoding='utf-8'))
    if args.policy_only:
        매니페스트 = json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest else None
        print(json.dumps(정책_진단(원천, 매니페스트), ensure_ascii=False))
        return
    if args.output is None or args.manifest is None:
        ap.error('검색 자료 준비에는 --output과 --manifest가 필요합니다')
    매니페스트 = json.loads(args.manifest.read_text(encoding='utf-8'))
    결과 = 준비(원천, 매니페스트)
    export.새_파일_쓰기(args.output, 결과)
    print(json.dumps({'청크수': len(결과['내용']['청크']), 'sha256': 결과['sha256']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
