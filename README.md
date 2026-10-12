# grantmatchpy

그랜트매치 공고 첨부의 **내려받기**와 **글자 추출**을 하는 Python 3 / FastAPI 서비스. 기준은 PHP 코드(`jiwonnavi/packages/extract`, `packages/collect/src/Attachment/Downloader.php`)이고 같은 동작으로 옮겼다. **127.0.0.1 에만** 열리며 Java(grantmatch)가 내부 호출한다.

## 구성

```
app/
  main.py              FastAPI (토큰 검사, 3개 엔드포인트)
  __main__.py          python -m app — 127.0.0.1 고정으로 uvicorn 실행
  settings.py          환경 변수 설정(.env 선택)
  extract/
    text_extractor.py  형식 판별(파일 머리)·추출·normalize          ← PHP TextExtractor
    hwp.py             HWP 5.0 직접 해석(olefile + 태그 67)        ← PHP Hwp/Cfb·HwpReader
    xml_text.py        HWPX·DOCX·HWPML 문단 추출(안전한 lxml)       ← PHP XmlText
    pdf_text.py        pdftotext 우선 → pypdf                      ← PHP PdfText
    zip_bundle.py      ZIP 묶음 규칙                               ← PHP ZipBundle
    zip_guard.py       항목 수·풀린 크기·압축률 상한               ← PHP ZipGuard
    sandbox.py·worker.py  하위 프로세스 실행(시간·메모리 제한)     ← PHP Sandbox·Process
    limits.py          메모리 상한(Windows 작업 개체 / 리눅스 RLIMIT_AS)
  fetch/downloader.py  첨부 내려받기                               ← PHP Downloader·CurlClient
  embed/model.py       문장 임베딩(지연 로딩 싱글톤, ONNX int8 e5-small)  ← 새 기능
  embed/passage_text.py  공고 한 건 → 임베딩 입력 글
  embed/store.py       임베딩 작업·벡터 저장소(Protocol + PostgreSQL/pgvector 구현, psycopg 3)  ← 새 기능
  embed/worker.py      임베딩 작업자(작은 묶음·시간대별 크기·재시도)           ← 새 기능
  embed_worker.py      python -m app.embed_worker (상시 실행 / --once)
deploy/                systemd 예시 3개 — grantmatchpy.service(API, MODE=api, 8310, 300M)·grantmatchpy-attach.service(첨부, MODE=attach, 8311, 1500M)·grantmatchpy-embed.service(작업자, 1G)
tests/                 pytest (PHP ExtractTest 31개에 대응 + 서비스·내려받기·임베딩 시험)
tools/compare_with_db.py  실제 첨부 대조(DB 읽기 전용)
tools/eval/            임베딩 모델 비교(평가 세트·측정·순위 표) — docs/embedding_model_comparison.md
docs/                  임베딩 모델 비교와 추천
```

## 실행

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

$env:GRANTMATCHPY_TOKEN = "<긴 무작위 값>"        # 필수. 없으면 서비스가 시작하지 않는다
$env:GRANTMATCHPY_STORAGE = "D:\data\attachments"  # 기본 ./storage/attachments
.venv\Scripts\python.exe -m app                    # 127.0.0.1:8310 (GRANTMATCHPY_PORT 로 포트 변경)
```

운영(리눅스)은 서비스 셋으로 나눈다(`deploy/*.service`, User·EnvironmentFile 공통): API(`MODE=api`, 8310, MemoryMax 300M — 순위만, `/internal/embed` 를 쓰려면 모델 로딩 때문에 1G 로 올려야 함) · 첨부(`MODE=attach`, 8311, MemoryMax 1500M — 추출 하위 프로세스 `EXTRACT_MEMORY_MB=1024` 가 한도 안) · 임베딩 작업자(1G). Java 는 첨부 호출을 8311, rank·embed 를 8310 으로 보낸다. 운영(리눅스): `.env` 또는 systemd `EnvironmentFile` 에 값을 두고 `python -m app`. PDF 는 서버에 poppler(`apt install poppler-utils`)를 두면 `pdftotext` 를 쓰고, 없으면 pypdf 로 읽는다(한글 CID 글꼴 PDF 는 pypdf 가 못 읽는 것이 있으니 poppler 권장).

시험: `.venv\Scripts\python.exe -m pytest` (진짜 임베딩 모델 시험은 `$env:EMBED_REAL="1"` 일 때만 — 모델 캐시가 필요하다)

임베딩 모델 받기(처음 한 번, 네트워크 필요. 파일 두 개·약 135MB):
```powershell
$env:HF_HOME = "$PWD\.models"
.venv\Scripts\python.exe -c "from huggingface_hub import hf_hub_download as d; r='intfloat/multilingual-e5-small'; [d(r,f) for f in ('tokenizer.json','onnx/model_qint8_avx512_vnni.onnx')]"
```

### 설정(환경 변수)

| 이름 | 기본 | 설명 |
|---|---|---|
| `GRANTMATCHPY_TOKEN` | (필수) | `/internal/*` 의 `X-Internal-Token` 과 비교할 값 |
| `GRANTMATCHPY_STORAGE` | `./storage/attachments` | 첨부 저장소(`앞2글자/sha256`) |
| `GRANTMATCHPY_PORT` | 8310 | 포트(주소는 127.0.0.1 고정). 서비스를 나눌 때 서비스마다 다르게(API 8310·첨부 8311) |
| `GRANTMATCHPY_MODE` | `all` | `all` 모든 엔드포인트(개발·단일 서비스) · `api` health·`/internal/rank`·`/internal/embed` 만 · `attach` health·첨부 내려받기·추출만(모델·DB 접속 안 함). 모드에 없는 경로는 토큰 검사 전에 404 |
| `ALLOW_HTTP` | false | true 면 http 도 허용(PHP 는 허용했으나 이 서비스는 기본 금지) |
| `ALLOWED_HOSTS` | `bizinfo.go.kr,k-startup.go.kr` | 허용 도메인(끝이 같은 호스트 허용: `www.bizinfo.go.kr`) |
| `ALLOWED_PORTS` | `80,443` | 허용 포트 |
| `FETCH_TIMEOUT` / `FETCH_MAX_BYTES` | 120 / 30000000 | 내려받기 시간·크기 상한 |
| `FETCH_ALLOW_PRIVATE_IP`, `FETCH_CA_BUNDLE` | false / 빈 값 | **시험용**. 운영에서는 건드리지 않는다 |
| `EXTRACT_TIMEOUT` | 120 | 추출 하위 프로세스 시간 제한(초) |
| `EXTRACT_MEMORY_MB` | 1024 | 추출 프로세스 메모리 상한 |
| `EXTRACT_CONCURRENCY` | 2 | 동시에 도는 추출 프로세스 수 |
| `PDFTOTEXT_BIN` | — | pdftotext 경로(없으면 Git for Windows 것 → `/usr/bin/pdftotext`) |
| `EMBED_MODEL` | `intfloat/multilingual-e5-small` | 임베딩 모델(Hugging Face 저장소 이름 또는 `tokenizer.json`·`onnx/…` 가 든 폴더 경로). 이름에 `e5` 가 있으면 `query: `/`passage: ` 접두어를 붙인다 |
| `EMBED_BACKEND` | `onnx` | `onnx`(onnxruntime int8, 최대 약 550MB) 또는 `torch`(sentence-transformers fp32, 약 1.3GB — `requirements-eval.txt` 필요) |
| `EMBED_ONNX_FILE` | `onnx/model_qint8_avx512_vnni.onnx` | onnx 일 때 모델 안의 파일 |
| `EMBED_THREADS` | 1 | 계산 스레드 수(CPU 1개 서버 기준) |
| `EMBED_BATCH` | 1 | 한 번에 계산하는 글 수. 작을수록 메모리·패딩 낭비가 적고, 같은 글은 어떤 요청으로 보내도 같은 벡터가 나온다 |
| `EMBED_MAX_TOKENS` | 512 | 글 하나의 최대 토큰(넘으면 자름) |
| `HF_HOME` / `HF_HUB_OFFLINE` | — / — | 모델 캐시 폴더(개발은 `./.models`) / 1 이면 외부 접속 없이 캐시만 쓴다. 서버에서는 처음 한 번 내려받은 뒤 1 로 둔다 |
| `GRANTMATCHPY_DB_URL` | (작업자는 필수) | PostgreSQL(pgvector) 접속 문자열(psycopg). 서비스의 `/internal/rank` 와 작업자가 쓴다. 비면 rank 는 503, 작업자는 시작하지 않는다. 계정은 `embedding_jobs`·`grant_embeddings`·`member_embeddings`·`knowledge_embeddings` 만 읽고 쓰면 된다 |
| `EMBED_WORKER_BATCH_DAY` | 4 | 낮에 한 묶음에서 꺼낼 작업 수 |
| `EMBED_WORKER_BATCH_NIGHT` | 16 | 밤(한국 시간)의 묶음 크기 |
| `NIGHT_START_HOUR` / `NIGHT_END_HOUR` | 1 / 6 | 밤 시간대(한국 시간, 시작 이상·끝 미만. 시작 > 끝이면 자정을 넘는 구간) |
| `EMBED_WORKER_SLEEP_SECONDS` | 5 | 묶음 사이 쉬는 시간(초) |
| `EMBED_WORKER_IDLE_SECONDS` | 30 | 처리할 일이 없을 때(또는 모델·DB 문제로 멈췄을 때) 대기(초) |
| `EMBED_WORKER_RUN_SECONDS` | 600 | `--once` 한 번 실행의 시간 상한(초) |

작업자도 `EMBED_MODEL`·`EMBED_BACKEND`·`EMBED_ONNX_FILE`·`EMBED_THREADS`·`EMBED_BATCH`·`EMBED_MAX_TOKENS`·`HF_HOME` 을 그대로 쓴다(서비스와 같은 `.env` 를 읽어도 된다).

## 임베딩 작업자

서비스(API)와 **별개 프로세스**다. Java 가 `embedding_jobs` 에 쌓은 글을 작은 묶음씩 꺼내 벡터로 만들어 `grant_embeddings`·`member_embeddings`·`knowledge_embeddings`(지식 자료 청크, 대상 `KNOWLEDGE`)에 저장한다(표 정의: Java 쪽 `V4__embedding_jobs.sql`·`db/vector/V5__embeddings.sql`, 계약: `grantmatch/docs/04_벡터추천_맞춤진단_구현.md`).

```powershell
$env:GRANTMATCHPY_DB_URL = "<psycopg 접속 문자열>"      # 필수. 코드·로그·저장소에 넣지 않는다
.venv\Scripts\python.exe -m app.embed_worker             # 상시 실행: SIGTERM·SIGINT 에 현재 작업을 마치고 종료
.venv\Scripts\python.exe -m app.embed_worker --once      # 시간 상한(EMBED_WORKER_RUN_SECONDS) 안에서 한 번 돌고 종료(cron·timer 용)
```

- 한 묶음 = ① `REMOVE` 작업 최대 100건(벡터 삭제 + 작업 줄 삭제) → ② `PENDING` 을 `priority`·`id` 순(회원 소개 1 이 공고 5 보다 먼저)으로 낮 4건·밤 16건(설정) 꺼내 처리. 꺼낼 때 `FOR UPDATE SKIP LOCKED`, 한 묶음이 한 트랜잭션이다.
- 작업마다: 저장소의 현재 `(text_hash, model)` 이 작업의 것과 같으면 임베딩 없이 `DONE`(건너뜀), 아니면 임베딩 → 벡터 저장 → `DONE`. 공고는 `passage`, 회원 소개는 `query` 로 임베딩한다.
- 실패하면 시도 횟수 +1·`error` 기록(500자), 3번째 실패에 `FAILED`, 그 전에는 `PENDING` 으로 되돌린다. **모델 로딩 실패는 시도 횟수를 쓰지 않고** 이번 묶음을 중단하고 다음 주기에 다시 한다.
- 로그는 한국어 한 줄 요약(`처리 n건·건너뜀 n건·실패 n건·삭제 n건`). 글 내용·접속 값은 남기지 않는다.
- 프로세스 하나·스레드 `EMBED_THREADS`(기본 1, onnxruntime intra-op 1). 서버에서는 `deploy/grantmatchpy-embed.service`(메모리 1G·CPU 50%·Nice 10).
- 시험: `tests/test_embed_worker.py`(가짜 저장소·가짜 임베더). 진짜 PostgreSQL 시험(`tests/test_embed_store_pg.py`)은 `GMPY_TEST_PG_URL`(시험용 DB 접속 문자열)이 있고 그 서버에 pgvector 가 있을 때만 돈다 — 임시 스키마를 만들고 끝나면 지운다.

## 엔드포인트

모든 `/internal/*` 는 헤더 `X-Internal-Token` 이 `GRANTMATCHPY_TOKEN` 과 같아야 한다(상수 시간 비교, 아니면 401). 업무상 실패는 **HTTP 200 + `ok:false`** 로 돌려준다.

- `POST /internal/attachments/fetch` `{"url","name"}`
  - 성공 `{"ok":true,"sha256","size","contentType","storedPath"}` (`storedPath` 는 저장소 기준 `ab/abcd…`)
  - 실패 `{"ok":false,"error":"한국어 사유","permanent":bool}` — `permanent` 는 `너무 큽니다|허용 목록|허용하지 않는` (PHP GrantTextService 가 바로 SKIPPED 로 보내던 기준)
- `POST /internal/attachments/extract` `{"storedPath":"ab/abcd…"}`
  - 성공 `{"ok":true,"format","text","chars","parts"?}` (`parts` 는 zip 일 때만: `{name,format,chars}` 또는 `{name,skipped}`)
  - 실패 `{"ok":false,"error","permanent"}` — `permanent` 는 `지원하지 않는 형식|암호|배포용|빈 파일|너무 큼|너무 많`
  - `..`·절대 경로·역슬래시·드라이브 문자·저장소 밖을 가리키는 심볼릭 링크는 **HTTP 400** + `{"ok":false,"error","permanent":true}`
- `POST /internal/embed` `{"texts":["…"],"kind":"passage"|"query"}`
  - 성공 `{"ok":true,"model":"intfloat/multilingual-e5-small","dim":384,"vectors":[[…]]}` — 벡터는 L2 정규화된 float 목록(texts 와 같은 순서). 공고는 `passage`, 회사 소개·질의는 `query`
  - texts 는 한 번에 최대 32개, 각 8000자 이하(빈 목록·잘못된 kind·깨진 JSON 포함) — 어기면 **HTTP 400** + `{"ok":false,"error","permanent":true}`
  - 모델 파일을 못 찾거나 불러오지 못하면 **HTTP 503** + `{"ok":false,"error","permanent":false}`(나중에 다시)
  - 모델은 첫 요청 때 올라가고(약 1초) 프로세스 안에서 하나만 둔다. 계산은 한 번에 한 요청씩.
  - 모델 선택 근거·측정: `docs/embedding_model_comparison.md`. 공고 글 만들기: `app/embed/passage_text.py`
- `POST /internal/rank` `{"memberId":1,"grantIds":[3,9,…]}` (grantIds 최대 5000개, 0개면 빈 결과)
  - 성공 `{"ok":true,"scores":[{"grantId":3,"score":0.83},…]}` — 회원 소개와 공고 글의 코사인 유사도(0~1로 자름), **score 내림차순**(같으면 grantId 순). **벡터가 없는 공고는 빠진다. 회원 소개 벡터가 없으면 `scores: []`**(오류 아님)
  - 잘못된 본문(정수 아님·5000개 초과·깨진 JSON) **HTTP 400** + `{"ok":false,"error","permanent":true}`
  - DB 에 접속하지 못하거나 `GRANTMATCHPY_DB_URL` 이 없으면 **HTTP 503** + `{"ok":false,"error","permanent":false}` — Java 는 이때 유사도 없이 정렬한다
  - 벡터 표(`grant_embeddings` ⋈ `member_embeddings`, 같은 모델끼리)만 읽는다. **임베딩 모델을 올리지 않는다**(서비스 프로세스 메모리 절약)
- `GET /internal/health` → `{"status":"ok"}` (토큰 불필요)

## 안전장치

- 내려받기: https 만(기본), 허용 도메인, 포트 80/443 만, URL 에 사용자 정보 금지, 리다이렉트 3번까지이며 **매번** 허용 목록·사설/내부 IP 재검사. DNS 해석 결과 중 하나라도 사설·loopback·link-local·예약 주소면 거부하고, 검사한 IP 로 직접 접속(인증서는 원래 호스트 이름으로 검증)해 검사 뒤 DNS 가 바뀌는 공격을 막는다. 30MB 를 넘으면 받다가 중단(압축 응답은 풀린 크기로 센다), 120초 제한, 빈 파일·`text/html` 응답 거부, 받다 만 파일은 남기지 않음.
- 추출: 형식은 파일 머리(서명)로 판별, 크기 30MB, ZIP 항목 2,000개·풀린 합계 200MB·항목 64MB·압축률 200배 상한(HWPX·DOCX 는 읽는 본문 XML 에만 크기 검사), HWP 구역 압축 풀기 64MB 상한, ZIP 묶음 읽는 파일 40개·ZIP 안 ZIP 한 단계.
- XML: DOCTYPE 거부(HWPML 의 `<!ENTITY 이름 "&#숫자;">` 문자 개체 선언만 직접 치환 후 제거), 개체 치환·DTD·네트워크 접근 끈 lxml.
- 암호 걸린 HWP·배포용 HWP·배포용(암호화) HWPX 는 이유를 알려 거부. 손상 파일은 항상 `ExtractError`(한국어 메시지)로만 실패.
- **한 파일이 이상해도 서비스는 멈추지 않는다**: 추출은 매번 별도 파이썬 프로세스에서 시간 제한(기본 120초, 넘으면 자식 프로세스까지 종료 → `글자 추출 시간 초과(120초)`)·메모리 상한으로 돈다. 상한을 넘으면 `파일이 너무 큼(추출 중 메모리 상한 초과)`.

## PHP 와 다른 점

| 항목 | PHP | 이 서비스 | 이유 |
|---|---|---|---|
| http | 허용 | 기본 금지(`ALLOW_HTTP`) | 요구 사항 |
| 사설/내부 IP | 검사 없음 | DNS 해석 결과까지 검사·IP 고정 접속 | SSRF 방지(요구 사항) |
| 포트·사용자 정보 | 같음 | 같음 | — |
| 리다이렉트 주소 합치기 | 직접 구현(`//host` 형태 미처리) | `urljoin` | 표준 규칙, 결과는 어차피 다시 검사 |
| 시간 제한 | 추출 N초 + 바깥 N+5초 | 바깥에서 N초 한 번 | 단순화 |
| 메모리 상한 | PHP `memory_limit` 256M | 프로세스 메모리 상한 1024MB | 파이썬·lxml 기본 사용량이 큼 |
| PDF 대체 | smalot/pdfparser | pypdf | 같은 역할의 순수 파이썬 |
| HWP OLE | 직접 구현(Cfb.php) | olefile | 요구 사항 허용 |
| 유니코드 버전 | PCRE | 파이썬 3.14 (유니코드 16) | `normalize` 의 제어 문자 제거에서 새로 배정된 글자(예: U+1B7F)를 파이썬은 남긴다. 한글 문서에는 영향 없음 |
| 메모리 상한 초과 | PHP 치명 오류(`글자 추출 실패`) | `파일이 너무 큼(추출 중 메모리 상한 초과)` → permanent | 다시 해도 같으므로 |

## 알려진 한계

- 글자 없는 스캔 PDF 는 빈 글자(OCR 안 함, PHP 와 같음).
- 압축 안 파일 이름의 짝 없는 글자는 `?` 로 바뀔 수 있다(PHP mbstring 과 같은 처리).
- `normalize` 의 제어 문자 판정은 파이썬 유니코드 표를 따르므로 아주 최신에 배정된 글자는 PHP 와 다를 수 있다.
- PDF 는 poppler 가 있어야 PHP 와 같은 결과가 나온다(pypdf 는 줄 나눔·글꼴 처리가 다름).

## 실제 첨부 대조 도구

```powershell
.venv\Scripts\python.exe tools\compare_with_db.py --workers 4
```

PHP 가 개발 DB(`grant_attachments`)에 둔 글자와 Python 결과를 견주고(DB 는 읽기 전용 SELECT, 접속 값은 환경 변수 `GM_DST_*` 또는 `GM_ENV_FILE`), 차이가 나면 지금 PHP 코드로 다시 뽑은 것과도 견준다. DB 의 PDF 글자는 PDF 엔진을 poppler 로 바꾸기 전에 뽑은 것이 많아 DB 와는 다르다.
