# 층 B 조립기 연결 — `orchestrate.py` 패치 2곳 (ai-25 소유 파일이라 내가 안 쳤다)

이걸 넣어야 `llm_validate` 의 층 B(해야할일 설명 환각 대조 4종)가 **발효**한다.
안 넣으면 최종 E2E 수치에 층 B 가 나타나지 않는다 (무발효라 강등코드가 0건).

## (A) `b5_문장` 바로 아래에 함수 하나 추가 (현재 275행 근처)

```python
def b5_값(cur, org_id) -> dict | None:
    """B5 의 **값**. `b5_문장` 의 문자열판이 아니라 층 B 대조용 원본이다.

    🔴 반환값 셋을 갈라야 한다 — 뭉치면 게스트 가드가 조용히 꺼진다.
        None   조회 자체를 못 했다(예외) = 모른다   -> 층 B 상태 규칙 무발효
        {}     F축이 없다(게스트·미등록·전 NULL)    -> 최대 강도
        {...}  실제 값
    """
    if not org_id:
        return {}                       # 게스트. '모른다' 가 아니라 '없다' 다
    try:
        r = cur.execute("""SELECT 협약총액, 정부지원_현금, 자기부담_현금
                             FROM tenant.f_profile WHERE org_id=%s LIMIT 1""",
                        (org_id,)).fetchone()
    except Exception:
        return None                     # 모른다
    if not r:
        return {}
    return {k: v for k, v in zip(("협약총액", "정부지원_현금", "자기부담_현금"), r)
            if v is not None}
```

## (B) 검증 호출에 인자 2개 (현재 601행)

```diff
         응답, 사유 = 검증(출력, s맵,
                       룰들=(룰 or {}).get("룰들"),
                       체크코드=코드들,
                       현재기관=기관ID, 사업명=사업명,
                       dangling=검색결과["dangling"],
-                      l3게이팅=게이팅, 룰=룰, dsn=DSN)
+                      l3게이팅=게이팅, 룰=룰,
+                      f사실=b5_값(cur, org_id), 프롬프트=프롬프트,
+                      dsn=DSN)
```

`프롬프트` 는 539행에서 이미 그 이름으로 살아 있다. 추가 조회·추가 LLM 호출 0.

## 확인

- 선행조건 DDL 은 끝났다 — ai-40 이 `decisions_강등코드_check` 를 22종으로 확장했다
- 붙였는지 보는 법: 게스트(org_id 없음) 판정 1건을 돌리고
  `decisions.강등코드` 에 `TASK_*` 가 잡히는지, `해야할일[].설명` 이 떨어졌는지 본다
- 판정 4-way·인용·전제에는 영향이 없다. 층 B 는 `해야할일[].설명` 만 건드린다
  (항목·code 는 남긴다 → 화면이 `check_items` 정적 설명으로 폴백)
