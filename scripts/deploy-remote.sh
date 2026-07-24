#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 서버에서 실행되는 배포 스크립트 (GitHub Actions가 SSH로 파이프해서 실행).
# 최신 코드로 갱신 → 두 venv 의존성 업데이트 → 서비스 재시작 → 헬스체크.
#
# 서버별 설정은 ~/.household-deploy.env 에서 오버라이드 (provision.sh가 생성).
# 이 스크립트는 로컬에서 수동 배포용으로도 그대로 실행 가능:  bash scripts/deploy-remote.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 서버별 설정 (있으면 로드)
[ -f "$HOME/.household-deploy.env" ] && . "$HOME/.household-deploy.env"

# 기본값 (실제 배포 구성 기준; 설정파일로 오버라이드 가능)
# 봇·대시보드·git 체크아웃이 모두 /root/Household_accounts 한 곳에서 root 로 실행됨.
: "${APP_DIR:=/root/Household_accounts}"
: "${BRANCH:=main}"
: "${BOT_VENV:=$APP_DIR/venv}"
: "${WEB_VENV:=$APP_DIR/dashboard/venv}"
: "${BOT_SERVICE:=budget_bot.service}"
: "${WEB_SERVICE:=household-dashboard.service}"
: "${HEALTH_URL:=http://127.0.0.1:8081/health}"

# root 로 실행되면 sudo 불필요; 아니면 sudo 로 재시작(sudoers NOPASSWD 필요).
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"

echo "▶ 배포 시작: $APP_DIR (branch=$BRANCH)"
cd "$APP_DIR"

# 1) 최신 코드 (추적 파일만 교체; .env·credentials.json·venv·로그는 untracked라 보존됨)
git fetch --prune origin
git reset --hard "origin/$BRANCH"
echo "  코드: $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"

# 2) 의존성 (변경 없으면 즉시 통과)
echo "▶ 봇 의존성"
"$BOT_VENV/bin/pip" install -q -r requirements.txt
echo "▶ 대시보드 의존성"
"$WEB_VENV/bin/pip" install -q -r dashboard/requirements.txt

# 3) 재시작 (비-root 로 배포 시 sudoers NOPASSWD 필요 — provision.sh가 설정)
echo "▶ 서비스 재시작"
$SUDO systemctl restart "$BOT_SERVICE"
$SUDO systemctl restart "$WEB_SERVICE"
sleep 2

# 4) 검증: 두 서비스 active + 대시보드 헬스체크
fail=0
for s in "$BOT_SERVICE" "$WEB_SERVICE"; do
  if systemctl is-active --quiet "$s"; then
    echo "  ✅ $s active"
  else
    echo "  ❌ $s 가 active 아님 (서버에서 'journalctl -u $s -n 50' 확인)"; fail=1
  fi
done
if curl -fsS --max-time 15 "$HEALTH_URL" >/dev/null; then
  echo "  ✅ 대시보드 헬스체크 OK ($HEALTH_URL)"
else
  echo "  ❌ 대시보드 헬스체크 실패 ($HEALTH_URL)"; fail=1
fi

[ "$fail" = 0 ] && echo "✅ 배포 완료" || { echo "✖ 배포 검증 실패"; exit 1; }
