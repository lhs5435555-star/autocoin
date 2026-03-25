"""
fang_v10 전역 설정 모듈.

@dataclass 기반 단일 CONFIG 인스턴스.
API 키는 .env → os.environ.get()으로 읽으며, 키가 없으면 PAPER_TRADING=True 강제.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class FangConfig:
    """Bitget USDT-M 선물 자동매매 봇 설정."""

    # ── 모드 ──
    PAPER_TRADING: bool = True
    API_KEY: str = ""
    API_SECRET: str = ""
    PASSPHRASE: str = ""

    # ── 심볼 (고정) ──
    SYMBOLS: list = field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    TIMEFRAME_PRIMARY: str = "15m"
    TIMEFRAME_HTF: str = "1h"

    # ── 리스크 ──
    RISK_PER_TRADE_PCT: float = 1.0       # 1R = 잔고의 1%
    MAX_OPEN_POSITIONS: int = 2
    MAX_TOTAL_RISK_R: float = 1.5         # BTC+ETH 동시 보유 시 총 리스크 R 한도

    # ── DCA (1회만, 절대 2회 금지) ──
    DCA_MAX: int = 1
    DCA_ATR_MULT: float = 1.5             # DCA 진입 거리 (ATR 배수)
    DCA_SIZE_RATIO: float = 0.5           # DCA 수량 = 원래의 50%

    # ── TP (ATR R 기반 — ROE% 아님!) ──
    TP1_R: float = 1.0
    TP1_RATIO: float = 0.50               # TP1 도달 시 50% 청산
    TP2_R: float = 1.5
    TP2_RATIO: float = 0.30               # TP2 도달 시 30% 청산
    TP3_TRAIL_R: float = 0.5              # 트레일링 폭 (R 배수)

    # ── SL (넓게 → SL 선점 방지, 사이징 자동 축소) ──
    SL_ATR_MULT: Dict[str, float] = field(
        default_factory=lambda: {"BTC": 1.5, "ETH": 1.8}
    )
    SL_DIST_MIN: float = 0.0015           # 0.15% 미만 SL 거부

    # ── BE (손익분기 이동) ──
    BE_TRIGGER_R: float = 0.5

    # ── 레버리지 ──
    LEVERAGE_MAX: Dict[str, int] = field(
        default_factory=lambda: {"BTC": 20, "ETH": 15}
    )
    LEVERAGE_MIN: int = 5

    # ── 비용 ──
    TAKER_FEE: float = 0.0006
    TAKER_FEE_DISCOUNT: float = 1.0       # BGB 차감 시 0.8
    SLIPPAGE: Dict[str, float] = field(
        default_factory=lambda: {"BTC": 0.0002, "ETH": 0.0003}
    )
    FUNDING_RATE_PER_8H: float = 0.0001

    @property
    def EFFECTIVE_TAKER_FEE(self) -> float:
        """실효 테이커 수수료 (할인 적용)."""
        return self.TAKER_FEE * self.TAKER_FEE_DISCOUNT

    # ── 쿨다운 (3단계) ──
    COOLDOWN_NORMAL_SEC: int = 60          # 일반 쿨다운 60초
    COOLDOWN_AFTER_SL_SEC: int = 300       # SL 후 같은 심볼 5분(300초)
    COOLDOWN_AFTER_SL_SAME_DIR_SEC: int = 600  # SL+같은방향 10분(600초)

    # ── 킬스위치 ──
    DAILY_LOSS_SOFT: float = 1.0           # 일일 소프트 한도 (R)
    DAILY_LOSS_HARD: float = 1.5           # 일일 하드 한도 (R)
    DAILY_LOSS_STOP: float = 2.0           # 일일 정지 한도 (R)
    WEEKLY_MDD_LIMIT: float = 3.0          # 주간 MDD 한도 (R)
    MONTHLY_MDD_LIMIT: float = 5.0         # 월간 MDD 한도 (R)
    CONSEC_LOSS_LIMIT: int = 3             # 연속 손실 횟수 한도
    DAILY_RESET_HOUR_UTC: int = 15         # 한국 자정 = UTC 15시

    # ── 조기실패컷 ──
    EARLY_CUT_BARS: int = 8               # 8봉 = 40분 (5m 기준)
    EARLY_CUT_PEAK_R: float = 0.5         # peak_r < 0.5R이면 컷

    # ── SL 선점 추적 ──
    SL_PHANTOM_TRACK_BARS: int = 24
    BLOCKED_SIGNAL_TRACK_BARS: int = 24

    # ── 기타 ──
    MAIN_LOOP_SEC: int = 5
    MAX_HOLD_BARS: int = 500              # ~42시간 (5m 기준)
    DATA_DIR: str = ""

    def __post_init__(self) -> None:
        """환경변수 로드 및 PAPER_TRADING 강제 판단."""
        from dotenv import load_dotenv
        load_dotenv()

        self.API_KEY = os.environ.get("BITGET_API_KEY", "")
        self.API_SECRET = os.environ.get("BITGET_API_SECRET", "")
        self.PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

        # API 키 없으면 페이퍼 트레이딩 강제
        if not self.API_KEY or not self.API_SECRET or not self.PASSPHRASE:
            self.PAPER_TRADING = True
            logger.warning("API 키 미설정 → PAPER_TRADING=True 강제")

        # 영속 데이터 디렉토리
        if not self.DATA_DIR:
            self.DATA_DIR = str(Path.home() / ".fang_v10")
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)

        # user_config.json 오버라이드 시도
        self.sync_from_json()

    def sync_from_json(self) -> None:
        """user_config.json 에서 설정값 오버라이드.

        DATA_DIR/user_config.json 파일이 존재하면 읽어서
        현재 설정을 덮어쓴다. 없으면 무시.
        """
        config_path = Path(self.DATA_DIR) / "user_config.json"
        if not config_path.exists():
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                overrides: dict = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("user_config.json 파싱 실패: %s", e)
            return

        allowed_fields = {fld for fld in self.__dataclass_fields__}
        for key, value in overrides.items():
            if key not in allowed_fields:
                logger.warning("user_config.json 무시 키: %s", key)
                continue
            setattr(self, key, value)
            logger.info("user_config.json 오버라이드: %s=%s", key, value)

    def reload_env(self) -> None:
        """환경변수 재로드 (.env 파일 변경 후 호출)."""
        from dotenv import load_dotenv
        load_dotenv(override=True)
        self.API_KEY = os.environ.get("BITGET_API_KEY", "")
        self.API_SECRET = os.environ.get("BITGET_API_SECRET", "")
        self.PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
        if not self.API_KEY or not self.API_SECRET or not self.PASSPHRASE:
            self.PAPER_TRADING = True
            logger.warning("API 키 미설정 → PAPER_TRADING=True 강제")
        else:
            self.PAPER_TRADING = False
            logger.info("API 키 로드 완료 → LIVE 모드 가능")

    def sync_to_json(self) -> None:
        """현재 CONFIG 값을 user_config.json에 저장."""
        config_path = Path(self.DATA_DIR) / "user_config.json"
        skip = {"API_KEY", "API_SECRET", "PASSPHRASE", "DATA_DIR", "PAPER_TRADING"}
        data = {}
        for fld in self.__dataclass_fields__:
            if fld in skip:
                continue
            data[fld] = getattr(self, fld)
        config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("user_config.json 저장 완료")


# ── 전역 싱글턴 ──
CONFIG = FangConfig()
