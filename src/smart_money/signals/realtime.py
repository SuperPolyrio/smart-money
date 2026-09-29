"""Transaction-level observations; inventory and eligibility remain upstream facts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal
from typing import Any

from smart_money.markets.evidence import build_evidence
from smart_money.markets.taxonomy import CLASSIFIER_VERSION, classify_market, cluster_identity, normalize_market
from smart_money.markets.trades import V2_EXCHANGES, decimal_value, receipt_fees
from smart_money.wallets.history import instant


def event_key(chain_id: int, tx_hash: str, log_index: int) -> str:
    return f"{chain_id}:{tx_hash.lower()}:{log_index}"


def position_action(previous_tokens: Decimal, delta_tokens: Decimal) -> str:
    current = previous_tokens + delta_tokens
    if previous_tokens < 0 or current < 0 or current == previous_tokens:
        return "UNKNOWN"
    if previous_tokens == 0 < current:
        return "OPEN"
    if current > previous_tokens:
        return "ADD"
    return "EXIT" if current == 0 else "REDUCE"


def monitoring_at(versions: list[dict[str, Any]], sector: str, at: str) -> dict[str, Any] | None:
    """Use the last applied membership per sector; admission covers the wallet's trades."""
    latest = {}
    for version in sorted(versions, key=lambda v: instant(v["available_at"])):
        if instant(version["available_at"]) <= instant(at):
            latest[version["record"]["sector_id"]] = version
    admitted = [
        v
        for v in latest.values()
        if v["record"]["monitor"]["enabled"]
        and v["record"]["monitor"]["mode"] in {"NORMAL", "EXPLORE"}
        and not v["record"]["manual_paused"]
    ]
    return min(
        admitted,
        key=lambda v: (v["record"]["sector_id"] != sector, v["record"]["sector_id"] != sector.split(".")[0]),
        default=None,
    )


def transaction_observations(
    transaction: dict[str, Any],
    wallet: str,
    *,
    before: dict[str, Decimal | None],
    after: dict[str, Decimal | None],
    changed_tokens: set[str],
    markets: dict[str, dict[str, Any]],
    qualifications: list[dict[str, Any]],
    observed_at: str,
    live_max_delay_seconds: int,
    inventory_complete: bool = True,
) -> list[dict[str, Any]]:
    """Aggregate owner fills once, distinguish paired/ambiguous activity, and freeze evidence."""
    fills = [f for f in transaction["fills"] if f["proxy_wallet"] == wallet]
    fee_error = None
    fees = {}
    if fills:
        try:
            fees = receipt_fees(fills, transaction)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            fee_error = str(exc)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        grouped[(fill["token_id"], fill["side"])].append(fill)
    for token in changed_tokens:
        if not any(key[0] == token for key in grouped):
            grouped[(token, "NON_TRADE")] = []
    observations = []
    for (token, side), rows in sorted(grouped.items()):
        previous, current = before[token], after[token]
        snapshot = markets.get(token)
        market = normalize_market(snapshot["payload"]) if snapshot else None
        issues = []
        if not inventory_complete:
            issues.append("BASELINE_TOKEN_DISCOVERY_INCOMPLETE")
        if previous is None or current is None:
            issues.append("POSITION_BASELINE_UNVERIFIED")
        sector = classify_market(market).sector_id if market else "OTHER"
        event = cluster_identity(market, CLASSIFIER_VERSION)[0] if market else None
        outcome = None
        if not market or len(market.token_ids) != 2 or len(market.outcomes) != 2 or token not in market.token_ids:
            issues.append("MARKET_MAPPING_UNVERIFIED")
        else:
            outcome = str(market.outcomes[market.token_ids.index(token)])
            other = next(t for t in market.token_ids if t != token)
            if before.get(other) is None or after.get(other) is None:
                issues.append("OPPOSITE_POSITION_UNKNOWN")
            elif before[other] or after[other]:
                issues.append("PAIRED_POSITION_CONTEXT")
        for other, balance in after.items():
            if other == token:
                continue
            if before[other] is None or balance is None:
                issues.append("POSITION_CONTEXT_UNVERIFIED")
                continue
            if not before[other] and not balance:
                continue
            other_market = normalize_market(markets[other]["payload"]) if other in markets else None
            if other_market is None:
                issues.append("POSITION_CONTEXT_UNVERIFIED")
            elif (
                market
                and other_market.condition_id != market.condition_id
                and cluster_identity(other_market, CLASSIFIER_VERSION)[0] == event
            ):
                issues.append("MULTI_MARKET_EVENT_CONTEXT")
        if any(key[0] == token and key[1] != side for key in grouped):
            issues.append("ROUND_TRIP_OR_MIXED_DIRECTION")
        size = sum((decimal_value(row["size"]) for row in rows), Decimal(0))
        cash = sum((decimal_value(row["notional"]) for row in rows), Decimal(0))
        price = cash / size if size else None
        if rows and (size <= 0 or price is None or not 0 < price <= 1):
            issues.append("INVALID_TRADE_ECONOMICS")
        share_fees = sum(
            (
                fees[int(r["log_index"])][1] - fees[int(r["log_index"])][2]
                for r in rows
                if int(r["log_index"]) in fees and fees[int(r["log_index"])][0] != "0"
            ),
            Decimal(0),
        )
        expected_delta = (size if side == "BUY" else -size) - share_fees
        if rows and previous is not None and current is not None and expected_delta != current - previous:
            issues.append("FILL_TRANSFER_ATTRIBUTION_UNRESOLVED")
        if any(r["contract"] not in V2_EXCHANGES for r in rows):
            issues.append("LIVE_DEPLOYMENT_NOT_SUPPORTED")
        if fee_error:
            issues.append("FEES_UNVERIFIED:" + fee_error)
        action = (
            position_action(previous, current - previous)
            if rows
            and previous is not None
            and current is not None
            and not fee_error
            and not {"FILL_TRANSFER_ATTRIBUTION_UNRESOLVED", "ROUND_TRIP_OR_MIXED_DIRECTION"}.intersection(issues)
            else "UNKNOWN"
            if rows
            else "NON_TRADE"
        )
        version = monitoring_at(qualifications, sector, transaction["block_time"])
        reasons = [
            issue
            for issue in issues
            if issue in {"MARKET_MAPPING_UNVERIFIED", "INVALID_TRADE_ECONOMICS", "LIVE_DEPLOYMENT_NOT_SUPPORTED"}
        ]
        if version is None:
            reasons.append("NOT_MONITORED_AT_TRADE")
        if not rows or side not in {"BUY", "SELL"}:
            reasons.append("NOT_A_CONFIRMED_TRADE")
        delay = (instant(observed_at) - instant(transaction["block_time"])).total_seconds()
        mode = "LIVE_SHADOW" if 0 <= delay <= live_max_delay_seconds else "RETROSPECTIVE_WIRING_REPLAY"
        identity = f"137:{transaction['transaction_hash']}:{wallet}:{token}:{side}"
        identifier = "sig_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        row = version["record"] if version else {}
        net_fees = (
            sum(
                (
                    fees[int(r["log_index"])][1] - fees[int(r["log_index"])][2]
                    for r in rows
                    if fees[int(r["log_index"])][0] == "0"
                ),
                Decimal(0),
            )
            if rows and not fee_error
            else None
        )
        evidence = build_evidence(
            signal_id=identifier,
            signal_at=instant(transaction["block_time"]),
            market={
                "condition_id": market.condition_id if market else None,
                "asset_id": token,
                "title": market.title if market else None,
                "event_cluster_id": event,
                "primary_sector": sector,
                "metadata_ref": token,
                "metadata_obtained_at": snapshot["obtained_at"] if snapshot else None,
            },
            wallet={
                "address": wallet,
                "qualification_ref": version["reference"] if version else None,
                "monitoring_ref": version["reference"] if version else None,
                "monitoring_mode": row.get("monitor", {}).get("mode"),
                "historical_eligible": row.get("historical_eligible", False),
                "historical_status": row.get("status"),
                "profile_stale": row.get("stale"),
                "profile_data_cutoff": row.get("data_cutoff"),
                "admission_status": row.get("forward_status"),
                "market_sector": sector,
                "source_profile_sector": row.get("sector_id"),
                "sector_match": row.get("sector_id") in {sector, sector.split(".")[0]},
                "metrics": row.get("metrics", {}),
            },
            trade={
                "action": action,
                "side": side,
                "outcome": outcome,
                "size": str(size),
                "wallet_entry_price": str(price) if price is not None else None,
                "average_entry_price": None,
                "current_trade_notional": str(cash),
                "position_before": str(previous) if previous is not None else None,
                "position_after": str(current) if current is not None else None,
                "net_fees_usdc": str(net_fees) if net_fees is not None else None,
                "fees_verified": bool(rows) and not fee_error,
                "source_refs": [event_key(137, transaction["transaction_hash"], int(r["log_index"])) for r in rows],
            },
            signal={
                "observation_mode": mode,
                "first_observed_at": observed_at,
                "trade_at": transaction["block_time"],
                "detection_delay_seconds": delay,
                "suppressed": bool(reasons),
                "suppression_reasons": sorted(set(reasons)),
                "signal_type": "SMART_MONEY_TRADE" if version and rows else "WALLET_OBSERVATION",
            },
            caveats=sorted(set(issues))
            + [
                "Existing position cost is unknown; current trade price does not replace it.",
                "Historical qualification is separate from forward validation and follow eligibility.",
            ],
        )
        observations.append(
            {
                "observation_id": identifier,
                "wallet": wallet,
                "token_id": token,
                "transaction_ref": transaction["transaction_hash"],
                "evidence": evidence,
                "qualification": version,
                "research_eligible": not reasons,
            }
        )
    return observations
