# K-뷰티 시장·경쟁사 트래커

국내 화장품 수출 시장(축1)과 주요 경쟁 브랜드 실적(축2)을 추적하는 대시보드.
자사 = **Skin1004 (크레이버코퍼레이션)**. GitHub Pages로 배포되고 **매월 자동 갱신**됩니다.

---

## 동작 방식 (자동 갱신 구조)

```
GitHub Actions (매월 1일 + 수동)
   └─ python etl.py --outdir data        # DART·관세청 적재 (키 없으면 시드)
         └─ data/market.json, data/competitors.json 갱신·커밋
               └─ index.html 이 로드 시 data/*.json 를 fetch
                     └─ GitHub Pages 가 정적 배포 → 공유 링크
```

- **index.html** — 자체완결(React·차트 내장, 빌드 불필요). 열릴 때 `data/*.json`을 불러오고, 실패하면 내장 시드로 표시.
- **data/*.json** — 실제 수치. 매월 Actions가 갱신.
- 페이지 우측 상단에 **데이터 소스(시드/라이브)** 와 **갱신일**이 표시됩니다.

---

## 배포 방법 (최초 1회, 약 5분)

1. **이 폴더를 GitHub 저장소에 푸시** (`index.html`, `data/`, `etl.py`, `.github/workflows/update.yml` 포함).
2. 저장소 **Settings → Pages → Build and deployment → Source: GitHub Actions** 선택.
3. **Actions 탭 → "Update & Deploy dashboard" → Run workflow** 로 즉시 1회 배포.
4. 배포 완료 후 `https://<사용자명>.github.io/<저장소명>/` 이 공유 링크입니다.

→ 받는 사람은 링크 클릭 한 번으로 접속하며, 소스코드는 다룰 필요가 없습니다.
   (사이트에는 `index.html`과 `data/`만 배포되어 `etl.py` 등 코드는 페이지로 노출되지 않습니다.
   단, **저장소가 public이면 코드는 GitHub에서 보입니다** — 코드도 비공개로 하려면 private 저장소 + Pages 권한을 사용하세요.)

---

## 실제(라이브) 데이터로 갱신하려면

키가 없으면 매월 **시드 데이터**가 그대로 재배포됩니다(항상 정상 작동). 실제 공시 수치로 채우려면:

1. **API 키 발급**
   - DART: <https://opendart.fss.or.kr> (경쟁사 재무)
   - 관세청: <https://data.go.kr> (수출 시장)
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에 추가:
   - `DART_API_KEY`
   - `CUSTOMS_API_KEY`
3. **`etl.py`의 `ENTITIES`에 각 브랜드 운영 법인의 `corp_code` 입력** (DART 고유번호).
   `resolve_corp_codes()`로 이름→코드 매핑을 도울 수 있습니다.
4. 다음 실행부터 라이브 적재 → 실패 시 자동으로 시드 폴백(배포는 멈추지 않음).

> ⚠️ 정직한 한계
> - **해외/지역별 매출**은 재무 API에 없어 사업보고서·감사보고서 원문을 파싱(`parse_regional_sales`)하므로, 라이브 첫 적용 시 **사람 검수**가 필요합니다. 검증 전까지는 `DISCLOSED_OVERSEAS` 실측값(스킨1004 98%·COSRX 80%·Medicube 55%)과 추정치를 사용합니다.
> - COSRX·VT·Medicube는 브랜드 단독이 아닌 **모회사 연결/화장품 부문** 기준.
> - 매출 미확인 브랜드(I'm From·Isntree·Purito 등)는 제외되어 있습니다.

---

## 갱신 주기

소스가 발표되는 시점에 의미가 있습니다(연중 상시 아님):
- 시장(수출): 월간 (관세청, 매월 중순)
- 상장 경쟁사(APR·VT·아모레): 분기
- 비상장 경쟁사(구다이 계열·비나우 등): 연 1회 (감사보고서, 약 4월)

cron은 매월 1일 실행이며, 주기/일자는 `.github/workflows/update.yml`의 `cron`에서 조정하세요.

---

## 로컬 확인

```bash
python etl.py --demo --outdir data   # 시드 JSON 생성
python -m http.server 8000           # http://localhost:8000 에서 index.html 열기 (fetch는 http로만 동작)
```

`index.html`을 더블클릭(file://)해도 열리지만, 그 경우 fetch가 차단되어 **내장 시드**로 표시됩니다.

---

## 파일

| 파일 | 역할 |
|------|------|
| `index.html` | 대시보드(자체완결, 빌드 불필요) — `data/*.json` 로드, 실패 시 시드 |
| `data/market.json` | 축1 시장 데이터 |
| `data/competitors.json` | 축2 브랜드별 매출·해외·자사(`brands`/`homeTrend`/`home2025`) |
| `etl.py` | DART·관세청 적재 → JSON. 키 없으면 시드. `--outdir` 지정 |
| `.github/workflows/update.yml` | 매월 자동 갱신 + Pages 배포 |
| `requirements.txt` | `requests` |
