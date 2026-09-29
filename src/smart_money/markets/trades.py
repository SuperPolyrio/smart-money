"""Normalize confirmed on-chain OrderFilled records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

USDC_SCALE = Decimal("1000000")
V2_FILL_TOPIC = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
LEGACY_FILL_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
REFUND_TOPIC = "0xb608d2bf25d8b4b744ba23ce2ea9802ea955e216c064a62f42152fbf98958d24"
# Official deployment registry and exchange-fee-module v2.0.0 release.
# The upgradeable combos exchange is intentionally not assigned the binary V2 fee contract.
V2_EXCHANGES = {"0xe111180000d2663c0091e4f400237545b87b996b", "0xe2222d279d744050d28e00520010520000310f59"}
FEE_MODULES = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": "0xe3f18acc55091e2c48d883fc8c8413319d4ab7b0",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": "0xb768891e3130f6df18214ac804d4db76c2c37730",
}
TRANSACTION_SOURCE = "xue-lab:core.orderfilled_transactions:evidence_zstd:v1"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"


def chain_hex(value: Any, size: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{" + str(size * 2) + "}", value):
        raise ValueError("INVALID_CHAIN_IDENTITY")
    return value.lower()


def transfer_changes(log: dict[str, Any], wallet: str) -> dict[str, Decimal]:
    """Decode CTF inventory changes; fills must never be added to these balances."""
    topics = log.get("topics", [])
    if log["address"].lower() != CTF or not topics or topics[0] not in {TRANSFER_SINGLE, TRANSFER_BATCH}:
        return {}
    if len(topics) != 4 or any(not re.fullmatch(r"0x[0-9a-fA-F]{64}", t) for t in topics):
        raise ValueError("INVALID_TRANSFER_TOPICS")
    if any(int(topic[2:26], 16) for topic in topics[1:]):
        raise ValueError("INVALID_TRANSFER_ADDRESS")
    sender, receiver = ("0x" + t[-40:].lower() for t in topics[2:])
    sign = int(receiver == wallet) - int(sender == wallet)
    if not sign:
        return {}
    data = log["data"]
    if not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})+", data):
        raise ValueError("INVALID_TRANSFER_DATA")
    words = [int(data[i : i + 64], 16) for i in range(2, len(data), 64)]
    if topics[0] == TRANSFER_SINGLE:
        if len(words) != 2:
            raise ValueError("INVALID_TRANSFER_SINGLE")
        pairs = [(words[0], words[1])]
    else:
        if len(words) < 4 or words[0] != 64:
            raise ValueError("INVALID_TRANSFER_BATCH")
        count = words[2]
        if words[1] != (3 + count) * 32 or len(words) != 4 + 2 * count or words[3 + count] != count:
            raise ValueError("INVALID_TRANSFER_BATCH")
        pairs = list(zip(words[3 : 3 + count], words[4 + count :], strict=True))
    changes: dict[str, Decimal] = {}
    for token, amount in pairs:
        key = str(token)
        changes[key] = changes.get(key, Decimal(0)) + sign * Decimal(amount) / USDC_SCALE
    return changes


@dataclass(frozen=True)
class OrderFilledComponent:
    asset_id: str
    side: str
    size: Decimal
    notional: Decimal


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid decimal value: {value!r}") from error


def orderfilled_component(row: Mapping[str, Any], wallet: str) -> OrderFilledComponent:
    """Interpret decoded fills with decimal asset IDs; source adapters own raw encoding."""
    maker = str(row.get("maker") or "").lower()
    taker = str(row.get("taker") or "").lower()
    wallet = wallet.lower()
    if wallet not in {maker, taker}:
        raise ValueError("wallet is neither OrderFilled maker nor taker")

    maker_asset_id = str(row.get("maker_asset_id") or "").strip()
    taker_asset_id = str(row.get("taker_asset_id") or "").strip()
    maker_amount = decimal_value(row.get("maker_amount_filled"))
    taker_amount = decimal_value(row.get("taker_amount_filled"))
    if maker_asset_id == "0" and taker_asset_id != "0":
        maker_side = "BUY"
        token_id = taker_asset_id
        usdc_raw = maker_amount
        token_raw = taker_amount
    elif taker_asset_id == "0" and maker_asset_id != "0":
        maker_side = "SELL"
        token_id = maker_asset_id
        usdc_raw = taker_amount
        token_raw = maker_amount
    else:
        raise ValueError("OrderFilled must contain exactly one collateral asset")
    if usdc_raw <= 0 or token_raw <= 0:
        raise ValueError("OrderFilled amounts must be positive")

    if not re.fullmatch(r"[1-9][0-9]*", token_id):
        raise ValueError("OrderFilled token id must be a positive decimal integer")
    side = maker_side if wallet == maker else ("SELL" if maker_side == "BUY" else "BUY")
    return OrderFilledComponent(
        asset_id=token_id,
        side=side,
        size=token_raw / USDC_SCALE,
        notional=usdc_raw / USDC_SCALE,
    )


def wallet_fill(row: dict[str, Any], wallet: str, timestamp: str) -> dict[str, Any]:
    """One decoded OrderFilled projection shared by history and live observation."""
    component = orderfilled_component(row, wallet)
    fee = decimal_value(row["fee"]) / USDC_SCALE
    if not fee.is_finite() or fee < 0:
        raise ValueError("LOCAL_FEE_INVALID")
    collateral_fee = row["event_topic"] == V2_FILL_TOPIC or component.side == "SELL"
    return {
        "proxy_wallet": wallet,
        **{key: row[key] for key in ("condition_id", "outcome_index") if key in row},
        "token_id": component.asset_id,
        "side": component.side,
        "size": str(component.size),
        "price": str(component.notional / component.size),
        "notional": str(component.notional),
        "fee_usdc": str(fee) if collateral_fee or fee == 0 else None,
        "fee_raw": str(row["fee"]),
        "fee_asset_id": "0" if collateral_fee else component.asset_id,
        "order_hash": row["order_hash"],
        "event_topic": row["event_topic"],
        "timestamp": timestamp,
        "transaction_hash": row["transaction_hash"],
        "block_number": row["block_number"],
        "log_index": row["log_index"],
        "contract": row["contract"],
        "trade_id": f"{row['contract']}:{row['transaction_hash']}:{row['log_index']}",
    }


def receipt_fees(fills: list[dict[str, Any]], evidence: dict[str, Any]) -> dict[int, tuple[str, Decimal, Decimal]]:
    """Return asset, gross fee and refund per fill, bound to a full upstream receipt.

    V2 charges actual collateral fees. Legacy FeeModule refunds the original fee asset:
    collateral on SELL, outcome shares on BUY. Neither rewards nor surplus transfers are refunds.
    """

    def quantity(value: Any) -> int:
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
            return int(value, 16)
        raise ValueError("FEE_REFUND_INVALID_QUANTITY")

    def words(value: Any, count: int) -> list[int]:
        data = chain_hex(value, count * 32)[2:]
        return [int(data[i : i + 64], 16) for i in range(0, len(data), 64)]

    first = fills[0]
    tx_hash, wallet = chain_hex(first["transaction_hash"], 32), chain_hex(first["proxy_wallet"], 20)
    tx, receipt = evidence["transaction_json"], evidence["receipt_json"]
    block, index = int(first["block_number"]), quantity(tx["transactionIndex"])
    block_hash = chain_hex(tx["blockHash"], 32)
    if (
        evidence.get("source") not in {TRANSACTION_SOURCE, "polygon_rpc:confirmed_receipt"}
        or evidence["transaction_hash"] != tx_hash
        or evidence["chain_id"] != 137
        or quantity(tx["chainId"]) != 137
        or evidence["block_number"] != block
        or quantity(receipt["status"]) != 1
        or chain_hex(tx["hash"], 32) != tx_hash
        or chain_hex(receipt["transactionHash"], 32) != tx_hash
        or any(chain_hex(tx[key], 20) != chain_hex(receipt[key], 20) for key in ("from", "to"))
        or index < 0
    ):
        raise ValueError("FEE_REFUND_RECEIPT_IDENTITY_MISMATCH")
    for obj in (tx, receipt, *receipt["logs"]):
        if (
            quantity(obj["blockNumber"]) != block
            or chain_hex(obj["blockHash"], 32) != block_hash
            or quantity(obj["transactionIndex"]) != index
        ):
            raise ValueError("FEE_REFUND_RECEIPT_BLOCK_MISMATCH")
    logs = {}
    for log in receipt["logs"]:
        log_index = quantity(log["logIndex"])
        if log.get("removed") is not False or chain_hex(log["transactionHash"], 32) != tx_hash or log_index < 0:
            raise ValueError("FEE_REFUND_INVALID_LOG_IDENTITY")
        if log_index in logs:
            raise ValueError("FEE_REFUND_DUPLICATE_LOG")
        logs[log_index] = log
    owned = {
        (chain_hex(log["address"], 20), i)
        for i, log in logs.items()
        if len(log["topics"]) == 4
        and chain_hex(log["topics"][0], 32) in {V2_FILL_TOPIC, LEGACY_FILL_TOPIC}
        and chain_hex(log["topics"][2], 32) == "0x" + wallet[2:].zfill(64)
    }
    if len(owned) != len(fills) or owned != {(chain_hex(row["contract"], 20), int(row["log_index"])) for row in fills}:
        raise ValueError("FEE_REFUND_FILL_COVERAGE_MISMATCH")
    groups: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for row in fills:
        contract, log_index = chain_hex(row["contract"], 20), int(row["log_index"])
        log = logs[log_index]
        topic = chain_hex(log["topics"][0], 32)
        order = chain_hex(row["order_hash"], 32)
        v2 = contract in V2_EXCHANGES and topic == V2_FILL_TOPIC
        if not v2 and not (
            contract in FEE_MODULES and topic == LEGACY_FILL_TOPIC and chain_hex(tx["to"], 20) == FEE_MODULES[contract]
        ):
            raise ValueError("FEE_REFUND_DEPLOYMENT_UNVERIFIED")
        raw = words(log["data"], 7 if v2 else 5)
        side = row["side"]
        asset = "0" if v2 or side == "SELL" else str(row["token_id"])
        if (
            chain_hex(row["proxy_wallet"], 20) != wallet
            or chain_hex(row["transaction_hash"], 32) != tx_hash
            or int(row["block_number"]) != block
            or topic != row["event_topic"]
            or chain_hex(log["topics"][1], 32) != order
            or (v2 and raw[:2] != [0 if side == "BUY" else 1, int(row["token_id"])])
            or (not v2 and raw[:2] != ([0, int(row["token_id"])] if side == "BUY" else [int(row["token_id"]), 0]))
            or raw[2] != decimal_value(row["notional"] if side == "BUY" else row["size"]) * USDC_SCALE
            or raw[3] != decimal_value(row["size"] if side == "BUY" else row["notional"]) * USDC_SCALE
            or raw[4] != decimal_value(row["fee_raw"])
            or asset != row["fee_asset_id"]
        ):
            raise ValueError("FEE_REFUND_FILL_MISMATCH")
        groups.setdefault((contract, order, asset), []).append((log_index, raw[4]))
    refunds: dict[tuple[str, str, str], tuple[int, int]] = {}
    for log in logs.values():
        topics = log["topics"]
        if not topics or chain_hex(topics[0], 32) != REFUND_TOPIC:
            continue
        if len(topics) != 4:
            raise ValueError("FEE_REFUND_INVALID_EVENT")
        if chain_hex(topics[2], 32) != "0x" + wallet[2:].zfill(64):
            if chain_hex(topics[1], 32) in {key[1] for key in groups}:
                raise ValueError("FEE_REFUND_RECIPIENT_MISMATCH")
            continue
        asset_id, refund = words(log["data"], 2)
        matches = [
            key
            for key in groups
            if key[1:] == (chain_hex(topics[1], 32), str(asset_id))
            and FEE_MODULES.get(key[0]) == chain_hex(log["address"], 20)
        ]
        if len(matches) != 1 or refund <= 0:
            raise ValueError("FEE_REFUND_ATTRIBUTION_UNVERIFIED")
        key = matches[0]
        total_refund, total_charged = refunds.get(key, (0, 0))
        refunds[key] = (total_refund + refund, total_charged + int(chain_hex(topics[3], 32), 16))
    result = {}
    for key, items in groups.items():
        gross = sum(fee for _, fee in items)
        refund, charged = refunds.get(key, (0, gross))
        if refund + charged != gross:
            raise ValueError("FEE_REFUND_AMOUNT_MISMATCH")
        for log_index, fee in sorted(items):
            applied = min(refund, fee)
            result[log_index] = (key[2], Decimal(fee) / USDC_SCALE, Decimal(applied) / USDC_SCALE)
            refund -= applied
    return result
