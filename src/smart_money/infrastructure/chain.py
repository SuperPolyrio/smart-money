"""Bounded confirmed reads using the existing market-data RPC and fill decoder."""

from __future__ import annotations

import importlib
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from smart_money.infrastructure.local_history import read_token_markets
from smart_money.infrastructure.polymarket import PolymarketClient
from smart_money.markets.taxonomy import normalize_market
from smart_money.markets.trades import (
    CTF,
    TRANSFER_BATCH,
    TRANSFER_SINGLE,
    USDC_SCALE,
    chain_hex,
    transfer_changes,
    wallet_fill,
)


class HistoricalStateUnavailable(OSError):
    """The node explicitly reports unavailable historical state, not a transport failure."""


class ChainReader:
    def __init__(self) -> None:
        # Optional integration: do not import collectors into offline qualification or MAS.
        try:
            rpc = importlib.import_module("market_data.rpc")
            self.collector = importlib.import_module("market_data.orderfilled.collector")
        except ImportError as exc:
            raise ValueError("Install the existing market-data package to run monitoring") from exc
        urls = tuple(
            url.strip() for url in os.environ.get("POLYDATA_ORDERFILLED_RPC_URLS", "").split(",") if url.strip()
        )
        if not urls:
            raise ValueError("POLYDATA_ORDERFILLED_RPC_URLS is required")
        self.rpc = rpc.RpcClient(urls=urls, timeout=15)
        self.official = PolymarketClient()

    def call(self, method: str, params: list[Any]) -> Any:
        try:
            return self.rpc.call(method, params)
        except (OSError, RuntimeError, ValueError) as exc:
            # Upstream errors may contain private endpoint URLs.
            detail = str(exc).lower()
            if method == "eth_call" and (
                any(
                    message in detail
                    for message in (
                        "missing trie node",
                        "historical state unavailable",
                        "state is pruned",
                        "pruned state",
                    )
                )
                or re.search(r"\bhistorical state (?:0x)?[0-9a-f]{64} is not available\b", detail)
            ):
                raise HistoricalStateUnavailable("HISTORICAL_STATE_UNAVAILABLE") from None
            raise OSError(f"RPC_FAILED:{method}:{type(exc).__name__}") from None

    def block(self, number: int | str) -> dict[str, Any]:
        row = self.call("eth_getBlockByNumber", [hex(number) if isinstance(number, int) else number, False])
        block = {
            "number": int(row["number"], 16),
            "hash": chain_hex(row["hash"], 32),
            "timestamp": datetime.fromtimestamp(int(row["timestamp"], 16), timezone.utc).isoformat(),
        }
        if isinstance(number, int) and block["number"] != number:
            raise ValueError("BLOCK_NUMBER_MISMATCH")
        return block

    def finalized(self, max_age_seconds: int) -> dict[str, Any]:
        if int(self.call("eth_chainId", []), 16) != 137:
            raise ValueError("WRONG_CHAIN_EXPECTED_POLYGON_137")
        if self.call("eth_syncing", []) is not False:
            raise ValueError("POLYGON_NODE_SYNCING")
        head = self.block("finalized")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(head["timestamp"])).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise ValueError("FINALIZED_BLOCK_NOT_FRESH")
        return head

    def preflight(self, max_age_seconds: int) -> dict[str, Any]:
        """Exercise the same log/receipt adapter and a recent historical balance before scanning."""
        head = self.finalized(max_age_seconds)
        logs = self.call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(max(0, head["number"] - 15)),
                    "toBlock": hex(head["number"]),
                    "address": CTF,
                    "topics": [[TRANSFER_SINGLE, TRANSFER_BATCH]],
                }
            ],
        )
        if not isinstance(logs, list) or not logs:
            raise ValueError("NODE_PREFLIGHT_NO_CTF_SAMPLE")
        log = logs[-1]
        number = int(log["blockNumber"], 16)
        if not max(0, head["number"] - 15) <= number <= head["number"]:
            raise ValueError("NODE_PREFLIGHT_LOG_OUT_OF_RANGE")
        wallet = "0x" + chain_hex(log["topics"][3], 32)[-40:]
        if int(wallet, 16) == 0:
            wallet = "0x" + chain_hex(log["topics"][2], 32)[-40:]
        tokens = transfer_changes(log, wallet)
        transactions = self.transactions([wallet], number, number)
        if not tokens or not any(t["transaction_hash"] == log["transactionHash"].lower() for t in transactions):
            raise ValueError("NODE_PREFLIGHT_RECEIPT_UNVERIFIED")
        token = next(iter(tokens))
        self.balance(wallet, token, number - 1)
        if self.block(head["number"])["hash"] != head["hash"]:
            raise ValueError("NODE_PREFLIGHT_FINALIZED_CHANGED")
        return {
            "status": "READY",
            "finalized": head,
            "receipt_ref": log["transactionHash"],
            "balance_block": number - 1,
            "token": token,
        }

    def balance(self, wallet: str, token: str, block: int) -> Decimal:
        address = chain_hex(wallet, 20)[2:].zfill(64)
        asset = int(token)
        if not 0 < asset < 2**256:
            raise ValueError("INVALID_TOKEN_ID")
        value = self.call("eth_call", [{"to": CTF, "data": "0x00fdd58e" + address + f"{asset:064x}"}, hex(block)])
        return Decimal(int(chain_hex(value, 32), 16)) / USDC_SCALE

    def seed_tokens(self, wallet: str) -> list[str]:
        page = self.official.page(
            "/v2/positions",
            {"user": wallet, "status": "OPEN", "include_archived": "true", "filter_amount": 0, "limit": 1000},
        )
        if not page["coverage"]["complete"]:
            raise OSError("BASELINE_TOKEN_DISCOVERY_INCOMPLETE")
        return sorted({str(row["token_id"]) for row in page["rows"]})

    def markets(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        identities: dict[str, tuple[str, frozenset[str]]] = {}

        def accept(rows: list[dict[str, Any]], source: str) -> None:
            for row in rows:
                market = normalize_market(row)
                if market is None:
                    continue
                for token in set(tokens) & set(market.token_ids):
                    identity = (market.condition_id, frozenset(market.token_ids))
                    if token in identities and identities[token] != identity:
                        raise ValueError("AMBIGUOUS_TOKEN_MARKET")
                    identities[token] = identity
                    if (
                        len(set(market.token_ids)) != len(market.token_ids)
                        or len(market.outcomes) != len(market.token_ids)
                        or not all(isinstance(outcome, str) and outcome.strip() for outcome in market.outcomes)
                    ):
                        continue  # Incomplete local metadata must still reach the official fallback.
                    if token in result and normalize_market(result[token]["payload"]) != market:
                        raise ValueError("AMBIGUOUS_TOKEN_MARKET")
                    result[token] = {
                        "payload": row,
                        "source": source,
                        "obtained_at": datetime.now(timezone.utc).isoformat(),
                    }

        if not tokens:
            return result
        try:
            accept(read_token_markets(tokens), "core.market_tokens+core.markets")
        except OSError:
            pass  # Official identity lookup is the documented metadata fallback.
        for token in tokens:
            if token not in result:
                try:
                    rows = self.official.get("https://gamma-api.polymarket.com/markets", {"clob_token_ids": token})
                except OSError:
                    continue  # Keep successful mappings; missing tokens remain in the monitor's pending set.
                if not isinstance(rows, list):
                    raise ValueError("INVALID_MARKET_RESPONSE")
                accept(rows, "gamma:markets")
        return result

    def transactions(self, wallets: list[str], start: int, end: int) -> list[dict[str, Any]]:
        hits: dict[tuple[str, int], dict[str, Any]] = {}
        for offset in range(0, len(wallets), 20):
            owners = ["0x" + chain_hex(w, 20)[2:].zfill(64) for w in wallets[offset : offset + 20]]
            for contracts, topics in (
                (self.collector.EXCHANGES, [self.collector.LEGACY_TOPIC, self.collector.V2026_TOPIC]),
                ((CTF,), [TRANSFER_SINGLE, TRANSFER_BATCH]),
            ):
                for position in (2, 3):
                    filters: list[Any] = [topics, None, owners] if position == 2 else [topics, None, None, owners]
                    logs = self.call(
                        "eth_getLogs",
                        [{"fromBlock": hex(start), "toBlock": hex(end), "address": list(contracts), "topics": filters}],
                    )
                    if not isinstance(logs, list):
                        raise ValueError("INVALID_LOG_RESPONSE")
                    for log in logs:
                        key = (chain_hex(log["transactionHash"], 32), int(log["logIndex"], 16))
                        if not start <= int(log["blockNumber"], 16) <= end or log.get("removed") is not False:
                            raise ValueError("INVALID_CONFIRMED_LOG")
                        if key in hits and hits[key] != log:
                            raise ValueError("CONFLICTING_LOG")
                        hits[key] = log
        headers = {}
        result = []
        for tx_hash in sorted({key[0] for key in hits}):
            receipt = self.call("eth_getTransactionReceipt", [tx_hash])
            tx = self.call("eth_getTransactionByHash", [tx_hash])
            number = int(receipt["blockNumber"], 16)
            if number not in headers:
                headers[number] = self.block(number)
            header = headers[number]
            if not start <= number <= end or int(receipt["status"], 16) != 1:
                raise ValueError("RECEIPT_NOT_CONFIRMED")
            if chain_hex(tx["hash"], 32) != tx_hash or int(tx["chainId"], 16) != 137:
                raise ValueError("TRANSACTION_IDENTITY_MISMATCH")
            logs_by_index = {}
            for obj in (tx, receipt, *receipt["logs"]):
                if (
                    int(obj["blockNumber"], 16) != number
                    or chain_hex(obj["blockHash"], 32) != header["hash"]
                    or obj["transactionIndex"] != receipt["transactionIndex"]
                ):
                    raise ValueError("RECEIPT_BLOCK_MISMATCH")
            for log in receipt["logs"]:
                index = int(log["logIndex"], 16)
                if (
                    log.get("removed") is not False
                    or chain_hex(log["transactionHash"], 32) != tx_hash
                    or index in logs_by_index
                ):
                    raise ValueError("INVALID_RECEIPT_LOG")
                logs_by_index[index] = log
            if chain_hex(receipt["transactionHash"], 32) != tx_hash or any(
                logs_by_index.get(i) != log for (t, i), log in hits.items() if t == tx_hash
            ):
                raise ValueError("RECEIPT_LOG_COVERAGE_MISMATCH")
            fills = []
            for log in receipt["logs"]:
                if (
                    log["address"].lower() not in self.collector.EXCHANGES
                    or not log["topics"]
                    or log["topics"][0] not in {self.collector.LEGACY_TOPIC, self.collector.V2026_TOPIC}
                ):
                    continue
                raw = self.collector.decode_log(log)
                if raw["maker"] in wallets:
                    raw.update(
                        transaction_hash=raw["tx_hash"],
                        maker_amount_filled=raw["maker_amount"],
                        taker_amount_filled=raw["taker_amount"],
                    )
                    fills.append(wallet_fill(raw, raw["maker"], header["timestamp"]))
            result.append(
                {
                    "transaction_hash": tx_hash,
                    "chain_id": 137,
                    "block_number": number,
                    "block_hash": header["hash"],
                    "block_time": header["timestamp"],
                    "transaction_json": tx,
                    "receipt_json": receipt,
                    "fills": fills,
                    "source": "polygon_rpc:confirmed_receipt",
                }
            )
        return sorted(result, key=lambda r: (r["block_number"], int(r["receipt_json"]["transactionIndex"], 16)))

    def close(self) -> None:
        self.rpc.session.close()
        self.official.close()
