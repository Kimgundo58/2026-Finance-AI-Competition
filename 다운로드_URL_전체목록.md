# 「써도돼요」 데이터셋 — 다운로드 URL 전체 목록

작성: 2026-08-10 (Cowork 세션). 용도: 새 채팅에서 미수집 문서 다운로드 작업 지시용.

- **[완료]** = 이미 다운로드됨 (`써도돼요_데이터셋_1차.zip`에 포함). 재다운로드 불필요, 출처 기록용.
- **[미수집]** = URL은 검증됐지만 파일은 아직 안 받음. **이것들만 다운로드하면 됨.**
- 미수집 항목의 "내용검증" = 문서 내용까지 확인됨 / "링크확인" = 파일 존재·제목만 확인됨.
- 다운로드 팁: 브라우저로 바로 받거나 `curl -L -A "Mozilla/5.0" -o <파일명> "<URL>"`. 받은 뒤 PDF는 파일 시작이 `%PDF`인지, HWP는 OLE(d0cf11e0) 또는 PK인지 확인할 것 (HTML 오류 페이지가 저장되는 경우 있음).

---

# ① 수집 완료 (25개 파일) — 출처 URL 기록

## L1 — 법령 (12건, 국가법령정보 DRF API, 조문 단위 XML)

| 파일명 | 법령명 | 시행일 | 출처 URL |
|---|---|---|---|
| L1_중소기업창업지원법_20260701.xml | 중소기업창업 지원법 | 2026-07-01 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=281995&type=XML |
| L1_중소기업창업지원법_시행령_20260324.xml | 동법 시행령 | 2026-03-24 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=284871&type=XML |
| L1_중소기업창업지원법_시행규칙_20260101.xml | 동법 시행규칙 | 2026-01-01 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=282421&type=XML |
| L1_보조금관리에관한법률_20260602.xml | 보조금 관리에 관한 법률 | 2026-06-02 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=286449&type=XML |
| L1_보조금관리에관한법률_시행령_20260102.xml | 동법 시행령 | 2026-01-02 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=281407&type=XML |
| L1_산업교육진흥및산학연협력촉진에관한법률_20250621.xml | 산학협력법 | 2025-06-21 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=267351&type=XML |
| L1_산업교육진흥및산학연협력촉진에관한법률_시행령_20260324.xml | 동법 시행령 | 2026-03-24 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=284767&type=XML |
| L1_국가연구개발혁신법_20260911.xml | 국가연구개발혁신법 | 2026-09-11 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=283849&type=XML |
| L1_국가연구개발혁신법_시행령_20260728.xml | 동법 시행령 | 2026-07-28 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=288335&type=XML |
| L1_국가연구개발혁신법_시행규칙_20260611.xml | 동법 시행규칙 | 2026-06-11 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=286879&type=XML |
| L1_중소기업기술혁신촉진법_20260701.xml | 중소기업 기술혁신 촉진법 | 2026-07-01 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=281987&type=XML |
| L1_산업기술혁신사업공통운영요령_20241230.xml | 산업기술혁신사업 공통 운영요령 (행정규칙) | 2024-12-30 | https://www.law.go.kr/DRF/lawService.do?OC=test&target=admrul&ID=2100000251982&type=XML |

- 참고: 산학협력법 시행규칙(MST=285257)은 미수집 — 필요 시 같은 패턴으로 추가.

## 통계·시장규모 (8건, data.go.kr)

| 파일명 | 데이터셋 | 기준시점 | 출처 URL |
|---|---|---|---|
| 통계_한국창업보육협회_창업보육센터_전국_현황_2026.csv | 창업보육센터 전국 현황 (247개) | 2026-07-31 | https://www.data.go.kr/data/15039249/fileData.do |
| 통계_중소벤처기업부_창업보육센터_센터현황_2025.csv | 센터현황 (257개, 입주기업·입주율 포함) | 2025-03-21 | https://www.data.go.kr/data/15122846/fileData.do |
| 통계_창업진흥원_예비창업패키지_주관기관_현황_2023.csv | 예창패 주관기관 (27개) | 2023-07-17 | https://www.data.go.kr/data/15088129/fileData.do |
| 통계_창업진흥원_초기창업패키지_주관기관_현황_2023.csv | 초창패 주관기관 (20개) | 2023-07-17 | https://www.data.go.kr/data/15088133/fileData.do |
| 통계_창업진흥원_창업도약패키지_주관기관_정보_2023.csv | 도약패 주관기관 (13개) | 2023-07-17 | https://www.data.go.kr/data/15037531/fileData.do |
| 통계_창업진흥원_창업중심대학_주관기관_현황_2023.csv | 창업중심대학 (9개) | 2023-07-17 | https://www.data.go.kr/data/15103177/fileData.do |
| 통계_창업진흥원_창조경제혁신센터_데이터_현황_2026.csv | 창조경제혁신센터 (19개) | 2026-06-04 | https://www.data.go.kr/data/15047223/fileData.do |
| 통계_창업진흥원_K-Startup_조회서비스_OpenAPI명세_2026.json | K-Startup 조회서비스 API 명세 (Swagger) | 2026 | https://www.data.go.kr/data/15125364/openapi.do |

- CSV 인코딩은 전부 EUC-KR(CP949) 원본 그대로임.

## L4 — 기관 규정 텍스트본 (5건)

| 파일명 | 문서 | 비고 |
|---|---|---|
| AC_중소벤처기업부_창업기획자등록및관리규정_2021.txt | 창업기획자(액셀러레이터) 등록 및 관리 규정 | 수집 에이전트가 중도 종료되어 출처 URL 미기록. 국가법령정보센터(law.go.kr) 행정규칙·자치법규에서 문서명 검색으로 원본 재확인 가능 |
| BI_중소벤처기업부_창업보육센터운영요령_2025.txt | 창업보육센터 운영요령 (고시) | 〃 |
| BI_영월군_창업보육센터설치및운영조례_2019.txt | 영월군 창업보육센터 설치·운영 조례 | 〃 |
| 혁신센터_경기도_경기창조경제혁신센터지원조례_2020.txt | 경기창조경제혁신센터 지원 조례 | 〃 |
| TP_산업통상자원부_산업기술혁신사업사업비산정관리및사용정산에관한요령_2017.txt | 산업기술혁신사업 사업비 산정·관리·사용·정산 요령 | 〃 |

---

# ② 미수집 — 다운로드 대상 (URL 검증 완료)

## L2 — 부처 지침

| 문서 | 버전 | URL | 상태 |
|---|---|---|---|
| 중소기업창업 지원사업 통합관리지침 | 제14차 (2025.12.23) | https://grant-documents.thevc.kr/285873_(%EB%B3%84%EC%B2%A85)+%EC%A4%91%EC%86%8C%EA%B8%B0%EC%97%85%EC%B0%BD%EC%97%85+%EC%A7%80%EC%9B%90%EC%82%AC%EC%97%85+%ED%86%B5%ED%95%A9%EA%B4%80%EB%A6%AC%EC%A7%80%EC%B9%A8+%EC%A0%9C14%EC%B0%A8.pdf | 내용검증 |
| 창업사업화 지원사업 통합관리지침 (구버전, diff 테스트용) | 제4차 (2019.02.01) | https://t1.daumcdn.net/brunch/service/user/vhc/file/WqmotH73l1_yavyEgCtSdsQYpFo.pdf?download= | 내용검증 |
| 창업사업화 지원사업 통합관리지침 | 제12차 | https://www.vcs.go.kr/web/portal/bbs/issue/11976 (게시글, 첨부 수동 다운로드) | 링크확인 |
| 창업사업화 지원사업 통합관리지침 | 제13차 | https://www.k-startup.go.kr/afile/fileDownload/sZ9mn | 링크확인 |
| 통합관리지침 (부정사용 제재조항 포함본) | — | https://www.mss.go.kr/common/board/Download.do?bcIdx=1034315&cbIdx=126&streFileNm=ef2d779b-1806-475d-b865-473759b20430.pdf | 링크확인 |

## L3 — 공고문·별표·세부관리기준

| 문서 | 버전 | URL | 상태 |
|---|---|---|---|
| 예비창업패키지 세부 관리기준 | — | https://www.mss.go.kr/common/board/Download.do?bcIdx=1020539&cbIdx=246&streFileNm=5c0c6788-0310-48da-bddc-ff10630d7ee2.pdf | 링크확인 |
| 예비창업패키지 세부 관리기준 (창진원본) | — | https://www.kised.or.kr/attachedFileDownload.es?seq=171 | 링크확인(바이너리 응답) |
| 초기창업패키지 세부 관리기준 | — | https://www.kised.or.kr/attachedFileDownload.es?seq=642 | 링크확인(바이너리 응답) |
| 【별표】집행기준 및 증빙서류 | 2024경 | https://startup.cku.ac.kr/bbs/startup/1142/75294/download.do | 링크확인(바이너리 응답) |
| 2025 예비창업패키지 모집공고 | 공고 제2025-105호 | https://grant-documents.thevc.kr/250398_%5B%EA%B3%B5%EA%B3%A0%EB%AC%B8%5D+2025%EB%85%84%EB%8F%84+%EC%98%88%EB%B9%84%EC%B0%BD%EC%97%85%ED%8C%A8%ED%82%A4%EC%A7%80+%EC%98%88%EB%B9%84%EC%B0%BD%EC%97%85%EC%9E%90+%EB%AA%A8%EC%A7%91%EA%B3%B5%EA%B3%A0.pdf | 내용검증 |
| 2025 초기창업패키지 모집공고 (민간투자매칭형) | 2025 | https://grant-documents.thevc.kr/258637_%EB%AA%A8%EC%A7%91+%EA%B3%B5%EA%B3%A0%EB%AC%B8.pdf | 내용검증 |
| 2026 초기창업패키지 모집공고 (딥테크특화형) | 공고 제2026-671호 | https://grant-documents.thevc.kr/286401_(%EA%B3%B5%EA%B3%A0%EB%AC%B8)+2026%EB%85%84+%EC%B4%88%EA%B8%B0%EC%B0%BD%EC%97%85%ED%8C%A8%ED%82%A4%EC%A7%80(%EB%94%A5%ED%85%8C%ED%81%AC+%ED%8A%B9%ED%99%94%ED%98%95)+%EC%B0%BD%EC%97%85%EA%B8%B0%EC%97%85+%EB%AA%A8%EC%A7%91+%EA%B3%B5%EA%B3%A0%EB%AC%B8.pdf | 링크확인 |
| 2025 초기창업패키지 주요 질의응답집 [별첨4] | 2025.2 | https://grant-documents.thevc.kr/250405_%5B%EB%B3%84%EC%B2%A8+4%5D+2025%EB%85%84%EB%8F%84+%EC%B4%88%EA%B8%B0%EC%B0%BD%EC%97%85%ED%8C%A8%ED%82%A4%EC%A7%80+%EC%B0%BD%EC%97%85%EA%B8%B0%EC%97%85+%EB%AA%A8%EC%A7%91%EA%B3%B5%EA%B3%A0+%EA%B4%80%EB%A0%A8+%EC%A3%BC%EC%9A%94+%EC%A7%88%EC%9D%98%EC%9D%91%EB%8B%B5.pdf | 링크확인 |
| 2026 창업지원사업 통합공고문 | 공고 제2025-648호 | https://www.k-startup.go.kr/afile/fileDownload/8niLn | 링크확인 |
| 2025 창업도약패키지(일반형) 모집공고 | 공고 제2025-119호 | https://bioagora.khidi.or.kr/fileDownload?titleId=4573&fileId=1&fileDownType=C&paramMenuId=MENU00468 | 링크확인 |

## L4 — 대학 (21개교; ★=내용검증)

| 대학 | 문서 | URL |
|---|---|---|
| 협성대 ★ | 산학협력단 사업비 관리 지침 | https://iacf.uhs.ac.kr/file/data/file_2_1_20200207.pdf |
| 협성대 ★ | 대학혁신지원사업 관리·집행 지침 (2025.10) | https://www.uhs.ac.kr/bbs/ace/407/dTRtMVpBU0lMajNlSTJQLzFiRlZaZz09/download.do |
| 목포해양대 ★ | 산학협력단 회계처리규칙 해설 (2021) | https://iacf.mmu.ac.kr/upload/board/164/eoez/oB/oB/main/ae1a402e615544d5b4763612cd9610e3.pdf |
| 부산대 ★ | 대학혁신지원사업 사업비 집행·관리 기준 | https://pnui.pusan.ac.kr/bbs/pnui/17294/798311/download.do |
| 성신여대 | 연구비 비목별 계상·집행기준 지침 | https://www.sungshin.ac.kr/bbs/acm/3860/73075/download.do |
| 성결대 ★ | 대학혁신지원사업비 집행 가이드라인 | https://www.sungkyul.ac.kr/bbs/skuc/1398/19650/download.do |
| 한신대 ★ | 대학혁신지원사업 사업비 집행·관리 지침 (2025.3) | https://www.hs.ac.kr/bbs/kor/24/63392/download.do |
| 대구한의대 | 대학혁신지원사업 사업비 집행 지침 (2025.5) | https://www.duh.ac.kr/lib/filedownload.php?code=_79&id=35306&f_num=1&downname=FN20250502102507_1.pdf |
| 한국항공대 ★ | 연구비 중앙관리 지침 (2019, 16차 개정) | http://research.kau.ac.kr/upfile/1.%20%EC%97%B0%EA%B5%AC%EB%B9%84%EC%A4%91%EC%95%99%EA%B4%80%EB%A6%AC%EC%A7%80%EC%B9%A8.pdf |
| 한양여대 ★ | 외부연구비 관리규정 (2014) | https://www.hywoman.ac.kr/resources/RU/rule_5_0_3.pdf |
| 중앙대 | 외부연구비 관리 규정 (6-9) | https://iacf.cau.ac.kr/bbs/file/downloadRule/10667 (규정집: https://iacf.cau.ac.kr/service/rule) |
| 고려대 ★ | 산학협력단 교외연구비 관리지침 (2024.5) | https://socbk21.korea.ac.kr/socbk21/community/data.do?articleNo=508792&attachNo=263343&mode=download |
| 경희대 ★ | 산학협력단 외부연구비 관리 지침 (2020.9) | https://com.khu.ac.kr/research/cmmn/file/fileDown.do?menuNo=6400049&atchFileId=2d9a2867199b410fb56347e1135e4139&fileSn=1&bbsId=BMSR00040 |
| 강원대 | 연구비 관리 지침 (2025.9, HWP) | https://uicf.kangwon.ac.kr/Uploads/RuleBook/09050157K5QE.hwp (규정집 목록: https://uicf.kangwon.ac.kr/RuleBook/list) |
| 서울시립대 | 산학협력단 회계처리규칙 (2016, HWP) | https://research.uos.ac.kr/sites/default/files/2023-03/%EC%82%B0%ED%95%99%ED%98%91%EB%A0%A5%EB%8B%A8%ED%9A%8C%EA%B3%84%EC%B2%98%EB%A6%AC%EA%B7%9C%EC%B9%99_2016.1.29.hwp |
| 서울대 | 연구비 관리 규정 (2020.3, HWP) | https://snurnd.snu.ac.kr/sites/default/files/statute/%EC%84%9C%EC%9A%B8%EB%8C%80%ED%95%99%EA%B5%90%20%EC%97%B0%EA%B5%AC%EB%B9%84%20%EA%B4%80%EB%A6%AC%20%EA%B7%9C%EC%A0%95(2020.%203.%2025.).hwp |
| 위덕대 | 산학협력단 회계처리규칙 / 2026 예산관리규칙 운영지침 | https://sandan.uu.ac.kr/board/down.asp?idx=723&m=6&s=4&g=2 및 idx=5175 |
| 동아대 | 창업기업 사업비 집행 매뉴얼 (창업지원단) | https://dms.donga.ac.kr/bbs/changup/1548/92323/download.do |
| 전남대 | 산학협력단 연구관리업무 간편 매뉴얼 | https://sanhak.jnu.ac.kr/sites/sanhak/files/video_download05.pdf |
| 신라대 | 대학혁신지원사업 사업비 집행 지침 | https://cess.silla.ac.kr/prime/index.php?pCode=578&pg=1&mode=fdn&idx=836&num=2 |
| 동서대 | 대학혁신지원사업 유형 운영 매뉴얼 Ⅰ·Ⅱ | https://uisp.dsu.ac.kr/uisp/index.php?pCode=usip_board&pg=1&mode=fdn&idx=165&num=1 |
| 인하대 | 대학혁신지원사업 집행·관리기준 (게시글) | http://inhainnovation.or.kr/bbs/board.php?bo_table=data&wr_id=3 |

## 사례집·판례·FAQ (판정 룰 부트스트랩용)

| 문서 | 발행 | URL | 상태 |
|---|---|---|---|
| 정부연구비 사용 Q&A 사례집 (2025 개정, 80+ Q&A) | 한국연구재단 | https://research.uos.ac.kr/sites/default/files/2025-06/%EB%B6%99%EC%9E%841_%EC%A0%95%EB%B6%80%EC%97%B0%EA%B5%AC%EB%B9%84%20%EC%82%AC%EC%9A%A9%20Q%26A%20%EC%82%AC%EB%A1%80%EC%A7%91.pdf | 내용검증 |
| 연구비 사용 상담·부당집행 사례집 (2013) | 미래창조과학부 | https://iacf.mokpo.ac.kr/sites/iacftmp/iacf/files/%EC%97%B0%EA%B5%AC%EB%B9%84%EC%82%AC%EC%9A%A9%EC%83%81%EB%8B%B4%EB%B0%8F%EB%B6%80%EC%A0%95%EC%A7%91%ED%96%89%EC%82%AC%EB%A1%80_.pdf | 내용검증 |
| 국가연구개발사업 제재처분 판례 조사분석 (2022) | KISTEP | https://www.kistep.re.kr/reportDownload.es?rpt_no=PRG0720220006&seq=prg_0071P@5 | 내용검증 |
| 제재처분 가이드라인 (2025) | IITP | https://www.iitp.kr/file/download.it?seq=9890 | 링크확인 |
| 행정심판 재결례 (보조금 환수) | 중앙행심위 | https://www.law.go.kr/LSW/deccInfoP.do?deccSeq=243657&mode=3 · 대체: https://www.data.go.kr/data/15039823/fileData.do (권익위 재결례집) | 링크확인 |
| 국고보조금 통합관리지침 (기재부공고 2021-210) | 기획재정부 | https://www.dcdcenter.or.kr/sites/default/files/2023-09/3.%20%EA%B5%AD%EA%B3%A0%EB%B3%B4%EC%A1%B0%EA%B8%88%20%ED%86%B5%ED%95%A9%EA%B4%80%EB%A6%AC%EC%A7%80%EC%B9%A8(%EA%B8%B0%ED%9A%8D%EC%9E%AC%EC%A0%95%EB%B6%80%EA%B3%B5%EA%B3%A0)(%EC%A0%9C2021-210%ED%98%B8)(20211216).pdf | 링크확인 |
| e나라도움 보조사업자 매뉴얼 | 기획재정부 | https://www.gosims.go.kr/manual/ver2/2-1.%EB%B3%B4%EC%A1%B0%EC%82%AC%EC%97%85%EC%9E%90%EA%B0%9C%EB%85%90.pdf | 링크확인 |
| 창업사업화 부정행위 사례집 | 창진원 | 공개 PDF 미발견 — 비공개/교육용 배포 추정 (존재 근거: 한국경제 202205315082i). 창업지원단에 직접 요청 권장 | 미발견 |
| 산학협력단 연구비 집행 FAQ (게시판) | 경인교대 | https://edu.ginue.ac.kr/rndb/CMS/Board/Board.do?mCode=MN031 | 링크확인 |
