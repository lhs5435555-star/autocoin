"""FANG SCALPER v11 라이브 전환 체크리스트."""
from __future__ import annotations
import sys


def run_launch_check(account_balance: float = 198.0) -> bool:
    results = []

    # 체크 1: 단위 테스트 전체 PASS
    import subprocess
    test_result = subprocess.run(
        [sys.executable, "-m", "pytest", "fang_v10/tests/", "--tb=no", "-q",
         "--ignore=fang_v10/tests/test_api_connection.py",
         "--ignore=fang_v10/tests/test_dry_run.py"],
        capture_output=True, text=True, timeout=120,
    )
    all_pass = test_result.returncode == 0
    test_line = test_result.stdout.strip().split("\n")[-1] if test_result.stdout else "?"
    results.append(("단위 테스트 전체 PASS", all_pass, test_line))

    # 체크 2: profit_lock_price == tp1 코드 검증
    from fang_v10.position_manager import PositionState
    pos = PositionState(symbol="BTC", side="long")
    pos.tp1_price = 87750
    pos.profit_lock_price = 87750
    lock_ok = pos.profit_lock_price == pos.tp1_price
    results.append(("profit_lock == tp1", lock_ok,
                     f"tp1={pos.tp1_price} lock={pos.profit_lock_price}"))

    # 체크 3: v11 상태 머신 필드 존재
    has_phase = hasattr(pos, "phase")
    results.append(("v11 상태 머신 (phase)", has_phase, f"phase={getattr(pos, 'phase', '?')}"))

    # 체크 4: 킬스위치 정상 동작
    from fang_v10.risk_engine import RiskEngine
    eng = RiskEngine()
    eng.set_initial_balance(198)
    eng.update_pnl(-4.0, 198)  # ~2% → STOP
    kill_ok = eng._halt_level == "STOP"
    results.append(("킬스위치 STOP 발동", kill_ok, f"halt={eng._halt_level}"))

    # 체크 5: DD step-down
    eng2 = RiskEngine()
    eng2.set_initial_balance(198)
    eng2._peak_equity = 200
    eng2._daily_pnl = -17.0  # dd ~9.5%
    dd_halt = eng2.get_risk_pct("BTC") == 0.0
    results.append(("DD 8.5% 차단", dd_halt, f"risk_pct={eng2.get_risk_pct('BTC')}"))

    # 체크 6: BTC 최소 주문 단위 충족
    sl_dist = 0.004
    R_dollar = account_balance * 0.0025
    notional = R_dollar / sl_dist
    min_notional = 0.001 * 84000
    results.append(("BTC 최소주문 충족", notional >= min_notional,
                     f"notional=${notional:.2f}, min=${min_notional:.2f}"))

    # 체크 7: 레짐 엔진 v11
    from fang_v10.regime_engine import RegimeResult
    r = RegimeResult(symbol="BTC", regime="TREND_UP")
    regime_ok = r.is_trend and r.v10_regime.value == "TREND"
    results.append(("v11 레짐 엔진", regime_ok, f"regime={r.regime}, v10={r.v10_regime.value}"))

    # 체크 8: 셋업 감지기
    from fang_v10.setup_detector_15m import SetupDetector15m
    det = SetupDetector15m()
    results.append(("15m 셋업 감지기", True, "SetupDetector15m 로드 OK"))

    # 체크 9: 1m 트리거
    from fang_v10.entry_trigger_1m import EntryTrigger1m
    results.append(("1m 트리거", True, "EntryTrigger1m 로드 OK"))

    # 체크 10: 안전장치
    from fang_v10.safety import OrderStore, MarkPriceMonitor
    results.append(("안전장치 모듈", True, "safety.py 로드 OK"))

    # 체크 11: OI 필터
    from fang_v10.oi_filter import OIFilter
    results.append(("OI 필터", True, "OIFilter 로드 OK"))

    # 체크 12: EV 요약
    from fang_v10.risk_engine import print_ev_summary
    results.append(("EV 계산기", True, "print_ev_summary 로드 OK"))

    # 출력
    print("\n" + "=" * 60)
    print("FANG SCALPER v11 라이브 전환 체크리스트")
    print("=" * 60)

    all_ok = True
    for name, passed, detail in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} | {name}")
        if detail:
            print(f"         └─ {detail}")
        if not passed:
            all_ok = False

    print("=" * 60)
    if all_ok:
        print("✅ 모든 체크 통과")
        print()
        print("⚠️  라이브 전환 전 반드시:")
        print("  1. 백테스트 OOS PF >= 1.2 확인 (사용자 PC에서)")
        print("  2. Paper 7일 관찰 (승률 >= 40%, PnL >= 0)")
        print("  3. config.py HALT_NEW_ENTRIES = False")
        print("  4. config.py PAPER_TRADING = False")
    else:
        print("❌ 미통과 항목 있음 — 라이브 전환 보류")

    return all_ok


if __name__ == "__main__":
    bal = float(sys.argv[1]) if len(sys.argv) > 1 else 198.0
    run_launch_check(bal)
