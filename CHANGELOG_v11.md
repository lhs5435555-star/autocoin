# FANG SCALPER v11.0 CHANGELOG

## v10 → v11 주요 변경

### STEP 1: 진단 + 라이브 차단
- HALT_NEW_ENTRIES 플래그로 v11 전환 중 라이브 진입 차단
- 기존 포지션 청산은 정상 유지

### STEP 2: 1H 레짐 엔진 재작성
- 4 레짐: TREND_UP / TREND_DOWN / BOX / NO_TRADE
- 1H 완성봉만 사용 (5m/15m 노이즈 차단)
- EMA20/EMA50 + slope + ADX18 + DI 방향
- 캐시: 1H 봉 마감 시에만 재계산
- 15m→1H 리샘플 함수 (백테스트 호환)

### STEP 3: 15m 눌림목 Setup 감지
- 7+1 조건 AND 필터 (EMA, ADX, DI, EMA gap, Pullback Depth, EMA 접촉, 종가 회복, OI)
- Pullback Depth 0.33~0.75 (추격 진입 + 추세 붕괴 차단)
- SL: min(7봉 저가) - ATR*0.3, 거리 0.15%~2.0% 검증
- TP: R x 1.5 / R x 2.5 / R x 4.0
- profit_lock_price == tp1 (반드시 동일)

### STEP 4: 1m 체결 트리거
- 6 전제조건: 셋업 존재/미사용/미만료/Funding Veto/Mark-Last 괴리/SL 버퍼
- 3 트리거: 양봉(음봉) + 미세돌파 + 거래량 > MA20 x 1.2
- SL은 반드시 15m 기준 (1m SL = fee_R 0.92R → 금지)

### STEP 5: TP/SL/수익보호 재설계
- 5단계 상태 머신: OPEN → TP1_HIT → TP2_HIT → TRAIL_ONLY → CLOSED
- TP1: 40% 청산, 새 SL = entry + fee_buffer (0.20%)
- TP2: 35% 청산
- TRAIL: EMA20 + 구조 low 기반, 유리 방향만 이동
- profit_lock 발동 = TP1 (절대 TP1보다 먼저 발동 안 함)

### STEP 6: 리스크 엔진 재설계
- DD step-down: <3% 풀 / <5% 75% / <7% 50% / >=8.5% 차단
- 포지션 사이징: adj_sl = SL + fee(0.12%) + slip(0.08%)
- DCA: 70/30 예산 분할, 미실현R -0.75~-0.35 윈도우
- 5단계 킬스위치: Daily SOFT/HARD/STOP + Weekly + Monthly
- BTC+ETH 상관 리스크 캡: 0.55%
- EV 자동 계산: 손익분기 승률 37.5%

### STEP 7: OI 필터 + Funding 시간
- OI: 5m EMA6/EMA24 크로스 + 3봉 delta (라이브 전용)
- Funding Veto: 펀딩 10분 전 진입 차단
- 백테스트에서 OI 자동 비활성 (히스토리 미제공)

### STEP 8: 백테스트-라이브 일치
- check_exit() 상태 머신이 백테스트의 단일 진실 소스
- 펀딩비 PnL 차감 추가
- 진입 시 v11 TP/SL/phase 자동 설정

### STEP 9: 안전장치 강화
- 서버 SL 복원 (봇 크래시 대비)
- 주문 Idempotency (client_oid 기반 중복 방지)
- 재시작 State Reconciliation
- Mark Price 실시간 감시 (0.3% 경고, 0.8% CRITICAL)

### STEP 10: 통합 테스트 + 라이브 전환
- 122 테스트 전체 PASS
- launch_check.py: 12항목 자동 검증
- Paper Trading → OOS 합격 → 라이브 전환 순서

## 알려진 제한 사항
- OI 히스토리 미제공 (Bitget) → 백테스트에서 OI 필터 미반영
- 1m 트리거는 백테스트에 미반영 (15m 진입으로 대체)
- Mark price는 라이브에서만 사용 (백테스트는 close price)
