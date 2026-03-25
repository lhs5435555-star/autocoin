"""
경보선 기반 자동 파라미터 최적화 엔진.

Optuna 같은 블랙박스 탐색이 아니라,
측정된 문제에 대해 미리 정의된 처방을 A/B 비교하는 방식.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


class AutoOptimizer:
    """경보선 기반 자동 파라미터 최적화."""

    def __init__(
        self,
        backtest_engine: Any,
        df: Any,
        symbol: str,
        initial_balance: float = 198.0,
    ) -> None:
        self.engine = backtest_engine
        self.df = df
        self.symbol = symbol
        self.balance = initial_balance
        self._baseline = None
        self._result: Dict[str, Any] = {}

    # ════════════════════════════════════════
    # 메인 최적화 루프
    # ════════════════════════════════════════

    def run_optimization(
        self, progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        1) baseline 백테스트
        2) 경보선 체크 → 후보 생성
        3) 각 후보 개별 백테스트 → EV 비교
        4) EV 개선된 변경만 채택
        """
        cb = progress_callback or (lambda p, m: None)

        # 1단계: baseline
        cb(5, "Baseline 백테스트 실행 중...")
        self._baseline = self.engine.run(self.df, self.symbol, self.balance)
        baseline_ev = self._baseline.avg_r
        cb(20, f"Baseline 완료: EV={baseline_ev:+.3f}R")

        # 2단계: 경보선 체크
        cb(25, "경보선 분석 중...")
        candidates = self._check_alerts(self._baseline)
        if not candidates:
            cb(100, "경보선 미해당 — 현재 설정 유지")
            return {
                "baseline": self._baseline,
                "optimized": self._baseline,
                "changes": [],
                "final_config": {},
                "rejected": [],
            }

        cb(30, f"후보 {len(candidates)}개 발견, A/B 테스트 시작...")

        # 3단계: 각 후보 개별 테스트
        accepted: List[Dict] = []
        rejected: List[Dict] = []
        step = 60 // max(len(candidates), 1)

        for i, cand in enumerate(candidates):
            pct = 30 + (i + 1) * step
            param = cand["param"]
            cb(pct, f"테스트 중: {param} = {cand['candidate']}")

            try:
                test_result = self._run_with_override(param, cand["candidate"])
                ev_diff = test_result.avg_r - baseline_ev

                cand["ev_diff"] = round(ev_diff, 4)
                cand["test_ev"] = round(test_result.avg_r, 4)

                if ev_diff > 0:
                    cand["before"] = cand["current"]
                    cand["after"] = cand["candidate"]
                    accepted.append(cand)
                    logger.info(
                        "ACCEPT %s: %s→%s (EV %+.4f)",
                        param, cand["current"], cand["candidate"], ev_diff,
                    )
                else:
                    cand["reject_reason"] = f"EV 악화 ({ev_diff:+.4f})"
                    rejected.append(cand)
                    logger.info(
                        "REJECT %s: EV %+.4f", param, ev_diff,
                    )
            except Exception as e:
                cand["reject_reason"] = f"오류: {e}"
                rejected.append(cand)
                logger.error("후보 테스트 실패 %s: %s", param, e)

        # 4단계: 채택된 변경 모두 적용 후 최종 백테스트
        final_config: Dict[str, Any] = {}
        for ch in accepted:
            final_config[ch["param"]] = ch["after"]

        optimized = self._baseline
        if accepted:
            cb(90, "최종 조합 백테스트...")
            try:
                optimized = self._run_with_overrides(final_config)
            except Exception:
                optimized = self._baseline

        cb(100, f"완료: {len(accepted)}개 개선, {len(rejected)}개 기각")

        self._result = {
            "baseline": self._baseline,
            "optimized": optimized,
            "changes": accepted,
            "final_config": final_config,
            "rejected": rejected,
        }
        return self._result

    # ════════════════════════════════════════
    # 경보선 → 처방 매핑
    # ════════════════════════════════════════

    def _check_alerts(self, result: Any) -> List[Dict]:
        candidates = []

        # BE이탈률 > 40% → BE 0.5 → 0.8
        if result.be_exit_rate > 0.40:
            candidates.append({
                "param": "BE_TRIGGER_R",
                "current": CONFIG.BE_TRIGGER_R,
                "candidate": 0.8,
                "reason": f"BE이탈률 {result.be_exit_rate * 100:.1f}% > 40%",
            })

        # TP연장률 > 60% → TP1 비율 50% → 35%
        if result.tp_extension_rate > 0.60:
            candidates.append({
                "param": "TP1_RATIO",
                "current": CONFIG.TP1_RATIO,
                "candidate": 0.35,
                "reason": f"TP연장률 {result.tp_extension_rate * 100:.1f}% > 60%",
            })

        # SL 선점률 > 40% → SL배수 +0.3
        phantom = result.sl_phantom_stats or {}
        if phantom.get("premature_pct", 0) > 40:
            asset = "BTC" if "BTC" in self.symbol else "ETH"
            current = CONFIG.SL_ATR_MULT.get(asset, 1.5)
            candidates.append({
                "param": f"SL_ATR_MULT_{asset}",
                "current": current,
                "candidate": round(current + 0.3, 1),
                "reason": f"SL선점률 {phantom['premature_pct']:.1f}% > 40%",
            })

        # 차단 정확도 < 30% → 쿨다운 완화
        blocked = result.blocked_signal_stats or {}
        if blocked.get("total", 0) > 10 and blocked.get("correct_block_pct", 100) < 30:
            candidates.append({
                "param": "COOLDOWN_AFTER_SL_SEC",
                "current": CONFIG.COOLDOWN_AFTER_SL_SEC,
                "candidate": 180,
                "reason": f"차단정확도 {blocked['correct_block_pct']:.1f}% < 30%",
            })

        # BOX 거래 < 10건 (volume 완화 — CONFIG에 없으면 스킵)
        box_trades = (result.regime_stats or {}).get("BOX", {}).get("trades", 0)
        if box_trades < 10 and hasattr(CONFIG, "BOX_VOLUME_MIN"):
            candidates.append({
                "param": "BOX_VOLUME_MIN",
                "current": getattr(CONFIG, "BOX_VOLUME_MIN", 1.2),
                "candidate": 1.0,
                "reason": f"BOX 거래 {box_trades}건 < 10",
            })

        return candidates

    # ════════════════════════════════════════
    # 파라미터 오버라이드 백테스트
    # ════════════════════════════════════════

    def _run_with_override(self, param: str, value: Any) -> Any:
        return self._run_with_overrides({param: value})

    def _run_with_overrides(self, overrides: Dict[str, Any]) -> Any:
        """파라미터 임시 변경 → 백테스트 → 원복."""
        originals: Dict[str, Any] = {}

        for param, value in overrides.items():
            # SL_ATR_MULT_BTC / SL_ATR_MULT_ETH 특별 처리
            if param.startswith("SL_ATR_MULT_"):
                asset = param.split("_")[-1]
                originals[param] = CONFIG.SL_ATR_MULT.get(asset)
                CONFIG.SL_ATR_MULT[asset] = value
            else:
                originals[param] = getattr(CONFIG, param, None)
                setattr(CONFIG, param, value)

        try:
            # 새 엔진 인스턴스 (RiskEngine 상태 격리)
            from fang_v10.backtest import BacktestEngine
            engine = BacktestEngine(seed=42)
            result = engine.run(self.df, self.symbol, self.balance)
        finally:
            # 원복
            for param, orig in originals.items():
                if param.startswith("SL_ATR_MULT_"):
                    asset = param.split("_")[-1]
                    if orig is not None:
                        CONFIG.SL_ATR_MULT[asset] = orig
                elif orig is not None:
                    setattr(CONFIG, param, orig)

        return result

    # ════════════════════════════════════════
    # 결과 적용 / 요약
    # ════════════════════════════════════════

    def apply_to_config(self, final_config: Dict[str, Any]) -> None:
        """최적화 결과를 user_config.json에 저장 + CONFIG hot reload."""
        config_path = Path(CONFIG.DATA_DIR) / "user_config.json"

        # 기존 로드
        existing: Dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # SL_ATR_MULT 특별 처리: dict merge
        for key, val in final_config.items():
            if key.startswith("SL_ATR_MULT_"):
                asset = key.split("_")[-1]
                sl_dict = existing.get("SL_ATR_MULT", dict(CONFIG.SL_ATR_MULT))
                sl_dict[asset] = val
                existing["SL_ATR_MULT"] = sl_dict
            else:
                existing[key] = val

        config_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        CONFIG.sync_from_json()
        logger.info("최적화 결과 저장: %s", final_config)

    def get_summary(self) -> str:
        """사람이 읽을 수 있는 최적화 요약."""
        if not self._result:
            return "최적화 미실행"

        bl = self._result["baseline"]
        opt = self._result["optimized"]
        changes = self._result["changes"]
        rejected = self._result["rejected"]

        lines = [
            f"[Baseline] 거래 {bl.total_trades}건, 승률 {bl.winrate*100:.1f}%, "
            f"EV {bl.avg_r:+.3f}R, PF {bl.profit_factor:.2f}",
            f"[Optimized] 거래 {opt.total_trades}건, 승률 {opt.winrate*100:.1f}%, "
            f"EV {opt.avg_r:+.3f}R, PF {opt.profit_factor:.2f}",
            "",
        ]

        if changes:
            lines.append(f"채택 ({len(changes)}건):")
            for ch in changes:
                lines.append(
                    f"  {ch['param']}: {ch['before']} → {ch['after']} "
                    f"(EV {ch['ev_diff']:+.4f}) [{ch['reason']}]"
                )
        else:
            lines.append("채택: 없음 (현재 설정 최적)")

        if rejected:
            lines.append(f"기각 ({len(rejected)}건):")
            for rj in rejected:
                lines.append(
                    f"  {rj['param']}: {rj.get('reject_reason', '?')} [{rj['reason']}]"
                )

        return "\n".join(lines)
