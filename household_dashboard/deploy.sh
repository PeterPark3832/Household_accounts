#!/bin/bash
set -e

REPO_DIR="/root/Household_accounts"
DASH_DIR="$REPO_DIR/dashboard"

echo "=== 가계부 대시보드 배포 ==="

# 1. Repo clone (봇이 이미 설치되어 있으면 skip)
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/PeterPark3832/Household_accounts.git "$REPO_DIR"
else
  echo "[OK] 레포지토리 존재"
fi

# 2. 대시보드 파일 복사
mkdir -p "$DASH_DIR/templates"
# (이미 SCP로 파일을 올렸다면 이 단계는 생략)

# 3. 가상환경 + 패키지
cd "$DASH_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "[OK] Python 패키지 설치 완료"

# 4. .env 에 대시보드 인증 추가 (이미 있으면 skip)
if ! grep -q "DASHBOARD_USER" "$REPO_DIR/.env" 2>/dev/null; then
  echo ""                        >> "$REPO_DIR/.env"
  echo "DASHBOARD_USER=admin"    >> "$REPO_DIR/.env"
  echo "DASHBOARD_PASS=change_me_please" >> "$REPO_DIR/.env"
  echo "DASHBOARD_PORT=8080"     >> "$REPO_DIR/.env"
  echo "[추가] .env 에 대시보드 설정 추가됨 — 비밀번호를 꼭 변경하세요!"
fi

# 5. Systemd 서비스 등록
cp "$DASH_DIR/dashboard.service" /etc/systemd/system/household-dashboard.service
systemctl daemon-reload
systemctl enable household-dashboard.service
systemctl restart household-dashboard.service

echo ""
echo "✅ 배포 완료!"
echo "👉 http://66.42.37.65:8080"
echo ""
echo "⚠️  /root/Household_accounts/.env 에서 DASHBOARD_PASS 변경하세요!"
echo "   변경 후: systemctl restart household-dashboard"
