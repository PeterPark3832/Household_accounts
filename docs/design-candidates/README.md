# 디자인 후보 (Design Candidates)

향후 사용을 위해 보관하는 대시보드 UI 디자인 후보입니다.
현재 활성 대시보드(`dashboard/templates/index.html`)는 **라이트 보라/핑크 테마**를 유지합니다.

## index.dark-fintech.html — 다크 핀테크

토스·뱅킹앱 스타일의 다크 단일 모드 리디자인 완성본입니다.

- **설계 근거**: [`../ui-redesign-dark-fintech.md`](../ui-redesign-dark-fintech.md)
  (컬러 토큰, WCAG 대비율 실측, 차트 15색 CVD 검증, 컴포넌트 스펙 포함)
- **검증 완료**: 131개 테스트 통과, Playwright 데스크탑/태블릿/모바일/로그인 렌더 확인
- **기반 버전**: `index.html`이 토큰 인증 + 거래 추가/수정/삭제 모달을 포함하던 시점
  (커밋 `7e6991e`)에서 파생

### 적용 방법

이 후보를 활성 디자인으로 채택하려면:

```bash
cp docs/design-candidates/index.dark-fintech.html dashboard/templates/index.html
python -m pytest tests/   # 회귀 확인
```

적용 전, 파생 시점 이후 `index.html`에 추가된 기능이 있다면
[`../ui-redesign-dark-fintech.md`](../ui-redesign-dark-fintech.md)의 토큰·컴포넌트
규칙을 그 변경분에도 반영해야 합니다.
