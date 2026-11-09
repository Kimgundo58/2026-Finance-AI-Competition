# -*- coding: utf-8 -*-
"""설정된 사업의 캐시 반환 전에 AGE와 해당 사업 규범을 확인한다."""
from server._common import 온톨로지_사업


def 확인(사업명):
    if not 온톨로지_사업(사업명):
        return
    import ontology_lookup
    from _lib import db
    with db.connect(connect_timeout=5, autocommit=True) as conn:
        if not ontology_lookup.새경로_대상(conn.cursor(), 사업명):
            # 환경값이 요청 중 바뀐 경우도 기존 경로 결과를 반환하지 않는다.
            raise ontology_lookup.온톨로지_미준비('사업 규정의 설정이 변경되었습니다')
