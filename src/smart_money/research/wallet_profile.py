"""Qualified wallet profiles in the explicit analysis input."""

from typing import Literal

from smart_money.research.models import SignalCandidate, WalletProfilePacket


def public_smart_money_wallets(candidate: SignalCandidate) -> list[WalletProfilePacket]:
    return [
        profile
        for profile in candidate.wallet_profiles
        if profile.wallet_profile_complete
        and profile.wallet_validation_status == "VALIDATED_SPECIALIST"
        and profile.admission_status == "FORWARD_VALIDATED"
        and profile.sector_match is True
        and profile.market_sector == candidate.sector_id
        and profile.source_profile_sector in {candidate.sector_id, candidate.sector_id.split(".", 1)[0]}
        and profile.wallet.lower() == candidate.wallet.lower()
    ]


def wallet_display_label(candidate: SignalCandidate) -> Literal["已验证领域聪明钱", "普通候选账户"]:
    """Use frozen qualified profiles; size and caller-provided labels grant no identity."""
    return "已验证领域聪明钱" if public_smart_money_wallets(candidate) else "普通候选账户"
