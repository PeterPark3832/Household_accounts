# 💰 가족 가계부 텔레그램 봇 v2

**Google Sheets + 멀티유저 + 차트**

## 파일 구조
```
household_budget_bot_v2/
├── budget_bot.py        # 메인 봇
├── sheets.py            # Google Sheets CRUD
├── charts.py            # 차트 생성 (matplotlib)
├── requirements.txt
├── credentials.json     # Google Service Account (직접 다운로드)
├── .env                 # 환경변수
├── .env.example
└── budget_bot.service   # systemd
```

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

## 2. Vultr 서버 배포

```bash
# 1. 파일 업로드
scp -r household_budget_bot_v2 ubuntu@your-ip:~/
scp credentials.json ubuntu@your-ip:~/household_budget_bot_v2/

# 2. 서버 접속
ssh ubuntu@your-ip
cd household_budget_bot_v2

# 3. 한국어 폰트 (차트용)
sudo apt-get install -y fonts-nanum

# 4. 가상환경 + 패키지
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 5. .env 설정
cp .env.example .env && nano .env

# 6. 권한 설정
chmod 600 .env credentials.json
```

---

## 3. systemd 등록

```bash
sudo cp budget_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable budget_bot
sudo systemctl start budget_bot
sudo systemctl status budget_bot
```

---

## 4. 로그 확인

```bash
sudo journalctl -u budget_bot -f
tail -f budget_bot.log
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
