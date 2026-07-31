"""Fireworks Zero Data Retention guardrails (OPE-37).

Fireworks' ZDR default only covers **open models** on the Chat Completions
API (https://docs.fireworks.ai/guides/security_compliance/data_handling).
The documented ways to fall out of ZDR are the Responses API with
``store=True``, per-feature opt-in logging, and proprietary Fireworks models
(e.g. FireFunction, which logs I/O for 30 days). These tests fail when a code
change would step outside the ZDR surface; see OPERATIONS.md "Fireworks Zero Data
Retention (OPE-37)".
"""

from agent.dashboard.options import DEPRECATED_MODEL_REPLACEMENTS, SUPPORTED_MODELS
from agent.utils.model import provider_model_kwargs

# Open-weight model ids verified against the Fireworks ZDR policy (OPE-37).
# Adding a new fireworks: id to SUPPORTED_MODELS requires re-verifying it is an
# open model (not a proprietary Fireworks model) and extending this pin.
ZDR_VERIFIED_FIREWORKS_IDS: frozenset[str] = frozenset(
    {
        "fireworks:accounts/fireworks/models/kimi-k3",
        "fireworks:accounts/fireworks/models/deepseek-v4-pro",
        "fireworks:accounts/fireworks/models/glm-5p2",
    }
)


def test_every_configured_fireworks_model_is_zdr_verified() -> None:
    """Every selectable or migration-target fireworks: id is on the verified pin."""
    configured = {m["id"] for m in SUPPORTED_MODELS if m["id"].startswith("fireworks:")}
    configured |= {v for v in DEPRECATED_MODEL_REPLACEMENTS.values() if v.startswith("fireworks:")}
    assert configured, "expected at least one Fireworks model to be configured"
    unverified = configured - ZDR_VERIFIED_FIREWORKS_IDS
    assert not unverified, (
        f"Fireworks model(s) not ZDR-verified: {sorted(unverified)}. "
        "Verify each is an open model under Fireworks' ZDR default, then extend "
        "ZDR_VERIFIED_FIREWORKS_IDS (see OPERATIONS.md, OPE-37)."
    )


def test_fireworks_kwargs_carry_no_responses_api_storage() -> None:
    """Fireworks model kwargs never opt into Responses-API conversation storage."""
    for model_id in sorted(ZDR_VERIFIED_FIREWORKS_IDS):
        kwargs = provider_model_kwargs(model_id, "high", max_tokens=16_000)
        assert "store" not in kwargs, f"{model_id} kwargs must not carry store"
        assert "use_responses_api" not in kwargs, (
            f"{model_id} kwargs must not route to the Responses API"
        )
