"""
2단계 그리드 탐색 기반 파라미터 최적화 엔진.

1단계: SL_BTC × SL_ETH × TP1_R (48조합)
2단계: 상위5개 × TP1_RATIO × BE_TRIGGER_R (15조합)
선택: EV>0 AND MDD<5% AND 거래수>=20 중 PF 최고.
"""
from __future__ import annotations

import json
import logging
from itertools import product
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)

# ── 탐색 공간 ──
SEARCH_SPACE = {
    "SL_ATR_MULT_BTC": [1.2, 1.5, 1.8, 2.1],
    "SL_ATR_MULT_ETH": [1.5, 1.8, 2.1, 2.4],
    "TP1_R": [0.8, 1.0, 1.2],
    "TP1_RATIO": [0.30, 0.40, 0.50],
    "BE_TRIGGER_R": [0.5, 0.8, 1.0],
}


class AutoOptimizer:
    """2단계 그리드 탐색 최적화."""

    def __init__(
        self,
        backtest_engine: Any = None,
        df: Any = None,
        symbol: str = "",
        initial_balance: float = 198.0,
    ) -> None:
        self.engine = backtest_engine
        self.df = df
        self.symbol = symbol
        self.balance = initial_balance
        self._result: Dict[str, Any] = {}

    def run_optimization(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """2단계 그리드 탐색 실행."""
        cb = progress_callback or (lambda p, m: None)

        if self.engine is None:
            from fang_v10.backtest import BacktestEngine
            self.engine = BacktestEngine(seed=42)

        # ══ 1단계: SL_BTC × SL_ETH × TP1_R ══
        stage1_combos = list(product(
            SEARCH_SPACE["SL_ATR_MULT_BTC"],
            SEARCH_SPACE["SL_ATR_MULT_ETH"],
            SEARCH_SPACE["TP1_R"],
        ))
        total_s1 = len(stage1_combos)
        cb(2, f"1단계: {total_s1}조합 탐색 시작...")

        stage1_results: List[Dict] = []
        for idx, (sl_btc, sl_eth, tp1_r) in enumerate(stage1_combos):
            pct = 5 + int(idx / total_s1 * 50)
            cb(pct, f"1단계 {idx+1}/{total_s1}: SL_BTC={sl_btc} SL_ETH={sl_eth} TP1_R={tp1_r}")

            params = {"SL_ATR_MULT_BTC": sl_btc, "SL_ATR_MULT_ETH": sl_eth, "TP1_R": tp1_r}
            result = self._run_with_overrides(params)
            if result is None:
                continue

            stage1_results.append({
                "params": params,
                "ev": result.avg_r,
                "pf": result.profit_factor,
                "mdd": result.max_drawdown,
                "winrate": result.winrate,
                "trades": result.total_trades,
                "result": result,
            })

        # 필터 + 정렬
        valid = [r for r in stage1_results
                 if r["ev"] > 0 and r["mdd"] < 0.05 and r["trades"] >= 20]
        if not valid:
            valid = sorted(stage1_results, key=lambda x: x["pf"], reverse=True)
        else:
            valid.sort(key=lambda x: x["pf"], reverse=True)

        top5 = valid[:5]
        cb(55, f"1단계 완료: 유효 {len(valid)}개, 상위 5개 선택")

        # ══ 2단계: 상위5 × TP1_RATIO × BE_TRIGGER_R ══
        stage2_combos = list(product(
            range(len(top5)),
            SEARCH_SPACE["TP1_RATIO"],
            SEARCH_SPACE["BE_TRIGGER_R"],
        ))
        total_s2 = len(stage2_combos)
        cb(57, f"2단계: {total_s2}조합 탐색 시작...")

        all_results: List[Dict] = []
        for idx, (top_idx, tp1_ratio, be_r) in enumerate(stage2_combos):
            pct = 58 + int(idx / total_s2 * 35)
            cb(pct, f"2단계 {idx+1}/{total_s2}")

            base_params = dict(top5[top_idx]["params"])
            base_params["TP1_RATIO"] = tp1_ratio
            base_params["BE_TRIGGER_R"] = be_r

            result = self._run_with_overrides(base_params)
            if result is None:
                continue

            all_results.append({
                "params": base_params,
                "ev": result.avg_r,
                "pf": result.profit_factor,
                "mdd": result.max_drawdown,
                "winrate": result.winrate,
                "trades": result.total_trades,
                "result": result,
            })

        # 최종 선택: EV>0 AND MDD<5% AND 거래수>=20 중 PF 최고
        final_valid = [r for r in all_results
                       if r["ev"] > 0 and r["mdd"] < 0.05 and r["trades"] >= 20]
        if not final_valid:
            final_valid = sorted(all_results, key=lambda x: x["pf"], reverse=True)
        else:
            final_valid.sort(key=lambda x: x["pf"], reverse=True)

        best = final_valid[0] if final_valid else None

        cb(95, "최종 결과 정리중...")

        # baseline (현재 설정)
        baseline = self._run_with_overrides({})

        self._result = {
            "best_params": best["params"] if best else {},
            "best_ev": best["ev"] if best else 0,
            "best_pf": best["pf"] if best else 0,
            "best_winrate": best["winrate"] if best else 0,
            "best_mdd": best["mdd"] if best else 0,
            "best_result": best["result"] if best else None,
            "baseline_result": baseline,
            "all_results": all_results,
            "stage1_count": len(stage1_results),
            "stage2_count": len(all_results),
        }

        cb(100, f"완료: 최적 PF={best['pf']:.2f} EV={best['ev']:+.3f}" if best else "완료: 유효 조합 없음")
        return self._result

    def _run_with_overrides(self, overrides: Dict[str, Any]) -> Any:
        """파라미터 임시 변경 → 백테스트 → 원복."""
        originals: Dict[str, Any] = {}

        for param, value in overrides.items():
            if param.startswith("SL_ATR_MULT_"):
                asset = param.split("_")[-1]
                originals[param] = CONFIG.SL_ATR_MULT.get(asset)
                CONFIG.SL_ATR_MULT[asset] = value
            else:
                originals[param] = getattr(CONFIG, param, None)
                setattr(CONFIG, param, value)

        try:
            from fang_v10.backtest import BacktestEngine
            engine = BacktestEngine(seed=42)
            result = engine.run(self.df, self.symbol, self.balance)
        except Exception as e:
            logger.error("백테스트 실패: %s (params=%s)", e, overrides)
            result = None
        finally:
            for param, orig in originals.items():
                if param.startswith("SL_ATR_MULT_"):
                    asset = param.split("_")[-1]
                    if orig is not None:
                        CONFIG.SL_ATR_MULT[asset] = orig
                elif orig is not None:
                    setattr(CONFIG, param, orig)

        return result

    def apply_best(self, result: Optional[Dict[str, Any]] = None) -> None:
        """최적 파라미터를 CONFIG + user_config.json에 저장."""
        res = result or self._result
        best_params = res.get("best_params", {})
        if not best_params:
            logger.warning("적용할 최적 파라미터 없음")
            return

        config_path = Path(CONFIG.DATA_DIR) / "user_config.json"
        existing: Dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        for key, val in best_params.items():
            if key.startswith("SL_ATR_MULT_"):
                asset = key.split("_")[-1]
                sl_dict = existing.get("SL_ATR_MULT", dict(CONFIG.SL_ATR_MULT))
                sl_dict[asset] = val
                existing["SL_ATR_MULT"] = sl_dict
                CONFIG.SL_ATR_MULT[asset] = val
            else:
                existing[key] = val
                setattr(CONFIG, key, val)

        config_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        CONFIG.sync_from_json()
        logger.info("최적 파라미터 적용: %s", best_params)

    def get_summary(self) -> str:
        """사람이 읽을 수 있는 최적화 요약."""
        if not self._result:
            return "최적화 미실행"
        r = self._result
        bp = r.get("best_params", {})
        if not bp:
            return "유효한 최적 조합 없음"
        return (
            f"1단계 {r['stage1_count']}조합 → 2단계 {r['stage2_count']}조합\n"
            f"최적: PF={r['best_pf']:.2f}, EV={r['best_ev']:+.3f}R, "
            f"승률={r['best_winrate']*100:.1f}%, MDD={r['best_mdd']*100:.1f}%\n"
            f"파라미터: {bp}"
        )
