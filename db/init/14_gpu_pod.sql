-- ════════════════════════════════════════════════════════════════════════════
-- 14_gpu_pod.sql — GPU 팟 주소를 DB 로 옮긴다 (Q2 제안, 2026-09-04)
--
-- ✅ **2026-09-05 배선 완료(`c0fb014`).** `scripts/adapter.py::vllm_url()` ·
--    `server/gpu_watchdog.py::_팟id()` 가 이 표(`ops.gpu_pod` id='default')를 우선 읽고
--    없으면 env(`VLLM_URL`/`RUNPOD_POD_ID`)로 폴백한다(30초 캐시). 아래 §「곁다리」의
--    `last_call_at`(다중 인스턴스 유휴 판단)은 **별개 항목** — 그쪽은 여전히 미배선이다.
--
-- ✅ **2026-09-06 쓰기 경로 추가(레인 ι).** «읽기» 는 배선됐는데 «누가 채우는가» 가
--    사람의 손 SQL UPDATE 뿐이었다(2026-09-05 ai-04 확인: pod_id·vllm_url 둘 다 NULL).
--    `POST /admin/gpu/pod`(`server/main.py`)가 그 자리를 대신한다 — pod_id 하나만 받아
--    vllm_url 은 RunPod 프록시 규칙(`{pod_id}-8000.proxy.runpod.net`)으로 유도해 같이 쓴다.
--    `상태` 컬럼은 여전히 이 엔드포인트가 안 건드린다 — 그건 `/api/gpu/wake` 의 몫이다.
--
-- ── 왜 필요한가 ────────────────────────────────────────────────────────────
-- 지금 `VLLM_URL` 은 Cloud Run 환경변수다. 팟을 새로 열면 주소가 바뀌는데,
-- 환경변수를 고치려면 **재배포**가 필요하다 — 그러면 "GPU 가 혼자 닫힌다"는 되어도
-- "GPU 가 혼자 켜진다" 다음 자동화가 사람 손을 또 요구한다. `scripts/adapter.py` ·
-- `server/gpu_watchdog.py` 둘 다 `VLLM_URL` 을 env 로 읽는 게 그 증거다.
-- → 서버가 **DB 를 읽게** 하면 팟이 바뀌어도 재배포가 없다.
--
-- ── 곁다리로 같이 챙기는 것 — 다중 인스턴스 유휴 판단 ─────────────────────
-- 🔴 2026-09-04 실측(Q2, 코드 읽기): Cloud Run 은 `maxScale 3` 이고, `GPU워치독`
--    의 `_마지막호출`·`_팟상태` 는 **프로세스 메모리**다. 인스턴스가 여럿이면
--    각자 따로 「마지막으로 GPU 를 쓴 시각」을 들고 있다는 뜻이다 —
--    인스턴스 A 로 트래픽이 안 온 지 30분이 지나 A 가 정지를 쏘는 동안,
--    인스턴스 B 는 방금 판정을 실행 중일 수 있다. 유휴 판단이 인스턴스 로컬이라
--    **아직 쓰는 중인 팟을 다른 인스턴스가 끄는 경합**이 구조적으로 가능하다.
--    `last_call_at` 컬럼은 이걸 DB 공유로 닫기 위한 자리다.
--    🔴 **코드 배선(호출기록()/유휴초 가 DB 를 왕복하게 하는 것)은 이 파일의 범위가
--    아니다** — 매 판정마다 DB 왕복이 늘면 지연에 영향을 준다(p50 20.2초 예산 안에서
--    무시 못할 수 있다). 스키마만 미리 마련해 둔다. 배선 여부·시점은 중앙 판단.
--
-- ── 설계 메모 ──────────────────────────────────────────────────────────────
-- `tenant.*` 가 아니다 — 팟은 기관(org)에 속하지 않는 **전역 인프라 상태**다.
-- 그래서 새 스키마 `ops` 를 연다. RLS 를 안 건다 — org 격리 대상이 아니고,
-- 이 테이블에 접근하는 건 서버 프로세스(`suddoe_app`)뿐이다. "동시 팟 1개" 정책과
-- 짝을 맞춰 **싱글턴 행**(`id='default'`)으로 둔다 — 여러 행이 생기면 그 자체가
-- "팟이 여러 개" 라는 뜻이고 그건 정책 위반이다.
-- ════════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE ops.gpu_pod (
    id            TEXT PRIMARY KEY DEFAULT 'default',
    -- 싱글턴 강제 — "동시 팟 1개" 를 스키마로도 막는다 (기본값 외 id 를 안 씀)
    CONSTRAINT gpu_pod_singleton CHECK (id = 'default'),

    pod_id        TEXT,             -- RunPod 팟 id. NULL = 지금 없음/닫힘
    vllm_url      TEXT,             -- 서버가 실제로 읽는 값 — env VLLM_URL 을 대체한다
    상태          TEXT NOT NULL DEFAULT '중지'
                  CHECK (상태 IN ('가동', '중지', '기동중', '알수없음')),
    last_call_at  TIMESTAMPTZ,      -- 마지막 «실제 GPU 호출». 다중 인스턴스 유휴판단 공유용
                                    -- (배선은 별도 작업 — 위 주석 참고)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    TEXT              -- 디버그용: 어느 인스턴스/세션이 마지막으로 썼는지
);

-- 초기 싱글턴 행. server 쪽은 이 행이 없으면 "env 로 폴백" 하게 짤 것(중앙 판단·별도 코드).
INSERT INTO ops.gpu_pod (id) VALUES ('default') ON CONFLICT DO NOTHING;

GRANT USAGE ON SCHEMA ops TO suddoe_app;
GRANT SELECT, UPDATE ON ops.gpu_pod TO suddoe_app;

-- ════════════════════════════════════════════════════════════════════════════
-- 검증 (적용한다면 — 중앙 지시 패턴을 그대로 따른다)
--
--   ① 새 트랜잭션·새 연결로 되읽기: update ops.gpu_pod set vllm_url='http://x' where id='default';
--      -- 새 psql 세션: select vllm_url from ops.gpu_pod where id='default';  → 'http://x'
--   ② 비특권 롤(suddoe_app)로 SELECT·UPDATE 둘 다 되는지 확인 (postgres 는 bypassrls 라
--      권한 실패를 못 잡는다 — `docs/0-3_초록이_가린다.md` ⓒ 가 그 자리다)
--   ③ 두 번째 행 INSERT 시도 → CHECK 위반으로 거부되는지 확인 (싱글턴 검증)
-- ════════════════════════════════════════════════════════════════════════════
