# 배포 가이드 (Vultr + GitHub Actions)

데이터는 전부 Google Sheets에 있어 **서버는 일회용**입니다. 아래 두 가지로
"푸시하면 자동 배포"와 "10분 재구축"을 Vultr 위에서 그대로 얻습니다.

- **자동 배포**: `.github/workflows/deploy.yml` + `scripts/deploy-remote.sh`
- **재구축(프로비저닝)**: `scripts/provision.sh`

---

## A. 새 서버 세팅 / 재구축 — `scripts/provision.sh`

새 Vultr 인스턴스(Ubuntu)에서 한 번 실행하면 패키지 설치 → 유저 생성 → 코드
클론 → venv 2개 → systemd 등록 → 자동보안업데이트 → 배포용 sudo까지 끝납니다.

```bash
# 서버에서 (root)
curl -fsSL https://raw.githubusercontent.com/PeterPark3832/Household_accounts/main/scripts/provision.sh -o provision.sh
sudo bash provision.sh
```

시크릿 두 개는 저장소에 없으므로(gitignore) 직접 올립니다:

```bash
# 로컬에서
scp .env             budget@<서버IP>:/home/budget/household/.env
scp credentials.json budget@<서버IP>:/home/budget/household/credentials.json
# 서버에서 다시
sudo bash provision.sh      # 이번엔 시크릿이 있으므로 서비스까지 기동
```

경로/유저가 다르면 오버라이드:

```bash
sudo APP_USER=ubuntu APP_DIR=/home/ubuntu/household BRANCH=main bash provision.sh
```

프로비저닝이 만드는 것:
- 서비스 `budget_bot.service`, `dashboard.service` (enable + 기동)
- `/home/<user>/.household-deploy.env` — 배포 스크립트가 읽는 서버별 설정
- `/etc/sudoers.d/household-deploy` — 배포 유저가 **두 서비스 재시작만** 무암호 sudo
- `unattended-upgrades` — 자동 보안 패치

---

## B. 자동 배포 — GitHub Actions

`main` 에 푸시되면 → 테스트 실행 → 통과 시 서버에 SSH로 배포(최신 코드 pull →
의존성 업데이트 → 서비스 재시작 → `/health` 검증). Actions 탭에서 수동 실행도 가능.

### 1) 배포 전용 SSH 키 생성

```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"
# 공개키를 서버의 배포 유저에 등록
ssh-copy-id -i deploy_key.pub budget@<서버IP>
#   또는: cat deploy_key.pub | ssh budget@<서버IP> 'cat >> ~/.ssh/authorized_keys'
```

### 2) GitHub 저장소 Secrets 등록
`Settings → Secrets and variables → Actions → New repository secret`

| Secret | 값 | 필수 |
|---|---|---|
| `DEPLOY_HOST` | 서버 IP 또는 도메인 | ✅ |
| `DEPLOY_USER` | 배포 유저 (예: `budget`) | ✅ |
| `DEPLOY_SSH_KEY` | `deploy_key` **개인키 전체** 내용 | ✅ |
| `DEPLOY_PORT` | SSH 포트 (기본 22면 생략) | ⬜ |
| `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <IP>` 출력 (호스트키 핀 고정, 권장) | ⬜ |

> `DEPLOY_KNOWN_HOSTS` 를 넣으면 MITM 방어가 강해집니다. 생략 시 매 실행 keyscan(TOFU).

### 3) 배포
```bash
git push origin main        # → 자동으로 테스트 후 배포
```
결과는 Actions 탭에서 확인. 배포 스크립트가 두 서비스 active + 대시보드 헬스체크까지
검증하고, 실패하면 워크플로우가 빨갛게 뜹니다.

### 수동 배포 (Actions 없이)
서버에서 직접:
```bash
ssh budget@<서버IP> 'bash -s' < scripts/deploy-remote.sh
```

---

## C. 권장 부가 세팅 (선택)

- **업타임 모니터**: [UptimeRobot](https://uptimerobot.com)(무료)로 `http://<IP>:8080/health`
  를 5분마다 핑 → 죽으면 이메일/텔레그램 알림.
- **HTTPS**: 외부 공개 시 nginx + Let's Encrypt(`certbot`)로 `:8080` 앞단에 TLS 종단.
- **스냅샷**: 데이터가 Sheets에 있어 불필요하지만, 안심되면 Vultr 자동 스냅샷 사용.
  진짜 필요한 백업은 `.env` + `credentials.json` 오프라인 보관뿐입니다.

---

## 서비스명이 다를 때
현재 서버가 다른 유닛명을 쓰면 `/home/<user>/.household-deploy.env` 를 수정하고
sudoers(`/etc/sudoers.d/household-deploy`)의 서비스명도 함께 맞추세요.
