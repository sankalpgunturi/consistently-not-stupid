"""Environment seed. Learned knobs live in the book after the first run."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from cst.models import StrategyParams


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CST_", env_file=".env", extra="ignore")

    bankroll: Decimal = Decimal("1000")
    data_dir: str = "data"
    host: str = "127.0.0.1"
    port: int = 8000

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_pages: int = 8
    kalshi_page_size: int = 200

    entry_window_minutes: float = 10
    min_probability: float = 0.90
    min_win_profit: float = 0.01
    min_edge: float = 0.01
    max_spread: float = 0.03
    max_days_to_expiry: float = 21
    min_hours_to_expiry: float = 2
    max_position_fraction: float = 0.008
    max_deployed_fraction: float = 0.40
    max_category_fraction: float = 0.15
    max_drawdown: float = 0.05
    stop_gap: float = 0.08
    scan_interval_seconds: int = 1
    mark_interval_seconds: int = 1
    min_stable_scans: int = 2
    correlation_threshold: float = 0.48
    min_sample: int = 30
    max_new_per_cycle: int = 10

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""

    def seed_params(self) -> StrategyParams:
        return StrategyParams(
            entry_window_minutes=self.entry_window_minutes,
            min_probability=self.min_probability,
            min_win_profit=self.min_win_profit,
            min_edge=self.min_edge,
            max_spread=self.max_spread,
            max_days_to_expiry=self.max_days_to_expiry,
            min_hours_to_expiry=self.min_hours_to_expiry,
            max_position_fraction=self.max_position_fraction,
            max_deployed_fraction=self.max_deployed_fraction,
            max_category_fraction=self.max_category_fraction,
            max_drawdown=self.max_drawdown,
            stop_gap=self.stop_gap,
            scan_interval_seconds=self.scan_interval_seconds,
            mark_interval_seconds=self.mark_interval_seconds,
            min_stable_scans=self.min_stable_scans,
            correlation_threshold=self.correlation_threshold,
            min_sample=self.min_sample,
            max_new_per_cycle=self.max_new_per_cycle,
        )

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "book.sqlite"


def load_settings() -> Settings:
    return Settings()
