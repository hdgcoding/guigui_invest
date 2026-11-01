"""Configuration for Turtle Screener (龟龟选股器).

Defines ScreenerConfig dataclass with all tunable thresholds, scoring weights,
and cache settings for Tier 1 / Tier 2 screening pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ScreenerConfig:
    """All tunable parameters for the screener pipeline.

    Tier 1 filters (market data only, applied to full A-share universe):
    - Excludes ST/PT/退市整理 stocks
    - Requires listing age >= min_listing_years
    - Requires market cap >= min_market_cap_yi (亿元)
    - Requires daily turnover amount >= min_daily_amount_yuan (liquidity proxy)
    - Requires 0 < PB <= max_pb

    Tier 1 PE basis (dual channel):
    - PE = TTM PE when the bulk source supplies it, else static PE
      (close / latest-annual EPS); the basis is exported per row as ``pe_basis``.
    - Main channel: 0 < PE <= max_pe and dividend yield > 0
    - Observation channel: loss-making (EPS < 0, no positive PE), top
      obs_channel_limit by market cap; dividend yield is not required.

    Tier 1 ranking:
    - score = dv_weight * dv_ttm + pe_weight * (1/PE) + pb_weight * (1/PB)
      (each component normalized to [0, 1] first)
    - Main channel takes top tier2_main_limit
    - Total into Tier 2: tier2_main_limit + obs_channel_limit

    Tier 2 hard vetoes:
    - Pledge ratio (未解押 preferred) > max_pledge_pct → reject
    - Audit opinion / pledge detail unavailable → flagged as unverified, never
      silently treated as a pass

    Tier 2 financial quality:
    - ROE(weighted) >= min_roe (%)
    - Gross margin >= min_gross_margin (%)
    - Debt-to-assets <= max_debt_ratio (%)

    Scoring weights (four factors + optional penetration return R):
    - ROE, FCF yield, EV/EBITDA (inverse), floor premium (inverse) are always
      scored; weight_penetration_r is optional and only participates when R is
      a verified metric (not the consolidated-cash proxy). The active weights
      are renormalized over the factors that actually participate, so the
      composite never silently loses 25% of its range when R is unavailable.
    - Validation enforces: four-factor weights sum to 1.0 when the R weight is
      0, otherwise all five weights sum to 1.0.

    Cache TTLs (all fields are wired; see ``cache_ttl_seconds``):
    - stock basic: listing dates / share-capital style data
    - daily basic: full-market spot snapshot (days; 0 disables the cache)
    - rf: risk-free rate
    - tier2: per-stock Tier 2 auxiliary data (e.g. pledge detail)
    - tier2 financial: statements / indicators / dividends
    - tier2 market: weekly & daily prices / valuation snapshots
    - tier2 global: market-wide bulk reports (业绩报表 / 分红送配)
    """

    # --- Tier 1: Hard filters ---
    min_listing_years: int = 3
    min_market_cap_yi: float = 5.0  # 亿元
    min_turnover_pct: float = 0.1   # deprecated; Sina does not expose turnover rate
    min_daily_amount_yuan: float = 1e7  # explicit liquidity proxy, not turnover %
    max_pb: float = 10.0
    max_pe: float = 50.0
    include_bank: bool = False  # 是否包含银行股

    # --- Tier 1: Dual-channel PE ---
    obs_channel_limit: int = 50

    # --- Tier 1: Ranking & cutoff ---
    tier2_main_limit: int = 150
    dv_weight: float = 0.4
    pe_weight: float = 0.3
    pb_weight: float = 0.3

    # --- Tier 2: Hard vetoes ---
    max_pledge_pct: float = 70.0

    # --- Tier 2: Financial quality ---
    min_roe: float = 8.0             # %
    min_gross_margin: float = 15.0   # %
    max_debt_ratio: float = 70.0     # %

    # --- Tier 2: Observation channel quality (relaxed vs main) ---
    min_roe_obs: float | None = None        # loss observation is not main-channel ROE qualification
    min_fcf_margin_obs: float = 0.0         # FCF/Revenue >= 0%
    min_fcf_positive_years_obs: int = 2     # FCF positive in >= 2 of 5 years
    obs_require_ocf_positive: bool = True   # Latest year OCF > 0

    # --- Scoring weights (four factors + optional penetration R) ---
    weight_roe: float = 0.20
    weight_fcf_yield: float = 0.20
    weight_penetration_r: float = 0.25   # optional: only used when R is verified
    weight_ev_ebitda: float = 0.15
    weight_floor_premium: float = 0.20

    # --- Cache (all wired; see cache_ttl_seconds) ---
    cache_dir: str = "output/.screener_cache"
    cache_stock_basic_ttl_days: int = 7
    cache_daily_basic_ttl_days: float = 0.25  # 6小时（当日行情）；0 = 不缓存
    cache_rf_ttl_days: int = 1
    cache_tier2_ttl_hours: int = 24
    cache_tier2_financial_ttl_hours: int = 168  # 7天，年报类数据
    cache_tier2_market_ttl_hours: int = 24      # 1天，周线行情
    cache_tier2_global_ttl_hours: int = 24      # 1天，全市场批量报表

    @property
    def tier2_max_stocks(self) -> int:
        """Total stocks entering Tier 2 = main + observation."""
        return self.tier2_main_limit + self.obs_channel_limit

    @property
    def scoring_weights(self) -> dict[str, float]:
        """Raw (un-renormalized) scoring weights as a dict."""
        return {
            "roe": self.weight_roe,
            "fcf_yield": self.weight_fcf_yield,
            "penetration_r": self.weight_penetration_r,
            "ev_ebitda": self.weight_ev_ebitda,
            "floor_premium": self.weight_floor_premium,
        }

    def renormalized_scoring_weights(self, include_penetration_r: bool) -> dict[str, float]:
        """Active factor weights, renormalized to sum to 1.0.

        The four always-available factors are kept; ``penetration_r`` is added
        only when the caller has a verified R (and the configured weight is
        positive). This keeps the composite on a full [0, 1] scale instead of
        capping it at the four-factor subtotal when R is unavailable.
        """
        weights = {
            "roe": self.weight_roe,
            "fcf_yield": self.weight_fcf_yield,
            "ev_ebitda": self.weight_ev_ebitda,
            "floor_premium": self.weight_floor_premium,
        }
        if include_penetration_r and self.weight_penetration_r > 0:
            weights["penetration_r"] = self.weight_penetration_r
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("no scoring factor has a positive weight")
        return {name: value / total for name, value in weights.items()}

    @property
    def cache_ttl_seconds(self) -> dict[str, int]:
        """TTL (seconds) for every logical data group used by the screener.

        Single source of truth so no cache call site hard-codes 6h/24h/7d.
        """
        return {
            "spot": int(self.cache_daily_basic_ttl_days * 24 * 3600),
            "stock_basic": int(self.cache_stock_basic_ttl_days * 24 * 3600),
            "rf": int(self.cache_rf_ttl_days * 24 * 3600),
            "tier2": int(self.cache_tier2_ttl_hours * 3600),
            "financial": int(self.cache_tier2_financial_ttl_hours * 3600),
            "market": int(self.cache_tier2_market_ttl_hours * 3600),
            "global": int(self.cache_tier2_global_ttl_hours * 3600),
        }

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of error messages (empty = OK)."""
        errors: list[str] = []

        weights = {"weight_roe": self.weight_roe,
                   "weight_fcf_yield": self.weight_fcf_yield,
                   "weight_penetration_r": self.weight_penetration_r,
                   "weight_ev_ebitda": self.weight_ev_ebitda,
                   "weight_floor_premium": self.weight_floor_premium}
        for name, value in weights.items():
            if value < 0:
                errors.append(f"{name} must be >= 0, got {value}")
        core_sum = (self.weight_roe + self.weight_fcf_yield
                    + self.weight_ev_ebitda + self.weight_floor_premium)
        if self.weight_penetration_r > 0:
            total = core_sum + self.weight_penetration_r
            if abs(total - 1.0) > 0.01:
                errors.append(
                    f"Scoring weights must sum to 1.0 (four-factor + optional R), got {total:.3f}")
            if core_sum <= 0:
                errors.append("four-factor weights must be > 0 (R alone cannot be scored)")
        elif abs(core_sum - 1.0) > 0.01:
            errors.append(
                f"Scoring weights must sum to 1.0 (four-factor, R weight 0), got {core_sum:.3f}")

        tier1_sum = self.dv_weight + self.pe_weight + self.pb_weight
        if min(self.dv_weight, self.pe_weight, self.pb_weight) < 0:
            errors.append("Tier 1 ranking weights must be >= 0")
        if abs(tier1_sum - 1.0) > 0.01:
            errors.append(f"Tier 1 ranking weights must sum to 1.0, got {tier1_sum:.3f}")

        if self.min_listing_years < 0:
            errors.append("min_listing_years must be >= 0")
        if self.min_market_cap_yi < 0:
            errors.append("min_market_cap_yi must be >= 0")
        if self.min_daily_amount_yuan < 0:
            errors.append("min_daily_amount_yuan must be >= 0")
        if self.min_turnover_pct < 0:
            errors.append("min_turnover_pct must be >= 0")
        if self.max_pb <= 0:
            errors.append("max_pb must be > 0")
        if self.max_pe <= 0:
            errors.append("max_pe must be > 0")
        if not 0 <= self.max_pledge_pct <= 100:
            errors.append("max_pledge_pct must be 0-100")
        if not 0 <= self.min_gross_margin <= 100:
            errors.append("min_gross_margin must be 0-100")
        if not 0 <= self.max_debt_ratio <= 100:
            errors.append("max_debt_ratio must be 0-100")
        if not -100 <= self.min_roe <= 100:
            errors.append("min_roe must be -100 to 100")
        if self.min_roe_obs is not None and not -100 <= self.min_roe_obs <= 100:
            errors.append("min_roe_obs must be -100 to 100")
        if self.tier2_main_limit < 1:
            errors.append("tier2_main_limit must be >= 1")
        if self.obs_channel_limit < 0:
            errors.append("obs_channel_limit must be >= 0")
        if self.min_fcf_positive_years_obs < 0 or self.min_fcf_positive_years_obs > 5:
            errors.append("min_fcf_positive_years_obs must be 0-5")

        ttl_values = {"cache_stock_basic_ttl_days": self.cache_stock_basic_ttl_days,
                      "cache_daily_basic_ttl_days": self.cache_daily_basic_ttl_days,
                      "cache_rf_ttl_days": self.cache_rf_ttl_days,
                      "cache_tier2_ttl_hours": self.cache_tier2_ttl_hours,
                      "cache_tier2_financial_ttl_hours": self.cache_tier2_financial_ttl_hours,
                      "cache_tier2_market_ttl_hours": self.cache_tier2_market_ttl_hours,
                      "cache_tier2_global_ttl_hours": self.cache_tier2_global_ttl_hours}
        for name, value in ttl_values.items():
            if value < 0:
                errors.append(f"{name} must be >= 0")
        if not str(self.cache_dir).strip():
            errors.append("cache_dir must be non-empty")

        return errors

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for display/serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScreenerConfig":
        """Create from dict, ignoring unknown keys."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)
