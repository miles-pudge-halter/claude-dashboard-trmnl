"""Claude pricing + cost calculation, ported from steipete/codexbar.

Source: Sources/CodexBarCore/Vendored/CostUsage/CostUsagePricing.swift
        (the `claudeCostUSD` static method and the `claude` model dictionary)

Two meaningful differences vs upstream session_stats:
  1. No "fast mode" 6× cost multiplier. session_stats checks
     `usage.speed == "fast"` for opus-4-6 and applies a separate `opus_fast`
     tier; codex bar uses the same per-token rate regardless of speed.
  2. Sonnet 4-5/4-6 have a 200_000-token threshold above which the cost
     doubles. session_stats treats sonnet flat. The threshold-tier math is
     why the dataclass carries `*_above_threshold` fields.

Costs are returned in USD. The Anthropic OAuth API reports billed amounts in
the account's currency; the consistent USD-equivalent figure here is what
Codex Bar shows in its menus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ClaudePricing:
    input_cost_per_token: float
    output_cost_per_token: float
    cache_creation_input_cost_per_token: float
    cache_read_input_cost_per_token: float
    threshold_tokens: Optional[int] = None
    input_cost_per_token_above_threshold: Optional[float] = None
    output_cost_per_token_above_threshold: Optional[float] = None
    cache_creation_input_cost_per_token_above_threshold: Optional[float] = None
    cache_read_input_cost_per_token_above_threshold: Optional[float] = None


_FLAT_OPUS_5 = ClaudePricing(
    input_cost_per_token=5e-6,
    output_cost_per_token=2.5e-5,
    cache_creation_input_cost_per_token=6.25e-6,
    cache_read_input_cost_per_token=5e-7,
)
_FLAT_OPUS_4 = ClaudePricing(
    input_cost_per_token=1.5e-5,
    output_cost_per_token=7.5e-5,
    cache_creation_input_cost_per_token=1.875e-5,
    cache_read_input_cost_per_token=1.5e-6,
)
_FLAT_HAIKU_45 = ClaudePricing(
    input_cost_per_token=1e-6,
    output_cost_per_token=5e-6,
    cache_creation_input_cost_per_token=1.25e-6,
    cache_read_input_cost_per_token=1e-7,
)
_TIERED_SONNET_4_5_6 = ClaudePricing(
    input_cost_per_token=3e-6,
    output_cost_per_token=1.5e-5,
    cache_creation_input_cost_per_token=3.75e-6,
    cache_read_input_cost_per_token=3e-7,
    threshold_tokens=200_000,
    input_cost_per_token_above_threshold=6e-6,
    output_cost_per_token_above_threshold=2.25e-5,
    cache_creation_input_cost_per_token_above_threshold=7.5e-6,
    cache_read_input_cost_per_token_above_threshold=6e-7,
)


CLAUDE_PRICING: dict[str, ClaudePricing] = {
    # Haiku 4.5
    "claude-haiku-4-5-20251001": _FLAT_HAIKU_45,
    "claude-haiku-4-5": _FLAT_HAIKU_45,
    # Opus 4.5 / 4.6 / 4.7 — same per-token rates
    "claude-opus-4-5-20251101": _FLAT_OPUS_5,
    "claude-opus-4-5": _FLAT_OPUS_5,
    "claude-opus-4-6-20260205": _FLAT_OPUS_5,
    "claude-opus-4-6": _FLAT_OPUS_5,
    "claude-opus-4-7": _FLAT_OPUS_5,
    # Opus 4.0 / 4.1 — older, more expensive
    "claude-opus-4-20250514": _FLAT_OPUS_4,
    "claude-opus-4-1": _FLAT_OPUS_4,
    # Sonnet 4.5 / 4.6 — tiered above 200K
    "claude-sonnet-4-5": _TIERED_SONNET_4_5_6,
    "claude-sonnet-4-5-20250929": _TIERED_SONNET_4_5_6,
    "claude-sonnet-4-6": _TIERED_SONNET_4_5_6,
    "claude-sonnet-4-20250514": _TIERED_SONNET_4_5_6,
}


_VERSION_SUFFIX_RE = re.compile(r"-v\d+:\d+$")
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def normalize_claude_model(model: str) -> str:
    """Mirror codex bar's `normalizeClaudeModel` fallback chain so unknown
    dated variants resolve to the base model name when possible."""
    if not model:
        return model
    trimmed = model.strip()
    if trimmed.startswith("anthropic."):
        trimmed = trimmed[len("anthropic.") :]
    # Strip leading provider prefix like "openrouter/claude-..."
    if "." in trimmed and "claude-" in trimmed:
        last_dot = trimmed.rfind(".")
        tail = trimmed[last_dot + 1 :]
        if tail.startswith("claude-"):
            trimmed = tail
    trimmed = _VERSION_SUFFIX_RE.sub("", trimmed)
    base_match = _DATE_SUFFIX_RE.search(trimmed)
    if base_match:
        base = trimmed[: base_match.start()]
        if base in CLAUDE_PRICING:
            return base
    return trimmed


def _tiered(tokens: int, base: float, above: Optional[float], threshold: Optional[int]) -> float:
    if threshold is None or above is None:
        return tokens * base
    below = min(tokens, threshold)
    over = max(tokens - threshold, 0)
    return below * base + over * above


def claude_cost_usd(
    model: str,
    input_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
) -> Optional[float]:
    """USD cost using codex bar's per-token rates and threshold tiers.

    Returns None when the model is unknown (codex bar's behavior — caller
    decides whether to skip or fall back). All token counts are clamped to
    non-negative.
    """
    key = normalize_claude_model(model)
    pricing = CLAUDE_PRICING.get(key)
    if pricing is None:
        return None
    return (
        _tiered(
            max(0, input_tokens),
            pricing.input_cost_per_token,
            pricing.input_cost_per_token_above_threshold,
            pricing.threshold_tokens,
        )
        + _tiered(
            max(0, cache_read_input_tokens),
            pricing.cache_read_input_cost_per_token,
            pricing.cache_read_input_cost_per_token_above_threshold,
            pricing.threshold_tokens,
        )
        + _tiered(
            max(0, cache_creation_input_tokens),
            pricing.cache_creation_input_cost_per_token,
            pricing.cache_creation_input_cost_per_token_above_threshold,
            pricing.threshold_tokens,
        )
        + _tiered(
            max(0, output_tokens),
            pricing.output_cost_per_token,
            pricing.output_cost_per_token_above_threshold,
            pricing.threshold_tokens,
        )
    )
