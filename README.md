# 💰 가족 가계부 텔레그램 봇 v2

**Google Sheets + 멀티유저 + 차트**

## 파일 구조
```
Household_accounts/
├── budget_bot.py        # 메인 봇
├── sheets.py            # Google Sheets CRUD
├── charts.py            # 차트 생성 (matplotlib)
├── budget_bot.service   # systemd (봇)
├── requirements.txt
├── credentials.json     # Google Service Account (직접 다운로드)
├── .env / .env.example  # 환경변수
├── dashboard/           # 웹 대시보드 (FastAPI, :8081) + dashboard.service
├── scripts/             # provision.sh(재구축) · deploy-remote.sh(배포)
├── docs/DEPLOY.md       # 배포·재구축 가이드
└── tests/
```

> **배포 구성**: 봇·대시보드가 모두 `/root/Household_accounts` 한 곳에서 root 로 실행됩니다.
> `main` 에 push 하면 GitHub Actions 가 테스트 후 서버에 자동 배포합니다.
> 상세는 [`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## 1. Google Sheets 서비스 계정 설정

### 1-1. Google Cloud Console
1. https://console.cloud.google.com 접속
2. 새 프로젝트 생성 (예: `family-budget`)
3. **API 및 서비스 → 라이브러리** 검색:
   - `Google Sheets API` → 사용 설정
   - `Google Drive API` → 사용 설정
4. **API 및 서비스 → 사용자 인증 정보**
   - `사용자 인증 정보 만들기` → `서비스 계정`
   - 이름: `budget-bot` → 만들기
5. 생성된 서비스 계정 클릭 → **키** 탭
   - `키 추가` → `새 키 만들기` → JSON
   - 다운로드된 파일을 `credentials.json`으로 저장

### 1-2. Google Sheets 공유
1. Google Sheets에서 새 스프레드시트 생성
2. URL에서 Spreadsheet ID 복사:
   `https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit`
3. 우상단 **공유** 버튼
4. `credentials.json` 안의 `client_email` 값을 **편집자**로 추가

---

## 2. 서버 배포

### 권장: 자동 배포 · 10분 재구축 — [`docs/DEPLOY.md`](docs/DEPLOY.md)

- **새 서버 / 재구축**: `sudo bash scripts/provision.sh` (패키지 → 코드 → venv 2개 →
  systemd 등록까지). 시크릿(`​.env`, `credentials.json`)만 올리면 됩니다.
- **자동 배포**: `git push origin main` → 테스트 → 서버에 SSH 배포(pull → 의존성 →
  서비스 재시작 → `/health` 검증). 데이터는 전부 Google Sheets 에 있어 **서버는 일회용**.

### 수동 배포 (Actions 없이 서버에서 직접)

```bash
ssh root@<서버IP> 'bash -s' < scripts/deploy-remote.sh
```

프로비저닝 없이 처음 세팅한다면 `docs/DEPLOY.md` A절을 따르세요. 핵심만:
`/root/Household_accounts` 에 클론 → `python3 -m venv venv` + 대시보드 venv →
`sudo apt-get install -y fonts-nanum`(차트 한글) → `.env`/`credentials.json` 배치
(`chmod 600`) → `budget_bot.service`·`dashboard/dashboard.service` 를
`/etc/systemd/system/` 에 설치 후 `enable --now`.

---

## 3. 로그 확인

```bash
sudo journalctl -u budget_bot -f              # 봇
sudo journalctl -u household-dashboard -f     # 대시보드
```

---

## Google Sheets 구조 (자동 생성)

| 시트 | 컬럼 |
|------|-------|
| records | id, user_id, display_name, type, category, amount, memo, date |
| budgets | user_id, display_name, category, amount, year, month |
| users   | user_id, display_name, role, joined_at |

---

## 멀티유저 흐름

1. 가족 구성원이 봇에 /start 전송
2. 관리자에게 승인 요청 알림 도착
3. 관리자: `/approve {user_id}`
4. 승인된 구성원 → 바로 사용 가능

---

## 봇 기능 전체

| 버튼/명령어 | 기능 |
|-------------|------|
| 💰 수입 기록 | 카테고리 → 금액 → 메모 |
| 💸 지출 기록 | 카테고리 → 금액 → 메모 + 예산 초과 즉시 알림 |
| 📊 이번달 요약 | 본인 카테고리별 + 예산 대비 현황 |
| 👨‍👩‍👧 가족 현황 | 가족 전체 합산 + 구성원별 지출 |
| 📋 최근 내역 | 최근 10건 |
| 🎯 예산 설정 | 카테고리별 월 예산 한도 |
| 📈 차트 보기 | 파이차트/예산대비/월별트렌드/가족비교 |
| 📤 CSV 내보내기 | 이번달 데이터 CSV 다운 |
| /del {id} | 기록 삭제 |
| /rename {이름} | 표시 이름 변경 |
| /approve {id} | (어드민) 가입 승인 |
| /users | (어드민) 구성원 목록 |

---

## 자동 발송 스케줄

- 매월 1일 09:00 KST → 전월 리포트 (구성원 전체)
- 매주 월요일 09:00 KST → 이번달 현황 (구성원 전체)

---

## 수입/지출 카테고리

**수입:** 급여, 투자수익, 배당금, 부업, 보너스, 환급/환불, 기타수입

**지출:** 식비, 카페/음료, 교통비, 주거비, 의료/건강, 교육비,
쇼핑, 문화/여가, 통신비, 보험, 구독서비스, 경조사, 기타지출
