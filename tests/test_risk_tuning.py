from __future__ import annotations

import pytest

from app.broadcast.risk_tuning import TierObservation, recommend_defaults


def test_recommend_from_first_unsafe_tier_with_margin():
    tiers = [
        TierObservation(
            tier_id="L1",
            msg_gap_seconds=(12, 36),
            contact_gap_seconds=(72, 180),
            contacts_tested=5,
            total_steps=15,
            failed_steps=0,
            warning_count=0,
        ),
        TierObservation(
            tier_id="L2",
            msg_gap_seconds=(10, 30),
            contact_gap_seconds=(60, 150),
            contacts_tested=5,
            total_steps=15,
            failed_steps=0,
            warning_count=0,
        ),
        TierObservation(
            tier_id="L3",
            msg_gap_seconds=(8, 24),
            contact_gap_seconds=(48, 120),
            contacts_tested=5,
            total_steps=15,
            failed_steps=1,
            warning_count=1,
        ),
    ]

    result = recommend_defaults(tiers, speed_margin=0.2, max_failed_rate=0.05, min_contacts=5)
    assert result["safe_tier_id"] == "L2"
    assert result["recommended_msg_gap_seconds"] == [10, 30]
    assert result["recommended_contact_gap_seconds"] == [60, 150]


def test_recommend_raises_when_no_safe_tier():
    tiers = [
        TierObservation(
            tier_id="L1",
            msg_gap_seconds=(8, 24),
            contact_gap_seconds=(48, 120),
            contacts_tested=5,
            total_steps=15,
            failed_steps=2,
            warning_count=1,
        )
    ]
    with pytest.raises(ValueError):
        recommend_defaults(tiers)
