"""Aggregate wallet facts into independent-event profiles and explicit reviews."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from smart_money.markets.settlement import terminal_settlement_code
from smart_money.markets.taxonomy import CLASSIFIER_VERSION, classify_market, cluster_identity, normalize_market
from smart_money.markets.trades import USDC_SCALE, decimal_value, receipt_fees
from smart_money.wallets.directional_expert_policy import DirectionalExpertPolicy, decide_directional_expert
from smart_money.wallets.discovery import wallet_address

FEEDS = ("closed_positions", "open_positions", "trades", "activity")
NON_TRADING_CASHFLOWS = {"REWARD", "MAKER_REBATE", "TAKER_REBATE", "YIELD", "DEPOSIT", "WITHDRAWAL"}


def incomplete_feeds(history: dict[str, Any]) -> list[str]:
    return [feed for feed in FEEDS if history.get("coverage", {}).get(feed, {}).get("complete") is not True]


def instant(value: Any) -> datetime:
    result = (
        datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, (int, float))
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if result.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return result.astimezone(timezone.utc)


def cutoff_block(boundary: dict[str, Any], as_of: str) -> int:
    """Require adjacent, identified blocks bracketing this evaluation's cutoff."""
    try:
        block = int(boundary["block_number"])
        if (
            block <= 0
            or int(boundary["next_block_number"]) != block + 1
            or not instant(boundary["block_time"]) <= instant(as_of) < instant(boundary["next_block_time"])
            or not boundary.get("source")
            or any(not re.fullmatch(r"0x[0-9a-f]{64}", boundary[key]) for key in ("block_hash", "next_block_hash"))
        ):
            raise ValueError
        return block
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("LOCAL_CUTOFF_BLOCK_UNVERIFIED") from exc


def number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("MISSING_NUMBER")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("NON_FINITE_NUMBER")
    return result


def _review(reasons: list[str], **details: Any) -> dict[str, Any]:
    return {"status": "UNRESOLVED" if reasons else "PASS", "reasons": sorted(set(reasons)), **details}


def _unique(rows: list[dict[str, Any]], *, trades: bool, gaps: list[str]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    without_identity = []
    for row in rows:
        if trades:
            identity = row.get("trade_id")
            if not identity and row.get("log_index") is not None and row.get("transaction_hash"):
                identity = f"{row['transaction_hash']}:{row['log_index']}:{row.get('proxy_wallet')}"
            if not identity:
                # Equal API rows can represent different fills in one transaction.
                # Keep them for quantity/cash comparison against unique chain logs.
                without_identity.append(row)
                continue
            key = str(identity)
        else:
            key = f"{row.get('condition_id')}:{row.get('token_id')}"
        if key in unique and unique[key] != row:
            gaps.append("CONFLICTING_FACT:" + key)
        unique[key] = row
    return [*unique.values(), *without_identity]


def _reconcile_fees(
    fills: list[dict[str, Any]], local: dict[str, Any], result: dict[str, Any], issues: list[str]
) -> dict[tuple[str, int], tuple[Decimal, Decimal]]:
    """Verify each charged transaction once; missing evidence never implies zero refunds."""
    by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fills:
        by_tx[row["transaction_hash"].lower()].append(row)
    adjustments = {}
    totals: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    required = verified = 0
    unknown_charge = False
    for tx, rows in by_tx.items():
        unidentified = any("fee_raw" not in row and row.get("fee_usdc") is None for row in rows)
        unknown_charge |= unidentified
        gross = {
            int(row["log_index"]): (
                str(row.get("fee_asset_id", "0" if row.get("fee_usdc") is not None else "UNKNOWN")),
                decimal_value(row["fee_raw"]) / USDC_SCALE
                if "fee_raw" in row
                else decimal_value(row.get("fee_usdc") or 0),
                Decimal(0),
            )
            for row in rows
        }
        if any(not fee.is_finite() or fee < 0 for _, fee, _ in gross.values()):
            raise ValueError("INVALID_LOCAL_FEE")
        if unidentified or any(fee > 0 for _, fee, _ in gross.values()):
            required += 1
            try:
                if unidentified:
                    raise ValueError("FEE_REFUND_FILL_IDENTITY_MISSING")
                evidence = local.get("receipts", {}).get(tx)
                if not evidence:
                    raise ValueError("FEE_REFUND_RECEIPT_MISSING")
                if any(instant(row["timestamp"]) != instant(evidence["block_time"]) for row in rows):
                    raise ValueError("FEE_REFUND_RECEIPT_TIME_MISMATCH")
                gross = receipt_fees(rows, evidence)
                verified += 1
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                issues.extend(["FEE_REFUND_COVERAGE_UNVERIFIED", f"FEE_REFUND_UNVERIFIED:{tx}:{exc}"])
                # An old snapshot needs the order/fee identity as well as its receipt.
                feed = (
                    "orderfilled"
                    if any("order_hash" not in row or "fee_raw" not in row for row in rows)
                    else "fee_refunds"
                )
                result["repair_targets"].append({"feed": feed, "transaction_hash": tx, "reason": str(exc)})
        for log_index, (asset, charged, refunded) in gross.items():
            totals[asset][0] += charged
            totals[asset][1] += refunded
            net = charged - refunded
            adjustments[(tx, log_index)] = (net, Decimal(0)) if asset == "0" else (Decimal(0), net)
    unknown = required != verified
    result.update(
        fee_transactions_required=required,
        fee_transactions_verified=verified,
        fees_charged_usdc=None if unknown_charge else str(totals["0"][0]),
        fees_refunded_usdc=None if unknown else str(totals["0"][1]),
        net_fees_usdc=None if unknown else str(totals["0"][0] - totals["0"][1]),
        fees_by_asset={
            asset: {
                "charged": None if asset == "UNKNOWN" else str(values[0]),
                "refunded": None if unknown else str(values[1]),
                "net": None if unknown else str(values[0] - values[1]),
            }
            for asset, values in sorted(totals.items())
        },
    )
    return adjustments


def _reconcile(
    history: dict[str, Any], positions: list[dict[str, Any]], trades: list[dict[str, Any]], gaps: list[str]
) -> dict[str, Any]:
    """Derive coverage and net economics from facts, never an input pass flag."""
    wallet, cutoff = history["wallet"], instant(history["as_of"])
    local = history.get("local_history") or {}
    issues = list(gaps) + list(local.get("issues", []))
    differences: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "wallet": wallet,
        "as_of": cutoff.isoformat(),
        "complete": False,
        "fees_included": False,
        "coverage_start": None,
        "fee_basis": "receipt_verified_net_fees_and_position_operations",
        "repair_targets": [],
        "scope": "wallet_history_checked_against_local_chain_receipts",
        "evidence_refs": [
            "history/local_history",
            "history/activity",
            "history/closed_positions",
            "history/open_positions",
            "history/trades",
            "history/resolutions",
        ],
    }

    def amount(value: Any) -> Decimal:
        parsed = decimal_value(value)
        if not parsed.is_finite():
            raise ValueError("NON_FINITE_ACCOUNTING_AMOUNT")
        return parsed

    def compare(check: str, key: str, expected: Decimal, actual: Any, tolerance: str = "0.0001") -> None:
        observed = amount(actual)
        if abs(expected - observed) > Decimal(tolerance):
            issues.append(check)
            differences.append({"check": check, "key": key, "expected": str(expected), "actual": str(observed)})

    try:
        if wallet_address(local.get("wallet")) != wallet_address(wallet) or instant(local.get("as_of")) != cutoff:
            raise ValueError("LOCAL_HISTORY_IDENTITY_OR_CUTOFF_MISMATCH")
        fills = _unique(local["trades"], trades=True, gaps=issues)
        if not fills or not local.get("source"):
            result["repair_targets"].extend(
                {"feed": "orderfilled", "transaction_hash": tx, "reason": "MISSING_LOCAL_TRADE"}
                for tx in sorted({row["transaction_hash"] for row in trades})
            )
            raise ValueError("LOCAL_HISTORY_EVIDENCE_MISSING")
        coverage = local["coverage"]
        first_event = min(instant(row["timestamp"]) for row in [*trades, *history["activity"], *fills])
        result["coverage_start"] = first_event.isoformat()
        lower = min(
            int(row["block_number"]) for row in [*fills, *local.get("operations", [])] if int(row["block_number"]) > 0
        )
        upper = None
        try:
            upper = cutoff_block(coverage.get("boundary", {}), history["as_of"])
        except ValueError as exc:
            issues.append(str(exc))
            result["repair_targets"].append({"feed": "boundary", "reason": str(exc)})
        if upper is not None and lower > upper:
            raise ValueError("INVALID_LOCAL_BLOCK_COVERAGE")
        fee_adjustments = _reconcile_fees(fills, local, result, issues)
        for feed in ("orderfilled", "activity"):
            if upper is None:
                break
            cursor = lower
            for start, end in sorted(coverage.get(feed) or []):
                if not isinstance(start, int) or not isinstance(end, int) or start > end:
                    raise ValueError("INVALID_LOCAL_SCAN_RECEIPT")
                if cursor > upper:
                    break
                if start > cursor:
                    result["repair_targets"].append(
                        {
                            "feed": feed,
                            "from_block": cursor,
                            "to_block": min(start - 1, upper),
                            "reason": "LOCAL_SCAN_GAP",
                        }
                    )
                cursor = max(cursor, end + 1)
            if cursor <= upper:
                result["repair_targets"].append(
                    {"feed": feed, "from_block": cursor, "to_block": upper, "reason": "LOCAL_SCAN_GAP"}
                )
            if any(item["feed"] == feed for item in result["repair_targets"]):
                issues.append("LOCAL_SCAN_GAP:" + feed)
        official: dict[tuple[str, str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        chain: dict[tuple[str, str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
        balances: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
        for row in trades:
            key = (row["transaction_hash"].lower(), str(row["token_id"]), row["side"])
            size = amount(row["size"])
            official[key][0] += size
            official[key][1] += size * amount(row["price"])
        for row in fills:
            if wallet_address(row.get("proxy_wallet")) != wallet_address(wallet) or instant(row["timestamp"]) > cutoff:
                raise ValueError("LOCAL_FILL_IDENTITY_OR_TIME_MISMATCH")
            if (
                not row.get("trade_id")
                or not row.get("contract")
                or (upper is not None and int(row["block_number"]) > upper)
            ):
                raise ValueError("LOCAL_FILL_EVIDENCE_MISSING")
            token, side = str(row["token_id"]), row["side"]
            size, cash = amount(row["size"]), amount(row["notional"])
            fee, share_fee = fee_adjustments[(row["transaction_hash"].lower(), int(row["log_index"]))]
            if side not in {"BUY", "SELL"} or size <= 0 or cash <= 0 or cash > size or fee < 0:
                raise ValueError("INVALID_LOCAL_FILL_ECONOMICS")
            key = (row["transaction_hash"].lower(), token, side)
            chain[key][0] += size
            chain[key][1] += cash
            chain[key][2] += fee
            balances[token][0] += (size if side == "BUY" else -size) - share_fee
            balances[token][1] += -cash if side == "BUY" else cash
            balances[token][2] += fee
        for key in sorted(official.keys() | chain.keys()):
            label = ":".join(key)
            if key not in chain and key in official:
                result["repair_targets"].append(
                    {
                        "feed": "orderfilled",
                        "transaction_hash": key[0],
                        "token_id": key[1],
                        "side": key[2],
                        "reason": "MISSING_LOCAL_TRADE",
                    }
                )
            compare("TRADE_QUANTITY_MISMATCH", label, chain[key][0], official[key][0])
            compare("TRADE_CASH_MISMATCH", label, chain[key][1], official[key][1], "0.01")
        identities = [
            {
                (
                    row["transaction_hash"].lower(),
                    str(row["token_id"]),
                    row["side"],
                    row["condition_id"],
                    row["outcome_index"],
                    instant(row["timestamp"]),
                )
                for row in rows
            }
            for rows in (trades, fills)
        ]
        if identities[0] != identities[1]:
            issues.append("LOCAL_OFFICIAL_TRADE_IDENTITY_MISMATCH")
        known = {str(row["token_id"]): row for row in positions}
        activity_trades: dict[tuple[str, str, str], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        # Cash from complete-set operations belongs to a condition, not an invented token allocation.
        operation_cash: dict[str, Decimal] = defaultdict(Decimal)
        paired_conditions: set[str] = set()
        local_operations: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
        official_operations: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
        seen_operations: dict[tuple[str, int], dict[str, Any]] = {}
        for row in local.get("operations", []):
            try:
                tx = row["transaction_hash"].lower()
                identity = (tx, int(row["log_index"]))
                if identity in seen_operations:
                    if seen_operations[identity] != row:
                        issues.append("CONFLICTING_LOCAL_OPERATION:" + tx)
                    continue
                seen_operations[identity] = row
                if (
                    wallet_address(row.get("proxy_wallet")) != wallet_address(wallet)
                    or not 0 < instant(row["timestamp"]).timestamp() <= cutoff.timestamp()
                    or int(row["block_number"]) <= 0
                    or not row.get("source")
                    or not row.get("source_contract")
                ):
                    raise ValueError("LOCAL_OPERATION_IDENTITY_OR_TIME_MISMATCH")
                kind, condition = row["type"], row["condition_id"]
                if kind in NON_TRADING_CASHFLOWS:
                    continue
                cash = amount(row["usdc_size"])
                if cash < 0:
                    raise ValueError("INVALID_LOCAL_OPERATION_AMOUNT")
                if kind == "FEE_REFUND":
                    raise ValueError("FEE_REFUND_SOURCE_UNVERIFIED")
                if kind not in {"SPLIT", "MERGE", "REDEEM"}:
                    raise ValueError("NON_TRADE_ACCOUNTING_UNRESOLVED:" + kind)
                if (
                    row["parent_collection_id"] != "0x" + "0" * 64
                    or row["source_contract"].lower() != "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
                    or row["collateral_token"].lower()
                    not in {
                        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",
                        "0x3a3bd7bb9528e159577f7c2e685cc81a765002e2",
                    }
                ):
                    raise ValueError("NON_TRADE_CONTRACT_OR_COLLATERAL_UNVERIFIED")
                if kind in {"SPLIT", "MERGE"} and sorted(row["partition"]) != [1, 2]:
                    raise ValueError("NON_BINARY_POSITION_OPERATION")
                local_operations[(tx, condition, kind)] += cash
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                issues.append(str(exc))
        seen_activity: set[str] = set()
        for row in history["activity"]:
            try:
                if (
                    wallet_address(row.get("proxy_wallet")) != wallet_address(wallet)
                    or instant(row["timestamp"]) > cutoff
                ):
                    raise ValueError("ACTIVITY_IDENTITY_OR_TIME_MISMATCH")
                key_text = json.dumps(row, sort_keys=True)
                if key_text in seen_activity and row.get("type") != "TRADE":
                    raise ValueError("AMBIGUOUS_DUPLICATE_ACTIVITY")
                seen_activity.add(key_text)
                kind, token = row.get("type"), str(row.get("token_id"))
                if kind in NON_TRADING_CASHFLOWS:
                    continue
                tx, condition = row["transaction_hash"].lower(), row["condition_id"]
                if kind == "TRADE":
                    key = (tx, token, row["side"])
                    activity_trades[key][0] += amount(row["size"])
                    activity_trades[key][1] += amount(row["usdc_size"])
                    continue
                if kind not in {"SPLIT", "MERGE", "REDEEM"} or row.get("is_combo"):
                    raise ValueError("NON_TRADE_ACCOUNTING_UNRESOLVED:" + str(kind))
                size, cash = amount(row["size"]), amount(row["usdc_size"])
                if size < 0 or cash < 0:
                    raise ValueError("INVALID_POSITION_OPERATION")
                official_operations[(tx, condition, kind)] += cash
                if kind in {"SPLIT", "MERGE"}:
                    market = normalize_market(history["markets"].get(condition, {}))
                    if market is None or market.condition_id != condition.lower() or len(set(market.token_ids)) != 2:
                        raise ValueError("POSITION_OPERATION_TOKEN_MAPPING_UNRESOLVED")
                    compare("POSITION_OPERATION_CASH_MISMATCH", tx, size, cash, "0.01")
                    sign = 1 if kind == "SPLIT" else -1
                    for asset in market.token_ids:
                        balances[asset][0] += sign * size
                    operation_cash[condition] -= sign * cash
                    paired_conditions.add(condition)
                    continue
                if not row.get("token_id"):
                    matching = [p for p in positions if p["condition_id"] == condition]
                    if len(matching) == 1 and matching[0]["outcome_index"] == row.get("outcome_index"):
                        token = str(matching[0]["token_id"])
                if token not in known or condition != known[token]["condition_id"]:
                    raise ValueError("REDEMPTION_TOKEN_MAPPING_UNRESOLVED")
                resolution = history["resolutions"].get(condition, {})
                code = terminal_settlement_code(
                    resolution.get("payouts"), closed=resolution.get("status") == "RESOLVED"
                )
                if not code or instant(row["timestamp"]) < instant(resolution["resolved_at"]):
                    raise ValueError("REDEMPTION_WITHOUT_FINAL_SETTLEMENT")
                payout = Decimal("0.5") if code == 3 else Decimal(int(known[token]["outcome_index"]) == code - 1)
                compare("REDEMPTION_CASH_MISMATCH", token, size * payout, cash, "0.01")
                balances[token][0] -= size
                balances[token][1] += cash
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                issues.append(str(exc))
        for key in sorted(local_operations.keys() | official_operations.keys()):
            if key not in local_operations or key not in official_operations:
                issues.append("LOCAL_OFFICIAL_OPERATION_MISSING")
                result["repair_targets"].append(
                    {
                        "feed": "operations",
                        "transaction_hash": key[0],
                        "condition_id": key[1],
                        "type": key[2],
                        "missing_in": "local" if key not in local_operations else "official",
                    }
                )
                continue
            compare("NON_TRADE_CASH_MISMATCH", ":".join(key), local_operations[key], official_operations[key], "0.01")
        for key in sorted(official.keys() | activity_trades.keys()):
            compare("ACTIVITY_TRADE_QUANTITY_MISMATCH", ":".join(key), official[key][0], activity_trades[key][0])
            # Activity reports cash paid/received, including the chain fee.
            fee = chain[key][2]
            cash = chain[key][1] + (fee if key[2] == "BUY" else -fee)
            compare("ACTIVITY_TRADE_CASH_MISMATCH", ":".join(key), cash, activity_trades[key][1], "0.01")
        if balances.keys() != known.keys():
            issues.append("LOCAL_POSITION_UNIVERSE_MISMATCH")
        for token, position in known.items():
            quantity, cash, fees = balances[token]
            compare("POSITION_BALANCE_MISMATCH", token, quantity, position["current_size"])
            if position["condition_id"] in paired_conditions:
                continue
            compare(
                "POSITION_NET_PNL_MISMATCH",
                token,
                cash + amount(position["current_value"]) - fees,
                amount(position["realized_pnl"]) + amount(position["unrealized_pnl"]),
                "0.01",
            )
        for condition in sorted(paired_conditions):
            market = normalize_market(history["markets"][condition])
            if market is None:
                raise ValueError("POSITION_OPERATION_TOKEN_MAPPING_UNRESOLVED")
            tokens = market.token_ids
            if any(token not in known for token in tokens):
                issues.append("POSITION_OPERATION_MISSING_POSITION_HISTORY")
                continue
            expected = operation_cash[condition] + sum(
                (balances[t][1] - balances[t][2] + amount(known[t]["current_value"]) for t in tokens), Decimal(0)
            )
            actual = sum(
                (amount(known[t]["realized_pnl"]) + amount(known[t]["unrealized_pnl"]) for t in tokens), Decimal(0)
            )
            compare("CONDITION_NET_PNL_MISMATCH", condition, expected, actual, "0.01")
        refund_unknown = any(reason.startswith("FEE_REFUND_") for reason in issues)
        if refund_unknown:
            result["fees_refunded_usdc"] = result["net_fees_usdc"] = None
            for asset_fees in result["fees_by_asset"].values():
                asset_fees["refunded"] = asset_fees["net"] = None
        result["fees_included"] = not issues
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        issues.append(str(exc) if local else "LOCAL_HISTORY_EVIDENCE_MISSING")
    if issues:
        issues.append("WALLET_HISTORY_COVERAGE_UNVERIFIED")
    if not result["fees_included"]:
        issues.append("NET_FEE_BASIS_UNVERIFIED")
    result.update(complete=not issues, reasons=sorted(set(issues)), differences=differences)
    return result


def event_scopes(history: dict[str, Any], *, as_of: str) -> tuple[dict[str, set[str]], list[str]]:
    """The shared event mother set; selection never starts from winning positions alone."""
    cutoff = instant(as_of)
    policy = DirectionalExpertPolicy()
    start = cutoff - timedelta(days=policy.history_window_days)
    assignments: dict[str, set[str]] = {}
    clusters: dict[str, list[str]] = defaultdict(list)
    unknown = []
    conditions = (
        set(history.get("markets", {}))
        | set(history.get("work", {}).get("index", []))
        | {str(row.get("condition_id")) for feed in FEEDS for row in history.get(feed, []) if row.get("condition_id")}
    )
    for condition in conditions:
        market = normalize_market(history.get("markets", {}).get(condition, {}))
        if market is None or not (market.event_id or market.event_slug):
            unknown.append(condition)
            continue
        sector = classify_market(market).sector_id
        if sector == "OTHER":
            unknown.append(condition)
            continue
        assignments[condition] = {sector, sector.split(".")[0]}
        clusters[cluster_identity(market, CLASSIFIER_VERSION)[0]].append(condition)
    included: set[str] = set(unknown)
    for members in clusters.values():
        dates = []
        for condition in members:
            result = history.get("resolutions", {}).get(condition, {})
            if result.get("resolved_at") and str(result.get("status", "")).upper() in {
                "RESOLVED",
                "SETTLED",
                "FINALIZED",
            }:
                dates.append(instant(result["resolved_at"]))
        if len(dates) != len(members) or start <= max(dates):
            # Future-dated evidence must reach validation, never silently remove a possible loss.
            included.update(members)
    scopes: dict[str, set[str]] = defaultdict(set)
    for condition in included:
        for sector in assignments.get(condition, ()):
            scopes[sector].add(condition)
    return dict(scopes), unknown


def evaluate_history(history: dict[str, Any], sectors: list[str], *, as_of: str) -> dict[str, Any]:
    """Evaluate each sector against the same settled-event window, retaining earlier costs."""
    scopes, unknown = event_scopes(history, as_of=as_of)
    start = instant(as_of) - timedelta(days=DirectionalExpertPolicy().history_window_days)
    profiles = {}
    reconciliations = {}
    for sector in sorted(set(sectors) | set(scopes)):
        selected = scopes.get(sector, set()) | set(unknown)
        scoped = _scoped_history(history, selected)
        result = _evaluate_scope(scoped, [sector], as_of=as_of)
        profiles[sector] = result["profiles"][sector]
        reconciliations[sector] = result["reconciliation"]
        profiles[sector]["window"] = {"start": start.isoformat(), "end": as_of, "basis": "formal_event_settlement"}
    combined = {
        "complete": all(r["complete"] for r in reconciliations.values()),
        "reasons": sorted({reason for r in reconciliations.values() for reason in r["reasons"]}),
        "repair_targets": list(
            {json.dumps(t, sort_keys=True): t for r in reconciliations.values() for t in r["repair_targets"]}.values()
        ),
        "sectors": reconciliations,
    }
    # Preserve the existing detailed accounting contract when only one scope is requested.
    if len({s.split(".")[0] for s in reconciliations}) == 1:
        primary = min(reconciliations, key=lambda s: (s.count("."), s))
        combined = {**reconciliations[primary], "sectors": reconciliations}
    return {"profiles": profiles, "reconciliation": combined}


def _scoped_history(history: dict[str, Any], conditions: set[str]) -> dict[str, Any]:
    def relevant(row: dict[str, Any]) -> bool:
        return not row.get("condition_id") or row["condition_id"] in conditions

    scoped = dict(history)
    scoped.update({feed: [r for r in history.get(feed, []) if relevant(r)] for feed in FEEDS})
    scoped["markets"] = {c: r for c, r in history.get("markets", {}).items() if c in conditions}
    scoped["resolutions"] = {c: r for c, r in history.get("resolutions", {}).items() if c in conditions}
    scoped["coverage"] = {key: dict(value) for key, value in history.get("coverage", {}).items()}
    for feed, coverage in scoped["coverage"].items():
        if "conditions" in coverage:
            coverage["complete"] = all(coverage["conditions"].get(c) is True for c in conditions)
    local = dict(history.get("local_history") or {})
    for feed in ("trades", "operations"):
        local[feed] = [row for row in local.get(feed, []) if relevant(row)]
    scoped["local_history"] = local
    scoped["fetch_errors"] = list(history.get("fetch_errors", []))
    if history.get("work") and not history["work"].get("index_complete"):
        scoped["fetch_errors"].append("EVENT_UNIVERSE_INCOMPLETE")
    for field in ("condition_errors", "request_errors"):
        for condition, errors in history.get(field, {}).items():
            if condition in conditions:
                scoped["fetch_errors"].extend(errors)
    return scoped


def _evaluate_scope(history: dict[str, Any], sectors: list[str], *, as_of: str) -> dict[str, Any]:
    policy = DirectionalExpertPolicy()
    cutoff = instant(as_of)
    wallet = wallet_address(history.get("wallet"))
    if not wallet or instant(history.get("as_of")) != cutoff:
        raise ValueError("HISTORY_IDENTITY_OR_CUTOFF_MISMATCH")
    gaps = list(history.get("gaps", [])) + list(history.get("fetch_errors", []))
    gaps.extend("INCOMPLETE_FEED:" + feed for feed in incomplete_feeds(history))
    coverage = history.get("coverage", {})
    for feed in FEEDS:
        if not isinstance(history.get(feed), list):
            raise ValueError(f"MISSING_FEED:{feed}")
    positions = _unique(history["closed_positions"] + history["open_positions"], trades=False, gaps=gaps)
    trades = _unique(history["trades"], trades=True, gaps=gaps)
    reconciliation = _reconcile(history, positions, trades, gaps)
    gaps.extend(reconciliation["reasons"])
    by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sides: dict[str, set[int]] = defaultdict(set)
    methods: dict[str, set[str]] = defaultdict(set)
    trade_times = []
    for trade in trades:
        try:
            when = instant(trade["timestamp"])
            if wallet_address(trade.get("proxy_wallet")) != wallet or when > cutoff:
                raise ValueError("TRADE_IDENTITY_OR_TIME_MISMATCH")
            outcome = number(trade["outcome_index"])
            if outcome not in (0, 1) or trade.get("side") not in ("BUY", "SELL"):
                raise ValueError("TRADE_DIRECTION_UNKNOWN")
            price, size = number(trade["price"]), number(trade["size"])
            if not 0 < price <= 1 or size <= 0 or not trade.get("transaction_hash"):
                raise ValueError("INVALID_TRADE")
            sides[trade["condition_id"]].add(int(outcome))
            by_token[str(trade["token_id"])].append(trade)
            trade_times.append(when)
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            gaps.append(str(exc))
    for activity in history["activity"]:
        try:
            if wallet_address(activity.get("proxy_wallet")) != wallet or instant(activity["timestamp"]) > cutoff:
                raise ValueError("ACTIVITY_IDENTITY_OR_TIME_MISMATCH")
            kind = str(activity.get("type", "UNKNOWN")).upper()
            if kind not in {"TRADE", "REDEEM"} | NON_TRADING_CASHFLOWS or activity.get("is_combo"):
                methods[str(activity.get("condition_id", "UNKNOWN"))].add(kind)
        except (KeyError, ValueError, TypeError) as exc:
            gaps.append(str(exc))
    known_tokens = {str(row.get("token_id")) for row in positions}
    if set(by_token) - known_tokens:
        gaps.append("TRADED_TOKEN_MISSING_POSITION_HISTORY")

    groups: dict[str, dict[str, Any]] = {}
    risk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sector_gaps: dict[str, list[str]] = defaultdict(list)
    for position in positions:
        try:
            condition, token = str(position["condition_id"]), str(position["token_id"])
            if wallet_address(position.get("proxy_wallet")) != wallet:
                raise ValueError("POSITION_WALLET_MISMATCH")
            if instant(position["last_event_at"]) > cutoff:
                raise ValueError("POSITION_AFTER_CUTOFF")
            market = normalize_market(history["markets"].get(condition, {}))
            if market is None or market.condition_id != condition.lower() or not (market.event_id or market.event_slug):
                raise ValueError("MARKET_OR_INDEPENDENT_EVENT_MAPPING_MISSING:" + condition)
            assignment = classify_market(market)
            sector = assignment.sector_id
            if sector == "OTHER":
                raise ValueError("SECTOR_UNKNOWN:" + condition)
            outcome = number(position["outcome_index"])
            if outcome not in (0, 1) or len(market.token_ids) != 2 or market.token_ids[int(outcome)] != token:
                raise ValueError("TOKEN_OUTCOME_MAPPING_MISMATCH:" + token)
            related = {sector, sector.split(".")[0]}
            event_id, _ = cluster_identity(market, CLASSIFIER_VERSION)
            group = groups.setdefault(
                event_id, {"event_id": event_id, "sectors": set(), "positions": [], "conditions": set(), "issues": []}
            )
            group["sectors"].update(related)
            group["conditions"].add(condition)
            token_trades = by_token[token]
            buys = [row for row in token_trades if row["side"] == "BUY"]
            if not buys:
                group["issues"].append("ENTRY_DIRECTION_OR_COST_UNVERIFIED")
            elif any(
                int(row["outcome_index"]) != int(outcome) or row["condition_id"] != condition for row in token_trades
            ):
                group["issues"].append("TRADE_POSITION_MAPPING_MISMATCH")
            if methods[condition]:
                group["issues"].append("NON_DIRECTIONAL_POSITION_OPERATIONS:" + ",".join(sorted(methods[condition])))
            if buys and any(
                instant(row["timestamp"]) < min(instant(buy["timestamp"]) for buy in buys)
                for row in token_trades
                if row["side"] == "SELL"
            ):
                group["issues"].append("SELL_BEFORE_KNOWN_ENTRY")
            size = number(position["current_size"])
            if size < 0:
                raise ValueError("NEGATIVE_POSITION_SIZE")
            realized = number(position["realized_pnl"])
            unrealized = number(position["unrealized_pnl"])
            entry_cost = number(position["entry_cost_usdc"])
            if entry_cost < 0:
                raise ValueError("INVALID_ENTRY_COST")
            resolution = history["resolutions"].get(condition, {})
            if resolution and resolution.get("condition_id") != condition:
                raise ValueError("RESOLUTION_CONDITION_MISMATCH")
            terminal = str(resolution.get("status", "")).upper() in {"RESOLVED", "SETTLED", "FINALIZED"}
            code = terminal_settlement_code(resolution.get("payouts"), closed=terminal)
            settled_at = instant(resolution["resolved_at"]) if code else None
            if code and (not resolution.get("transaction_hash") or not resolution.get("resolved_block")):
                raise ValueError("FORMAL_SETTLEMENT_EVIDENCE_MISSING")
            if settled_at and settled_at > cutoff:
                raise ValueError("SETTLEMENT_AFTER_CUTOFF")
            if settled_at and any(instant(row["timestamp"]) >= settled_at for row in buys):
                group["issues"].append("ENTRY_AT_OR_AFTER_SETTLEMENT")
            if not code and market.closed:
                group["issues"].append("CLOSED_MARKET_WITHOUT_FORMAL_SETTLEMENT")
            valuation_issues = []
            if size:
                price, value = number(position["current_price"]), number(position["current_value"])
                if not 0 <= price <= 1 or not math.isclose(value, size * price, abs_tol=0.01):
                    valuation_issues.append("OPEN_VALUATION_INCONSISTENT")
                if code:
                    payout = 0.5 if code == 3 else float(int(outcome) == code - 1)
                    if not math.isclose(price, payout, abs_tol=1e-6):
                        valuation_issues.append("SETTLED_POSITION_MARK_MISMATCH")
                elif (
                    not position.get("valuation_as_of")
                    or instant(position["valuation_as_of"]) != cutoff
                    or not position.get("valuation_evidence")
                ):
                    valuation_issues.append("OPEN_VALUATION_UNVERIFIED")
            if not math.isclose(unrealized, number(position["current_value"]) - entry_cost, abs_tol=0.01):
                valuation_issues.append("UNREALIZED_COST_RECONCILIATION_FAILED")
            for target in related:
                sector_gaps[target].extend(valuation_issues)
                risk[target].append(
                    {
                        "token_id": token,
                        "realized_pnl": realized,
                        "unrealized_pnl": unrealized,
                        "current_size": size,
                        "settled": bool(code),
                        "issues": valuation_issues,
                    }
                )
            group["positions"].append(
                {
                    "condition_id": condition,
                    "sectors": sorted(related),
                    "token_id": token,
                    "settled_at": settled_at,
                    "won": (int(outcome) == code - 1) if code in (1, 2) else None,
                    "pnl": realized + unrealized if code else None,
                    "realized_pnl": realized,
                    "two_sided": len(sides[condition]) > 1,
                    "entry_prices": [number(row["price"]) for row in buys],
                    "holding_days_upper_bound": (
                        min(settled_at or cutoff, cutoff) - min(instant(row["timestamp"]) for row in buys)
                    ).total_seconds()
                    / 86400
                    if buys
                    else None,
                    "early_exit": any(
                        row["side"] == "SELL" and (not settled_at or instant(row["timestamp"]) < settled_at)
                        for row in token_trades
                    ),
                }
            )
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            gaps.append(str(exc))
    if methods.get("UNKNOWN"):
        gaps.append("UNATTRIBUTED_POSITION_OPERATIONS")

    output = {}
    for sector in sorted(set(sectors) | {s for group in groups.values() for s in group["sectors"]}):
        events = []
        style_issues = []
        for group in groups.values():
            if sector not in group["sectors"]:
                continue
            positions = [row for row in group["positions"] if sector in row["sectors"]]
            if not positions:
                continue
            # Related markets count once. Mixed or multiple legs need attribution,
            # never an optimistic "any winning leg" event win.
            if len(group["conditions"]) > 1:
                style_issues.append("MULTI_MARKET_EVENT_ATTRIBUTION_UNRESOLVED")
            settled = all(row["settled_at"] is not None for row in positions)
            if (
                settled
                and not cutoff - timedelta(days=policy.history_window_days)
                <= max(row["settled_at"] for row in positions)
                < cutoff
            ):
                continue
            style_issues.extend(group["issues"])
            event = {
                "event_id": group["event_id"],
                "condition_ids": sorted(group["conditions"]),
                "two_sided": any(row["two_sided"] for row in positions),
                "settled_at": max(row["settled_at"] for row in positions).isoformat() if settled else None,
                "won": all(row["won"] for row in positions)
                if all(row["won"] is not None for row in positions)
                else None,
                "pnl": sum(row["pnl"] for row in positions) if settled else None,
                "position_count": len(positions),
                "entry_prices": [price for row in positions for price in row["entry_prices"]],
                "holding_days_upper_bound": [
                    row["holding_days_upper_bound"] for row in positions if row["holding_days_upper_bound"] is not None
                ],
                "early_exit": any(row["early_exit"] for row in positions),
            }
            if event["early_exit"]:
                style_issues.append("EARLY_EXIT_STYLE_REQUIRES_REVIEW")
            events.append(event)
        directional = [e for e in events if e["settled_at"] and e["won"] is not None and not e["two_sided"]]
        recent = [
            e for e in directional if instant(e["settled_at"]) >= cutoff - timedelta(days=policy.recent_window_days)
        ]
        metrics = _metrics(directional, recent, [e for e in events if e["settled_at"]], cutoff)
        limits = [name for name in ("profit_factor", "recent_profit_factor") if metrics[name] is None]
        data_review = _review(
            gaps + (["PF_WITHOUT_LOSS_SAMPLE:" + name for name in limits]),
            coverage=coverage,
            scope=reconciliation["scope"],
            fee_basis=reconciliation["fee_basis"],
            coverage_start=reconciliation.get("coverage_start"),
            coverage_end=as_of,
            first_trade_at=min(trade_times).isoformat() if trade_times else None,
        )
        largest = max((row for row in directional if row["pnl"] > 0), key=lambda row: row["pnl"], default=None)
        remainder = metrics["sector_pnl"] - max(0, largest["pnl"] if largest else 0)
        concentration = _review(
            ["PROFIT_CONCENTRATED"] if largest and remainder <= 0 else [],
            largest_profit_event=largest,
            pnl_without_largest_profit=remainder,
        )
        prices = [p for e in directional for p in e["entry_prices"]]
        style = _review(
            style_issues,
            entry_prices=prices,
            entry_price_distribution={
                f"({low},{high}]": sum(low < p <= high for p in prices)
                for low, high in ((0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1))
            },
            holding_days_upper_bound=[days for event in events for days in event["holding_days_upper_bound"]],
            early_exit_events=sum(e["early_exit"] for e in events),
            price_note="Entry prices are descriptive; a high win rate grants no extra credit",
        )
        total = sum(p["realized_pnl"] + p["unrealized_pnl"] for p in risk[sector])
        open_risk = _review(
            sector_gaps[sector] + (["TOTAL_PERFORMANCE_NEGATIVE"] if total < 0 else []),
            positions=risk[sector],
            realized_plus_unrealized_pnl=total,
        )
        reviews = {"data": data_review, "concentration": concentration, "trading_style": style, "open_risk": open_risk}
        if not directional:
            concentration.update(status="NOT_APPLICABLE", note="No eligible settled directional events")
        if not events and not style_issues:
            style.update(status="NOT_APPLICABLE", note="No attributable events")
        if not risk[sector] and not sector_gaps[sector]:
            open_risk.update(status="NOT_APPLICABLE", note="No positions in this sector")
        decision = decide_directional_expert(metrics, policy)
        unresolved = sorted({reason for review in reviews.values() for reason in review["reasons"]})
        eligible = decision.eligible and not unresolved
        output[sector] = {
            "historical_eligible": eligible,
            "status": "QUALIFIED" if eligible else "PENDING_REVIEW" if unresolved else "REJECTED",
            "reasons": list(decision.reasons) + unresolved,
            "metrics": metrics,
            "reviews": reviews,
            "events": events,
            "policy_version": policy.version,
        }
    return {"profiles": output, "reconciliation": reconciliation}


def _metrics(
    events: list[dict[str, Any]], recent: list[dict[str, Any]], all_events: list[dict[str, Any]], cutoff: datetime
) -> dict[str, Any]:
    def pf(rows: list[dict[str, Any]]) -> float | None:
        gain = sum(max(0, row["pnl"]) for row in rows)
        loss = -sum(min(0, row["pnl"]) for row in rows)
        return gain / loss if loss else None

    return {
        "observed_events": len(all_events),
        "settled_events": sum(bool(row["settled_at"]) for row in all_events),
        "two_sided_events": sum(row["two_sided"] for row in all_events),
        "directional_events": len(events),
        "win_rate": sum(row["won"] for row in events) / len(events) if events else 0,
        "sector_pnl": sum(row["pnl"] for row in events),
        "profit_factor": pf(events),
        "two_sided_ratio": sum(row["two_sided"] for row in all_events) / len(all_events) if all_events else 0,
        "recent_events": len(recent),
        "recent_win_rate": sum(row["won"] for row in recent) / len(recent) if recent else 0,
        "recent_sector_pnl": sum(row["pnl"] for row in recent),
        "recent_profit_factor": pf(recent),
        "recent_inactivity_days": (cutoff - max(instant(row["settled_at"]) for row in events)).total_seconds() / 86400
        if events
        else None,
    }
