# 가계부 대시보드 — 다크 핀테크 UI 리디자인 설계서

> **버전**: v1.0 (2026-07)
> **대상 파일**: `dashboard/templates/index.html` (main 기준 3,257줄, 인라인 CSS+JS 단일 파일)
> **컨셉**: 다크 핀테크 — 토스·뱅킹앱 스타일. 진한 네이비 배경, 민트 네온 액센트, 숫자 중심 고대비 타이포그래피
> **모드**: 다크 단일 모드 (라이트/토글 없음)
> **검증**: 모든 색 조합 WCAG 대비율 실측 완료, 차트 15색 팔레트 CVD(색각이상) 시뮬레이션 검증 통과

---

## 1. 개요와 디자인 원칙

### 1.1 컨셉

진한 네이비 배경 위에 밝기 단계로 표면을 쌓고, 채도 높은 민트 네온을 인터랙션 전용으로 아껴 쓴다.
**숫자가 주인공** — 금액은 가장 밝고 크게, 장식은 후퇴.

### 1.2 3대 규칙

1. **색의 층위 분리**: 데이터 색(income/expense/net/차트) ↔ 인터랙션 색(accent) ↔ 잉크(텍스트 계층)를 섞지 않는다. 금액 텍스트에 accent를 쓰지 않고, 버튼에 데이터 색을 쓰지 않는다(위험 액션 제외).
2. **입체감 = 밝기 + 1px 보더**: 다크에서 그림자는 보이지 않는다. 그림자는 모달/드로어 전용, 글로우는 accent 요소 전용(화면당 동시 2개 이하).
3. **그라디언트 채움 금지** (로고 마크 1곳 예외): 기존 "primary 버튼 3종 혼재" 부채를 단색 accent로 통일한다.

### 1.3 비변경 사항 (명시적 보존)

- DOM 구조, JS 로직, API 계약 — 전부 유지 (스킨 교체이지 리팩토링이 아님)
- 반응형 브레이크포인트 3단 구조 (≥1024 / 768–1023 / ≤767 / ≤360 미세조정)
- CSS 변수명 체계 — 변수 **이름은 유지하고 값만 교체** (`:root` 치환만으로 화면 80% 전환)
- 사이드바 + top-header + 모바일 하단탭 레이아웃 골격

---

## 2. 디자인 토큰

### 2.1 기존 변수 매핑 테이블

| 변수 | 현재 (라이트) | 신규 (다크 핀테크) | 역할 |
|---|---|---|---|
| `--bg` | `#F3EFFB` | **`#0A0E17`** | L0 페이지 바탕 (네이비-차콜, hue ~222) |
| `--sidebar-bg` | `#FFFFFF` | **`#0F1522`** | L1 사이드바/헤더/하단탭 |
| `--card` | `#FFFFFF` | **`#141C2E`** | L2 카드·모달·테이블 표면 |
| `--border` | `#EAE3F7` | **`#232E47`** | 헤어라인 보더 (비텍스트, 의도적 저대비) |
| `--text` | `#332F47` | **`#F2F5FA`** | 텍스트 1차 |
| `--muted` | `#8D87A6` | **`#8A96AD`** | 텍스트 3차/라벨 |
| `--accent` | `#8B5CF6` | **`#2CE0B4`** | 민트 네온 — 버튼·활성상태·포커스 전용 |
| `--accent-d` | `#7C3AED` | **`#17C39A`** | accent hover/pressed |
| `--accent-lt` | `#F1EBFE` | **`rgba(44,224,180,.12)`** | accent 틴트 표면 (hover wash, 활성 배경) |
| `--accent-pink` | `#EC4899` | **`#4C8DFF`** | **의미 변경**: 보조 액센트(블루). 정보성 하이라이트 전용 |
| `--accent-pink-lt` | `#FCE7F3` | **`rgba(76,141,255,.12)`** | 보조 액센트 틴트 |
| `--income` | `#22C55E` | **`#34D399`** | 수입 |
| `--expense` | `#FB7185` | **`#FF7A8A`** | 지출 |
| `--net` | `#818CF8` | **`#8AA8FF`** | 잔액/순액 |
| `--warn` | `#FBBF24` | `#FBBF24` (유지) | 경고 |
| `--shadow-sm` | 보라 그림자 | **`0 0 0 1px rgba(255,255,255,.02)`** | 사실상 폐기 — 표면 규칙으로 대체 (§4) |
| `--shadow` | 보라 그림자 | **`0 16px 48px rgba(0,0,0,.5)`** | 모달/드로어 전용 |
| `--radius` / `--radius-sm` / `--radius-pill` | 16 / 10 / 999px | 유지 | + 신규 `--radius-xs`, `--radius-lg` (§2.2) |

### 2.2 신규 토큰

기존 코드가 rgba 하드코딩으로 때우던 자리를 정식 토큰으로 승격:

```css
--surface-3:    #1B2437;                  /* L3: hover 행, secondary 버튼, 입력 필드 배경 */
--text-2:       #A9B4C9;                  /* 텍스트 2차 — 기존 text/muted 2단 → 3단 확장 */
--placeholder:  #66738E;                  /* 입력 placeholder (장식 텍스트 한정) */
--focus-ring:   0 0 0 3px rgba(44,224,180,.28);
--glow-accent:  0 4px 20px rgba(44,224,180,.22);
--income-bg:    rgba(52,211,153,.12);
--expense-bg:   rgba(255,122,138,.12);
--net-bg:       rgba(138,168,255,.12);
--warn-bg:      rgba(251,191,36,.12);
--danger-solid: #D6336C;                  /* 삭제 확정 버튼 채움 */
--overlay:      rgba(4,7,12,.65);         /* 모달/드로어 배리어 */
--radius-xs:    8px;
--radius-lg:    20px;
```

### 2.3 액센트 색 선정 근거 — 민트 vs 블루

**채택: 민트 네온 `#2CE0B4`**

1. **대비 성능**: 네이비 카드 위 10.06:1 — 후보 중 최고. 다크에서 "네온" 인상은 대비 격차에서 나오며, 블루 `#4C8DFF`는 5.31:1로 채움 버튼은 가능하지만 발광감이 약하다.
2. **시맨틱 충돌 최소**: 블루를 accent로 쓰면 `--net`(잔액, 블루 계열)과 같은 계열이 되어 KPI 잔액 카드가 브랜드색을 점유한 것처럼 읽힌다. 민트는 `--income`(에메랄드)과 인접 hue지만 의미 방향이 동일(긍정)하고, §1.2 규칙 1(accent는 컨트롤 전용, 금액 텍스트 금지)로 혼동 경로를 차단. Revolut·Robinhood 등 "그린=브랜드=긍정" 관례와 일치.
3. **지출색(로즈)과 보색 관계**라 화면 전체의 긴장감이 좋다.

**부록 B**: 블루 액센트 채택 시 파라미터 대안.

### 2.4 WCAG AA 대비율 실측표

상대휘도 공식으로 실측. 기준: 일반 텍스트 4.5:1, 큰 텍스트(≥18.66px bold)·UI 컴포넌트 3:1.

| 조합 | 대비율 | 판정 |
|---|---|---|
| `--text` `#F2F5FA` / card | 15.56:1 | AAA |
| `--text-2` `#A9B4C9` / card | 8.14:1 | AAA |
| `--muted` `#8A96AD` / card · hover표면 | 5.70 · 5.20:1 | AA |
| `--accent` `#2CE0B4` / card | 10.06:1 | AAA (텍스트로도 허용) |
| `--income` / card | 8.84:1 | AAA |
| `--expense` / card | 6.80:1 | AA+ |
| `--net` / card | 7.39:1 | AAA |
| `--warn` / card | 10.18:1 | AAA |
| primary 버튼: 텍스트 `#062A21` on `#2CE0B4` | 9.13:1 | AAA — 밝은 채움 + 어두운 텍스트 (다크 핀테크 관례) |
| danger 버튼: `#FFFFFF` on `#D6336C` | 4.62:1 | AA (채움 vs 카드 3.68:1도 통과) |
| `--placeholder` / card | 3.57:1 | placeholder 한정 허용 (실값 입력 시 `--text`) |
| `--border` / card | 1.26:1 | 비텍스트 헤어라인 — 의도적 저대비 |

---

## 3. 타이포그래피

### 3.1 폰트 스택

```css
font-family: 'Pretendard Variable', Pretendard, 'Segoe UI', -apple-system,
             BlinkMacSystemFont, 'Malgun Gothic', sans-serif;
```

- **Pretendard**: 한국어 핀테크 사실상 표준. 숫자 폭이 균일하고 웨이트 폭(100–900)이 넓어 고대비 숫자 연출에 적합.
- 로드: jsDelivr CSS 1줄 (`pretendard-dynamic-subset`). 실패 시 기존 스택으로 자연 폴백 — 위험 없음. 오프라인 환경이면 이 줄만 생략해도 설계 성립.

### 3.2 숫자 규칙

**모든 금액 표기 요소에** `font-variant-numeric: tabular-nums;` 적용:
`.kpi-value`, 미니 스탯 값, 테이블 금액 셀, 캘린더 `.cal-amt`, 모달 금액 입력.
→ 자릿수 정렬 + 값 갱신 시 레이아웃 흔들림 방지. 라벨/본문은 기본 비례 숫자 유지.

### 3.3 크기 스케일

| 토큰 | 값 | 용도 |
|---|---|---|
| display | 1.75rem / 800 / letter-spacing -0.02em | KPI 금액 (현재 1.5rem·700에서 상향) |
| num-lg | 1.2rem / 700 | 미니 스탯 값, 모달 금액 |
| title | 0.95rem / 700 | 카드 제목 |
| body | 0.88rem / 400–500 | 테이블, 폼 |
| caption | 0.74rem / 500 / `--muted` | 라벨, 배지, 축 |

다크 배경에서는 같은 웨이트가 더 굵어 보이므로(halation) 본문은 400–500 유지, **굵기는 숫자에만** 몰아준다. 모바일 `clamp()` 축소 로직은 유지하되 상한만 스케일에 맞춰 갱신.

---

## 4. 표면과 입체감 (Elevation)

| 레벨 | 색 | 용도 | 그림자 |
|---|---|---|---|
| L0 | `--bg` `#0A0E17` | 페이지 바탕 | 없음 |
| L1 | `--sidebar-bg` `#0F1522` | 사이드바, top-header, 모바일 하단탭 | 없음, 경계는 1px `--border` |
| L2 | `--card` `#141C2E` | 카드, 테이블, 토스트 | 없음, 1px `--border` |
| L3 | `--surface-3` `#1B2437` | hover 행, 입력 필드, secondary 버튼, 캘린더 i0 셀 | 없음 |
| 모달/드로어 | `--card` | + `--overlay` 배리어 | `--shadow` (유일한 그림자) |

**규칙**: 위로 갈수록 밝다.

- **body 배경**: 현재 파스텔 라디얼+리니어 그라디언트 → 평면 `--bg` + 옅은 네온 글로우 2점만:
  ```css
  background: radial-gradient(circle at 12% 0%,  rgba(44,224,180,.05), transparent 40%),
              radial-gradient(circle at 88% 100%, rgba(76,141,255,.06), transparent 45%),
              var(--bg);
  ```
  `background-attachment: fixed` 는 모바일 스크롤 리페인트 비용 때문에 **제거**.
- **글로우 사용처 전수 목록** (그 외 금지): primary 버튼 hover, `.nav-item.active`, 포커스 링, `.refresh-dot`, 오늘 날짜 셀 링. 값은 `--glow-accent` / `--focus-ring`만 사용.
- **전역 다크 선언**: `html { color-scheme: dark; }` + `<meta name="theme-color" content="#0A0E17">` — 네이티브 폼 컨트롤(date input)·스크롤바·모바일 상태바 다크 통일.

---

## 5. 컴포넌트 스펙

### 5.1 버튼 — 5종 통일 스킴

현재 primary 채움 3종 혼재(보라→핑크 그라디언트 / 단색 / 코랄→로즈 그라디언트)를 아래로 수렴:

| 종류 | 채움 | 텍스트 | 보더 | hover | disabled |
|---|---|---|---|---|---|
| **primary** | `--accent` 단색 | `#062A21` | 없음 | `--accent-d` + `--glow-accent` + translateY(-1px) | opacity .45, 글로우 제거 |
| **secondary** | `--surface-3` | `--text` | 1px `--border` | 보더 `--muted` | opacity .45 |
| **ghost** | 투명 | `--muted` | 없음 | 배경 `--accent-lt`, 텍스트 `--accent` | opacity .4 |
| **danger-solid** | `--danger-solid` | `#FFFFFF` | 없음 | 밝기 108% | opacity .45 |
| **danger-ghost** | `--expense-bg` | `--expense` | 1px rgba(255,122,138,.35) | 배경 알파 .2 | — |

- radius: 모두 `--radius-sm`(10px), pill형 CTA만 `--radius-pill`
- 용처: 삭제 확인 모달의 확정 버튼만 danger-solid. 행별 삭제 아이콘·logout은 danger-ghost.
- 공통: `transition: background .15s, box-shadow .15s, transform .1s` / `:focus-visible { box-shadow: var(--focus-ring); }`

### 5.2 입력 필드

배경 `--surface-3`, 1px `--border`, radius `--radius-xs`, 텍스트 `--text`, placeholder `--placeholder`.
focus: 보더 `--accent` + `--focus-ring`.
현재 `background: var(--bg)` 방식은 다크에서 "구멍"처럼 보이므로 L3 표면으로 올린다. `color-scheme: dark`로 date picker 아이콘 자동 반전.

### 5.3 필터 pill (`.fbtn`) / 검색박스

- 기본: 투명 배경, 1px `--border`, 텍스트 `--muted`, `--radius-pill`
- hover: 보더 `--muted`, 텍스트 `--text-2`
- **active** (그라디언트 폐기): `--accent-lt` 배경 + 1px `rgba(44,224,180,.4)` 보더 + `--accent` 텍스트 — 다크 핀테크는 채움 pill보다 **틴트 pill**이 표준 (발광 과다 방지)
- 수입/지출 전용 필터: 각자 `--income-bg` / `--expense-bg` 틴트로 동일 문법
- 검색박스: 입력 필드 스펙 + pill radius

### 5.4 배지 (`.badge-income` / `.badge-expense`)

`--income-bg` / `--expense-bg` + 시맨틱 텍스트색, `--radius-pill`. 구조 유지, rgba 하드코딩만 토큰으로 치환.

### 5.5 카드 / KPI

- 카드: L2 표면 + 1px `--border`, `--radius`(16px), **그림자 없음**
- KPI hover 리프트: `translateY(-2px)` + 보더 밝기 상승(`#2E3B5C`)으로 표현 (그림자 대신)
- KPI 상단 3px 컬러바 유지 — 다크에서도 유효한 장치
- 금액: display 스케일 + tabular-nums + 시맨틱 색. 라벨: `--muted` caption
- 미니 스탯 아이콘: 시맨틱 틴트 배경(`--*-bg`) + 시맨틱 색 아이콘, radius `--radius-xs`

### 5.6 모달 3종 (추가/수정/삭제) + 토스트

- 공통: `--card` + 1px `--border` + `--radius` + `--shadow`, 배리어 `--overlay` + `backdrop-filter: blur(4px)`
- 버튼 조합: 추가/수정 = secondary(취소) + primary(저장). 삭제 = secondary(취소) + **danger-solid**(삭제)
- 거래 추가 모달의 수입/지출 타입 토글: active-지출 = `--expense-bg` + `--expense` 보더/텍스트, active-수입 = `--income-bg` + `--income` — **teal 잔재 `rgba(0,191,165,*)` 제거 지점**
- **토스트**: 그라디언트 폐기 → `--card` 표면 + **좌측 3px 시맨틱 바**(ok=`--income`, err=`--expense`) + `--text` 텍스트 + `--shadow`. 다크에서 채움형보다 표면형이 계층에 맞음

### 5.7 테이블

- 헤더: `--muted` caption, sticky 배경 `--card`, 하단 1px `--border`
- 행 hover: `--surface-3` — 현재 `var(--bg)`는 다크에서 어두워지는 방향이라 **반전 필요**
- 금액 셀: tabular-nums, 수입 `--income` / 지출 `--expense`
- 행 액션: 수정 = ghost(`--accent`), 삭제 = danger-ghost
- 스크롤바: thumb `#2E3B5C`, track 투명

### 5.8 캘린더 히트맵 (지출 강도 i0–i4)

expense hue 단일색 램프, 카드→expense 보간 실측값. **불투명 solid로 교체**해 겹침 배경 의존 제거:

| 클래스 | 배경색 | 셀 내 텍스트 | 대비 |
|---|---|---|---|
| `.i0` | `#1B2437` (L3) | `--muted` | 지출 없음 = 표면 |
| `.i1` | `#3A2B3D` | `--text-2` | 10.5:1+ |
| `.i2` | `#693E4F` | `--text` | 8.00:1 |
| `.i3` | `#A65667` | `#F2F5FA` | 4.63:1 (AA) |
| `.i4` | `#E36F7F` | **`#0A0E17`** (어두운 텍스트로 반전) | 6.29:1 — 흰 텍스트는 3.07:1로 탈락 |

- 오늘 셀: `box-shadow: 0 0 0 2px var(--accent)` 유지
- `day-filter-badge`: `--expense-bg` 토큰으로

### 5.9 프로그레스 바 (향후 예산 기능 복원 대비)

트랙 `--surface-3`, 채움은 소진율 시맨틱: <80% `--income`, 80–100% `--warn`, >100% `--expense`. 높이 6px, `--radius-pill`. 채움 우측 끝에만 미세 글로우 허용(`0 0 8px` 동일색 30%).

### 5.10 사이드바 / 헤더 / 모바일 하단탭

- 사이드바: L1 표면, 우측 1px `--border`
- `.nav-item` 기본 `--muted` → hover `--accent-lt` 배경 + `--text-2` → **active** (그라디언트 폐기): `--accent-lt` 배경 + `--accent` 텍스트 + **좌측 3px `--accent` 인디케이터 바** (태블릿 64px 아이콘 모드에서도 활성 판별 가능)
- 로고 마크: **유일한 그라디언트 허용처** — `linear-gradient(135deg, #2CE0B4, #4C8DFF)`
- logout: danger-ghost 스펙 — `rgba(255,82,82,*)` 잔재 제거, hover `--expense-bg` + `--expense`
- top-header: L1 + 하단 1px `--border`, 선택적 `backdrop-filter: blur(8px)` + `rgba(15,21,34,.85)`
- 월 네비 버튼: secondary 미니 사양

### 5.11 로그인 화면 (teal 잔재 최다 구역)

- 배경: teal 그라디언트 → body와 동일한 `--bg` + 네온 글로우 2점 (§4)
- 카드: `#fff` → `--card`, radius 20px → `--radius-lg`, teal 그림자 → `--shadow`
- 아이콘 타일: `--accent-lt` 배경 + `--accent` 아이콘, `--radius-lg`
- 입력: §5.2 사양. focus ring `rgba(0,191,165,.15)` → `--focus-ring`
- 제출 버튼: primary 사양. 로딩 스피너: `--border` 트랙 + `--accent` 헤드

### 5.12 스켈레톤

shimmer 다크용: `linear-gradient(90deg, #1B2437 25%, #232E47 50%, #1B2437 75%)`. radius 하드코딩(5/3px) → `--radius-xs` 계열로 정리.

---

## 6. Chart.js 다크 팔레트

### 6.1 COLORS 15색 (카테고리)

dataviz 팔레트 검증기 4개 체크 전부 PASS (다크 표면 `#141C2E` 기준: 명도 밴드 OKLCH L 0.48–0.67 전원 통과 / 채도 플로어 통과 / 인접쌍 CVD 최악값 deutan ΔE 12.1 / 표면 대비 15색 전원 ≥ 3:1):

```js
const COLORS = ['#3E7DF0','#C4831E','#12A489','#E5527F','#8E7CE8',
                '#619C2C','#2596C8','#D96A3B','#B04FC4','#99901E',
                '#D14B4B','#4E6BE0','#2E9E4E','#C25A9E','#289FC4'];
```

- **순서 = 색약 안전장치**. 인접 슬롯 간 CVD 거리를 최대화한 배열이므로 순환·재정렬 금지. 카테고리는 항상 지출액 내림차순으로 슬롯 고정 소비.
- 네온이 아니라 한 단계 가라앉힌 톤인 이유: 다크 카드 위에서 15색이 공존하려면 명도 밴드가 좁아야 서로 구분되고 흰 라벨과 싸우지 않는다.
- UI accent `#2CE0B4`는 **의도적으로 배열에서 제외** — 데이터색/인터랙션색 분리 (§1.2 규칙 1).
- 기존 커스텀 HTML 범례(색 점 + 카테고리명)는 보조 인코딩이므로 **유지 필수**. 권장 개선: 9위 이하 카테고리 "기타" 접기.

### 6.2 CH (차트 크롬)

```js
const CH = {
  grid:    'rgba(138,150,173,.10)',   // 헤어라인 — 다크에서 그리드는 후퇴
  tick:    '#7D89A3',                 // 축 라벨 (카드 4.84:1)
  income:  'rgba(52,211,153,.80)',
  expense: 'rgba(255,122,138,.80)',
  net:     '#8AA8FF',                 // 라인은 불투명
};
```

### 6.3 Chart.js 전역 설정 (스크립트 초기화부 1회)

```js
Chart.defaults.color = '#8A96AD';
Chart.defaults.borderColor = 'rgba(138,150,173,.10)';
Chart.defaults.font.family = "'Pretendard Variable',Pretendard,'Segoe UI',sans-serif";
```

- 도넛: `borderColor: '#141C2E'`, `borderWidth: 2` — 조각 사이 2px "표면 갭" (흰 보더는 다크에서 와이어처럼 떠 보임)
- 툴팁: `backgroundColor:'#1B2437'`, `borderColor:'#232E47'`, `borderWidth:1`, `titleColor:'#F2F5FA'`, `bodyColor:'#A9B4C9'`
- 라인 차트 fill 알파 ≤ .12 (네온 라인 + 옅은 채움)

---

## 7. 반응형 — 다크 전환 차이점만

브레이크포인트 3단 구조는 그대로 유지. 달라지는 것:

| 항목 | 변경 |
|---|---|
| 모바일 하단탭 `.mob-nav` | 표면 `rgba(15,21,34,.92)` + `backdrop-filter: blur(12px)` + 상단 1px `--border`. active `--accent`. safe-area 패딩 기존 유지 |
| 드로어 오버레이 | `--overlay` — 라이트용 옅은 배리어는 다크 위에서 식별 불가 |
| body 그라디언트 | `background-attachment: fixed` 제거 (모바일 리페인트 비용) |
| `theme-color` meta | `#0A0E17` 추가 — iOS/Android 상태바 통일 |
| 태블릿 아이콘 사이드바 | active = 배경 틴트 + 좌측 3px 바 (§5.10) — 64px 폭에서도 판별 |
| KPI clamp | 상한만 조정: `clamp(.9rem, 4.2vw, 1.3rem)` |
| `@media(hover:none)` | 유지 + 글로우 hover 효과 무효화 추가 |
| OLED | 순수 `#000` 대신 `#0A0E17` 유지 — 스미어링 방지 + 표면 계층 확보 |

---

## 8. 기술 부채 해소 체크리스트

구현 시 전수 치환 대상 (라인 번호는 main 3,257줄 기준):

| 부채 | 위치 | 치환 |
|---|---|---|
| teal 잔재 `rgba(0,191,165,*)` | L697(타입 토글), L1115(로그인 그림자), L1145(로그인 focus) | `--focus-ring` / `--shadow` / 시맨틱 틴트 |
| red 잔재 `rgba(255,82,82,*)` | L303(logout), L696(타입 토글) | `--expense-bg` + `--expense` |
| 팔레트 외 hex `#F43F5E` | L765(삭제 버튼), L873(토스트 err) | `--danger-solid` / 토스트 표면형 |
| 팔레트 외 hex `#16A34A` | L871(토스트 ok) | 토스트 표면형 + `--income` 바 |
| 그라디언트 채움 | L249(nav active), L545(fbtn active), L711·L716(추가 모달), L765(삭제), L837(수정 저장), L1151(로그인) | §5.1 버튼 스킴 (로고 L209–211만 잔존) |
| 하드코딩 radius | 8px→`--radius-xs`, 10px→`--radius-sm`, 11px→`--radius-sm`, 16px→`--radius`, 18/20px→`--radius-lg` | 캘린더 셀 4–6px는 유지 가능 (주석으로 의도 명시) |
| 시맨틱 rgba 하드코딩 | L423, L591–597, L613, L644–646 (`rgba(34,197,94,*)`, `rgba(251,113,133,*)`) | `--income-bg` / `--expense-bg` / 히트맵 i1–i4 solid |
| 도넛 흰 보더 | L2342 `borderColor:'#FFFFFF'` | `#141C2E` (`--card` 값) |

**grep 검증 커맨드** (P2 완료 조건 — 로고 그라디언트 1건 외 0건이어야 함):

```bash
grep -c "linear-gradient" index.html    # 로고 1 + body 글로우만
grep -c "0,191,165" index.html          # 0
grep -c "255,82,82" index.html          # 0
grep -cE "#F43F5E|#16A34A" index.html   # 0
```

---

## 9. 구현 로드맵과 QA 시나리오

단일 파일이므로 단계별 커밋으로 시각 회귀 범위를 좁힌다:

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| **P0 준비** | 브랜치 생성, 현행 3뷰포트(1440/900/390px) 스크린샷 baseline | baseline 저장 |
| **P1 토큰 교체** | `:root` 치환 + 신규 토큰, body 배경, `color-scheme`/`theme-color`, 폰트 스택, 스켈레톤 | 화면 대부분 다크 전환 — "깨진 곳" = 부채 목록이 시각적으로 드러남 |
| **P2 부채 청소** | §8 전수 치환 | grep 체크리스트 통과 |
| **P3 컴포넌트** | 버튼 5종 → 입력/pill/배지 → 모달+토스트 → 테이블 → 히트맵 → 로그인 → 사이드바/하단탭 | §5 스펙과 일치 |
| **P4 차트** | COLORS/CH 교체, Chart.defaults, 도넛 보더, 툴팁 | §6 스펙과 일치 |
| **P5 검증** | 뷰포트 3종 Playwright 스크린샷 | 아래 시나리오 전부 통과 |

**QA 시나리오**:
1. 로그인 오버레이 (다크 배경 + 민트 primary)
2. 빈 데이터 월 (empty state 가독성)
3. 카테고리 15개 월 (도넛/범례 색 구분)
4. 7자리 금액 (tabular-nums 정렬, KPI 줄바꿈 없음)
5. 필터 + 검색 조합 (pill active 상태)
6. 모달 3종 열기/저장/삭제 (버튼 스킴 + backdrop blur)
7. 토스트 ok/err (표면형 + 시맨틱 바)
8. 360px 초소형 (스크롤·잘림 없음)
9. 대비 스팟체크: devtools contrast 검사 결과가 §2.4 표와 일치

**회귀 테스트**: `python -m pytest tests/` — UI 변경은 API 계약을 건드리지 않으므로 전부 통과해야 정상.

---

## 부록 A. 대비율 계산 기준

WCAG 2.1 상대휘도 공식 `(L1 + 0.05) / (L2 + 0.05)` 사용. §2.4, §5.8 표의 수치는 전부 실측 계산값이다. 구현 후 devtools의 Accessibility > Contrast 패널로 스팟체크 가능.

## 부록 B. 대안 B — 블루 액센트 파라미터

민트 대신 블루를 채택할 경우:

```css
--accent:   #4C8DFF;   /* 카드 위 5.31:1 — 채움 버튼 OK, 네온감은 약함 */
--accent-d: #2F6FE0;
--accent-lt: rgba(76,141,255,.12);
--net:      #A78BFA;   /* 필수: 잔액을 바이올렛으로 이동해 accent와 충돌 해소 */
--net-bg:   rgba(167,139,250,.12);
```

primary 버튼 텍스트는 `#FFFFFF`(4.62:1, AA)로 변경. 나머지 스펙은 본문과 동일.
