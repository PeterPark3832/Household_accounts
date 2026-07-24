#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 새 Vultr(또는 임의의 Ubuntu) 박스를 처음부터 완전 가동 상태로 만드는 스크립트.
# 데이터는 전부 Google Sheets에 있으므로 서버는 "일회용" — 이 스크립트 + .env +
# credentials.json 만 있으면 10분 안에 어디서든 복구됩니다.
#
# 기본 구성: /root/Household_accounts 에서 root 로 실행 (봇+대시보드 동일 경로).
# /root 는 0700 이라 전용 계정으로는 접근 불가하므로 root 로 통일한다.
#
# 사용법 (root 로 실행):
#   sudo bash provision.sh
#   # 완료 후 안내에 따라 .env / credentials.json 업로드 → 재실행하면 자동 기동
#
# 전용 계정 구성으로 오버라이드 (경로가 /root 밖일 때만 가능):
#   sudo APP_USER=budget APP_DIR=/home/budget/household bash provision.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_USER="${APP_USER:-root}"
APP_DIR="${APP_DIR:-/root/Household_accounts}"
REPO="${REPO:-https://github.com/PeterPark3832/Household_accounts.git}"
BRANCH="${BRANCH:-main}"
BOT_SERVICE="budget_bot.service"
WEB_SERVICE="household-dashboard.service"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8081/health}"

# 저장소 유닛 파일의 표준 경로 (install_unit 의 sed 치환 기준점)
CANON_DIR="/root/Household_accounts"

[ "$(id -u)" = 0 ] || { echo "root 로 실행하세요:  sudo bash provision.sh"; exit 1; }

# APP_USER 로 실행하는 헬퍼 (root 면 그대로, 아니면 sudo -u)
if [ "$APP_USER" = root ]; then AS_USER=(); else AS_USER=(sudo -u "$APP_USER"); fi

echo "▶ 프로비저닝: user=$APP_USER dir=$APP_DIR branch=$BRANCH"

# 1) 시스템 패키지 (fonts-nanum = matplotlib 한글 차트용)
echo "▶ 패키지 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl fonts-nanum unattended-upgrades

# 2) 전용 유저 (root 면 생략)
if [ "$APP_USER" != root ] && ! id "$APP_USER" &>/dev/null; then
  echo "▶ 유저 생성: $APP_USER"
  useradd -r -m -d "/home/$APP_USER" -s /bin/bash "$APP_USER"
fi
HOME_DIR="$(getent passwd "$APP_USER" | cut -d: -f6)"

# 3) 코드 (있으면 갱신, 없으면 클론)
echo "▶ 코드 준비"
if [ -d "$APP_DIR/.git" ]; then
  "${AS_USER[@]}" git -C "$APP_DIR" fetch --prune origin
  "${AS_USER[@]}" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  install -d -o "$APP_USER" -g "$APP_USER" "$(dirname "$APP_DIR")"
  "${AS_USER[@]}" git clone -b "$BRANCH" "$REPO" "$APP_DIR"
fi

# 4) venv 2개 + 의존성
echo "▶ 봇 venv"
"${AS_USER[@]}" python3 -m venv "$APP_DIR/venv"
"${AS_USER[@]}" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
"${AS_USER[@]}" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "▶ 대시보드 venv"
"${AS_USER[@]}" python3 -m venv "$APP_DIR/dashboard/venv"
"${AS_USER[@]}" "$APP_DIR/dashboard/venv/bin/pip" install -q --upgrade pip
"${AS_USER[@]}" "$APP_DIR/dashboard/venv/bin/pip" install -q -r "$APP_DIR/dashboard/requirements.txt"

# 5) systemd 유닛 설치 (유닛 파일의 표준 경로/유저를 실제 값으로 치환)
echo "▶ systemd 유닛 설치"
install_unit() {  # <소스파일> <설치이름>
  sed -e "s#$CANON_DIR#$APP_DIR#g" \
      -e "s/^User=.*/User=$APP_USER/" \
      "$1" > "/etc/systemd/system/$2"
}
install_unit "$APP_DIR/budget_bot.service"          "$BOT_SERVICE"
install_unit "$APP_DIR/dashboard/dashboard.service" "$WEB_SERVICE"
systemctl daemon-reload
systemctl enable "$BOT_SERVICE" "$WEB_SERVICE" >/dev/null

# 6) 배포 유저에 서비스 재시작만 무암호 sudo 허용 (비-root 배포 시에만 필요)
if [ "$APP_USER" != root ]; then
  echo "▶ sudoers (배포용 재시작 권한)"
  SYSCTL="$(command -v systemctl)"
  cat > /etc/sudoers.d/household-deploy <<EOF
$APP_USER ALL=(root) NOPASSWD: $SYSCTL restart $BOT_SERVICE, $SYSCTL restart $WEB_SERVICE
EOF
  chmod 440 /etc/sudoers.d/household-deploy
  visudo -cf /etc/sudoers.d/household-deploy >/dev/null
fi

# 7) 자동 보안 업데이트
echo "▶ 자동 보안 업데이트"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# 8) 배포 스크립트가 읽을 서버 설정 ($HOME_DIR/.household-deploy.env)
echo "▶ 배포 설정 파일"
"${AS_USER[@]}" tee "$HOME_DIR/.household-deploy.env" >/dev/null <<EOF
APP_DIR=$APP_DIR
BRANCH=$BRANCH
BOT_SERVICE=$BOT_SERVICE
WEB_SERVICE=$WEB_SERVICE
HEALTH_URL=$HEALTH_URL
EOF

# 9) 시크릿 확인 후 기동
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
missing=0
for f in .env credentials.json; do
  [ -f "$APP_DIR/$f" ] || { echo "  ⚠ 없음: $APP_DIR/$f"; missing=1; }
done
chmod 600 "$APP_DIR/.env" "$APP_DIR/credentials.json" 2>/dev/null || true

echo
if [ "$missing" = 1 ]; then
  cat <<EOF
────────────────────────────────────────────────────────────
비밀 파일이 아직 없습니다. 로컬에서 업로드 후 이 스크립트를 다시 실행하세요:

  scp .env             $APP_USER@<서버IP>:$APP_DIR/.env
  scp credentials.json $APP_USER@<서버IP>:$APP_DIR/credentials.json

(양식: $APP_DIR/.env.example, $APP_DIR/credentials.json.example)
서비스는 등록/활성화됐지만, 시크릿이 채워질 때까지 기동하지 않습니다.
────────────────────────────────────────────────────────────
EOF
  exit 0
fi

echo "▶ 서비스 기동"
systemctl restart "$BOT_SERVICE" "$WEB_SERVICE"
sleep 2
systemctl is-active "$BOT_SERVICE" "$WEB_SERVICE" || true
if curl -fsS --max-time 15 "$HEALTH_URL" >/dev/null; then
  echo "✅ 완료 — 대시보드 정상 ($HEALTH_URL)"
else
  echo "⚠ 완료했지만 대시보드 헬스체크 실패 — 'journalctl -u $WEB_SERVICE -n 50' 확인"
fi
