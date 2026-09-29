"""The wallet screening contract: evidence, event denominators and durable refresh."""

import base64
import fcntl
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import zstandard

from smart_money.cli import main
from smart_money.infrastructure.chain import ChainReader, HistoricalStateUnavailable
from smart_money.infrastructure.local_history import (
    _PSQL,
    read_cutoff_boundary,
    read_local_operations,
    read_local_receipts,
    read_resolutions,
)
from smart_money.infrastructure.polymarket import PolymarketClient
from smart_money.infrastructure.wallet_storage import (
    Archive,
    archive_for,
    load_evaluation,
    load_history,
    migrate_watchlist,
    read_watchlist,
    write_watchlist,
)
from smart_money.markets.trades import (
    CTF,
    LEGACY_FILL_TOPIC,
    REFUND_TOPIC,
    TRANSACTION_SOURCE,
    TRANSFER_BATCH,
    TRANSFER_SINGLE,
    V2_FILL_TOPIC,
    transfer_changes,
)
from smart_money.monitor import _replay_balance, run_monitor
from smart_money.research.engine import MasEngine
from smart_money.wallets.directional_expert_policy import decide_directional_expert
from smart_money.wallets.discovery import discover_candidates
from smart_money.wallets.history import evaluate_history
from smart_money.watchlist import _allocate_monitoring, refresh_watchlist, write_json

WALLET = "0x" + "ab" * 20
ALIAS = "0x" + "cd" * 20
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Qualification expiry must be tested against a fixed clock, not the test machine's date."""
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr("smart_money.watchlist.datetime", clock)
    monkeypatch.setattr("smart_money.infrastructure.polymarket.datetime", clock)


def local_operation(row):
    return {
        **row,
        "log_index": 1,
        "block_number": 500,
        "source": "fixture:ctf-log",
        "source_contract": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "parent_collection_id": "0x" + "0" * 64,
        "partition": [1, 2],
        "collateral_token": "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",
    }


def history():
    result = {
        "wallet": WALLET,
        "as_of": AS_OF,
        "closed_positions": [],
        "open_positions": [],
        "trades": [],
        "activity": [],
        "markets": {},
        "resolutions": {},
        "gaps": [],
        "coverage": {name: {"complete": True} for name in ("closed_positions", "open_positions", "trades", "activity")},
        "local_history": {
            "wallet": WALLET,
            "as_of": AS_OF,
            "source": "fixture:chain-receipts",
            "trades": [],
            "coverage": {
                "boundary": {
                    "block_number": 1000,
                    "block_time": AS_OF,
                    "block_hash": "0x" + "11" * 32,
                    "next_block_number": 1001,
                    "next_block_time": (NOW + timedelta(seconds=2)).isoformat(),
                    "next_block_hash": "0x" + "22" * 32,
                    "source": "fixture:block-headers",
                },
                "orderfilled": [[1, 1000]],
                "activity": [[1, 1000]],
            },
        },
    }
    for i in range(20):
        condition, token = f"0x{i:064x}", str(i * 2)
        won = i < 14
        settled = NOW - timedelta(days=i + 1)
        trade_time = int((settled - timedelta(days=2)).timestamp())
        position = {
            "proxy_wallet": WALLET,
            "condition_id": condition,
            "token_id": token,
            "outcome_index": 0,
            "current_size": 20 if i == 0 else 0,
            "current_value": 20 if i == 0 else 0,
            "current_price": 1 if won else 0,
            "entry_cost_usdc": 10 if i == 0 else 0,
            "realized_pnl": 0 if i == 0 else 10 if won else -5,
            "unrealized_pnl": 10 if i == 0 else 0,
            "last_event_at": trade_time,
        }
        result["open_positions" if i == 0 else "closed_positions"].append(position)
        result["trades"].append(
            {
                "proxy_wallet": WALLET,
                "condition_id": condition,
                "token_id": token,
                "outcome_index": 0,
                "side": "BUY",
                "price": 0.5,
                "size": 20 if won else 10,
                "timestamp": trade_time,
                "transaction_hash": f"0x{i + 100:064x}",
                "log_index": 0,
            }
        )
        result["local_history"]["trades"].append(
            {
                **result["trades"][-1],
                "notional": 10 if won else 5,
                "fee_usdc": "0",
                "block_number": i + 100,
                "contract": "0x" + "ef" * 20,
                "trade_id": f"chain:{i}",
            }
        )
        result["activity"].append({**result["trades"][-1], "type": "TRADE", "usdc_size": 10 if won else 5})
        if i:
            result["activity"].append(
                {
                    "proxy_wallet": WALLET,
                    "condition_id": condition,
                    "token_id": token,
                    "type": "REDEEM",
                    "timestamp": int(settled.timestamp()) + 1,
                    "size": 20 if won else 10,
                    "usdc_size": 20 if won else 0,
                    "transaction_hash": f"redemption:{i}",
                }
            )
        result["markets"][condition] = {
            "conditionId": condition,
            "question": f"Will Bitcoin exceed target {i}?",
            "category": "crypto",
            "events": [{"id": str(i + 1)}],
            "closed": True,
            "clobTokenIds": [token, str(i * 2 + 1)],
        }
        result["resolutions"][condition] = {
            "condition_id": condition,
            "status": "RESOLVED",
            "payouts": [1, 0] if won else [0, 1],
            "resolved_at": settled.isoformat(),
            "resolved_block": i + 1,
            "transaction_hash": f"settlement-{i}",
        }
    result["local_history"]["operations"] = [
        local_operation(row) for row in result["activity"] if row["type"] == "REDEEM"
    ]
    return result


def packet():
    return {
        "as_of": AS_OF,
        "profiles": {ALIAS: {"wallet": WALLET, "evidence": {"public_profile": {"proxyWallet": WALLET}}}},
        "candidates": [{"account": ALIAS.upper(), "source": "local:repeated_events", "sector_id": "CRYPTO"}],
        "boards": [
            {
                "category": "crypto",
                "period": period,
                "rows": [{"user_id": ALIAS, "pnl": 100, "rank": 1}],
                "coverage": {"complete": True},
            }
            for period in ("week", "month", "all")
        ],
        "histories": {WALLET: history()},
    }


@pytest.mark.parametrize("failure", ["pagination", "metadata"])
def test_real_command_deduplicates_and_preserves_old_evidence_on_failure(tmp_path, capsys, failure):
    source, destination = tmp_path / "input.json", tmp_path / "watchlist.json"
    data = packet()
    # Actual duplicate records, including a still-unredeemed settled winner.
    facts = data["histories"][WALLET]
    facts["closed_positions"].append(deepcopy(facts["closed_positions"][0]))
    facts["trades"].append(deepcopy(facts["trades"][0]))
    source.write_text(json.dumps(data))
    args = ["watchlist", "--input", str(source), "--output", str(destination), "--offline", "--categories", "crypto"]
    assert main(args) == 0
    first = json.loads(destination.read_text())
    record = first["records"][f"{WALLET}:CRYPTO"]
    events = load_evaluation(first, record["evaluation_ref"], detail=True)["profiles"]["CRYPTO"]["events"]
    assert len(events) == 20 and "events" not in record
    assert first["summary"]["candidates"] == 1
    assert first["summary"]["listening_wallets"] == [WALLET]
    assert len(first["candidates"][0]["sources"]) == 4
    assert record["historical_eligible"] and record["forward_status"] == "SHADOW_OBSERVE"
    assert load_evaluation(first, record["evaluation_ref"], detail=True)["reconciliation"]["complete"]
    assert record["metrics"]["directional_events"] == 20
    assert record["metrics"]["win_rate"] == 0.7
    assert record["metrics"]["sector_pnl"] == 110
    assert record["metrics"]["profit_factor"] == pytest.approx(140 / 30)
    assert main(args) == 0
    second = json.loads(destination.read_text())
    assert second["records"] == first["records"]
    assert load_evaluation(second, record["evaluation_ref"], detail=True) == load_evaluation(
        first, record["evaluation_ref"], detail=True
    )
    assert all(board["period"] != "all" for board in second["discovery"])
    data["boards"] = []  # Dropping out of a board is not deletion.
    if failure == "pagination":
        facts["coverage"]["trades"] = {"complete": False, "error": "timeout"}
    else:
        facts["fetch_errors"] = ["MARKET_METADATA_UNAVAILABLE"]
    source.write_text(json.dumps(data))
    capsys.readouterr()
    assert main(args) == 0
    failed = json.loads(destination.read_text())["records"][f"{WALLET}:CRYPTO"]
    assert failed["stale"] and not failed["historical_eligible"] and not failed["monitor"]["enabled"]
    assert failed["metrics"] == record["metrics"]
    assert failed["last_successful_evaluation_ref"] == record["evaluation_ref"]
    assert failed["first_discovered_at"] == AS_OF
    assert len(failed["changes"]) == 2
    assert failed["changes"][-1]["previous_evaluation_ref"] == record["evaluation_ref"]
    assert json.loads(capsys.readouterr().out)["stale"] == 2


def test_shared_facts_survive_incremental_receipt_repair(tmp_path):
    destination = tmp_path / "watchlist.json"
    data = packet()
    native = fee_history()
    data["histories"][WALLET] = deepcopy(native)
    data["histories"][WALLET]["local_history"]["receipts"] = {}
    first = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    data["candidates"].append({"account": ALIAS, "sector_id": "POLITICS", "source": "local:politics"})
    second = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    first_ref = next(iter(first["records"].values()))["evaluation_ref"]
    first_history = load_evaluation(first, first_ref)["history_ref"]
    assert all(
        load_evaluation(second, r["evaluation_ref"])["history_ref"] == first_history for r in second["records"].values()
    )
    archive = archive_for(second)
    manifest_before = archive.get(first_history)
    data["histories"][WALLET] = native
    repaired = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    latest = load_evaluation(repaired, repaired["records"][f"{WALLET}:CRYPTO"]["evaluation_ref"])["history_ref"]
    manifest_after = archive.get(latest)
    assert manifest_before["fields"]["trades"] == manifest_after["fields"]["trades"]
    assert (
        manifest_before["fields"]["local_history"]["fields"]["trades"]
        == manifest_after["fields"]["local_history"]["fields"]["trades"]
    )
    assert load_evaluation(repaired, first_ref, detail=True) == load_evaluation(first, first_ref, detail=True)
    record = repaired["records"][f"{WALLET}:CRYPTO"]
    assert record["historical_eligible"]
    reference = load_evaluation(repaired, record["evaluation_ref"], detail=True)["history_ref"]
    restored = load_history(repaired, reference)
    assert restored == native
    restored["local_history"]["receipts"].clear()
    assert load_history(repaired, reference) == native  # Consumers cannot mutate saved evidence.
    before = destination.read_bytes()
    refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    assert destination.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing_fact", "changed_fact", "changed_manifest"])
def test_broken_fact_references_block_repair_without_losing_old_audit(tmp_path, fault):
    destination = tmp_path / "watchlist.json"
    state = refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    record = state["records"][f"{WALLET}:CRYPTO"]
    reference = load_evaluation(state, record["evaluation_ref"], detail=True)["history_ref"]
    archive = archive_for(state)
    manifest = archive.get(reference)
    damaged = reference if fault == "changed_manifest" else manifest["fields"]["trades"]["blocks"][0]
    path = archive._path(damaged.split(":")[1])
    if fault == "missing_fact":
        path.unlink()
    else:
        path.write_bytes(zstandard.ZstdCompressor().compress(b"[]"))
    failed = refresh_watchlist(destination, repair=True)
    current = failed["records"][f"{WALLET}:CRYPTO"]
    assert current["stale"] and not current["historical_eligible"] and not current["monitor"]["enabled"]
    assert (
        "CORRUPT_ARCHIVE_BLOCK" in current["reasons"][0]
        if fault != "missing_fact"
        else "No such file" in current["reasons"][0]
    )
    assert current["last_successful_evaluation_ref"] == record["last_successful_evaluation_ref"]
    assert load_evaluation(failed, record["evaluation_ref"], detail=True) == load_evaluation(
        state, record["evaluation_ref"], detail=True
    )


@pytest.mark.parametrize(
    "case,reason",
    [
        ("coverage", "WALLET_HISTORY_COVERAGE_UNVERIFIED"),
        ("pagination", "INCOMPLETE_FEED:trades"),
        ("metadata", "MARKET_METADATA_UNAVAILABLE"),
        ("fees", "NET_FEE_BASIS_UNVERIFIED"),
        ("balance", "POSITION_BALANCE_MISMATCH"),
        ("receipt_gap", "LOCAL_SCAN_GAP:orderfilled"),
        ("missing_boundary", "LOCAL_CUTOFF_BLOCK_UNVERIFIED"),
        ("sparse_boundary", "LOCAL_CUTOFF_BLOCK_UNVERIFIED"),
        ("local_identity", "LOCAL_HISTORY_IDENTITY_OR_CUTOFF_MISMATCH"),
        ("missing_local_fill", "TRADE_QUANTITY_MISMATCH"),
        ("redeem_value", "REDEMPTION_CASH_MISMATCH"),
        ("concentrated", "PROFIT_CONCENTRATED"),
        ("no_losses", "PF_WITHOUT_LOSS_SAMPLE:profit_factor"),
        ("early_exit", "EARLY_EXIT_STYLE_REQUIRES_REVIEW"),
        ("future", "POSITION_AFTER_CUTOFF"),
        ("valuation", "SETTLED_POSITION_MARK_MISMATCH"),
        ("related", "MULTI_MARKET_EVENT_ATTRIBUTION_UNRESOLVED"),
        ("missing_resolution", "CLOSED_MARKET_WITHOUT_FORMAL_SETTLEMENT"),
    ],
)
def test_quality_review_never_auto_promotes_unresolved_history(case, reason):
    facts = history()
    if case == "coverage":
        facts["local_history"] = {}
        facts["reconciliation"] = {"complete": True, "fees_included": True}  # A supplied pass cannot bypass facts.
    elif case == "pagination":
        facts["coverage"]["trades"]["complete"] = False
    elif case == "metadata":
        facts["fetch_errors"] = ["MARKET_METADATA_UNAVAILABLE"]
    elif case == "fees":
        facts["local_history"]["trades"][0]["fee_usdc"] = None
    elif case == "balance":
        facts["open_positions"][0]["current_size"] = 21
    elif case == "receipt_gap":
        facts["local_history"]["coverage"]["orderfilled"] = [[1, 99], [101, 1000]]
    elif case == "missing_boundary":
        facts["local_history"]["coverage"].pop("boundary")
    elif case == "sparse_boundary":
        facts["local_history"]["coverage"]["boundary"]["next_block_number"] = 1002
    elif case == "local_identity":
        facts["local_history"]["wallet"] = ALIAS
    elif case == "missing_local_fill":
        facts["local_history"]["trades"].pop()
    elif case == "redeem_value":
        facts["activity"][2]["usdc_size"] = 99
    elif case == "concentrated":
        for row in facts["closed_positions"]:
            row["realized_pnl"] = -1
        facts["closed_positions"][0]["realized_pnl"] = 100
    elif case == "no_losses":
        for row in facts["closed_positions"]:
            row["realized_pnl"] = 1
    elif case == "early_exit":
        facts["trades"].append({**facts["trades"][1], "side": "SELL", "log_index": 1, "price": 0.8})
        facts["resolutions"][facts["trades"][1]["condition_id"]]["payouts"] = [0, 1]
    elif case == "future":
        facts["open_positions"][0]["last_event_at"] = int((NOW + timedelta(days=1)).timestamp())
    elif case == "valuation":
        facts["open_positions"][0]["current_price"] = 0.8
    elif case == "related":
        list(facts["markets"].values())[1]["events"] = [{"id": "1"}]
    elif case == "missing_resolution":
        facts["resolutions"] = {}
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]
    assert not result["historical_eligible"] and result["status"] == "PENDING_REVIEW"
    assert reason in result["reasons"]
    if case == "early_exit":
        assert result["metrics"]["win_rate"] == 0.65  # Profitable exit is still a direction loss.
        assert result["metrics"]["sector_pnl"] == 110
    if case == "related":
        assert result["metrics"]["directional_events"] == 19


def test_two_sided_event_excluded_from_all_directional_economics():
    facts = history()
    opposite = {**facts["trades"][0], "token_id": "1", "outcome_index": 1, "log_index": 1}
    facts["trades"].append(opposite)
    facts["closed_positions"].append(
        {
            **facts["closed_positions"][0],
            "condition_id": opposite["condition_id"],
            "token_id": "1",
            "outcome_index": 1,
            "realized_pnl": -100,
        }
    )
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]
    assert result["metrics"]["directional_events"] == 19
    assert result["metrics"]["win_rate"] == pytest.approx(13 / 19)
    assert result["metrics"]["sector_pnl"] == 100
    assert result["metrics"]["two_sided_ratio"] == 0.05
    assert sum(event["pnl"] for event in result["events"]) == 10  # Full audit PnL retained.


def test_cursor_walk_keeps_filters_and_detects_failed_or_cyclic_page():
    client = PolymarketClient(max_pages=5)
    try:
        client.get = Mock(
            side_effect=[
                {"data": [{"n": 1}], "pagination": {"next_cursor": "next", "has_more": True}},
                {"data": [{"n": 2}], "pagination": {"next_cursor": None, "has_more": False}},
            ]
        )
        params = {"user": WALLET, "start": 1, "end": 123, "taker_only": "false"}
        result = client.page("/v2/trades", params)
        assert result["coverage"]["complete"] and result["rows"] == [{"n": 1}, {"n": 2}]
        assert client.get.call_args.args[1] == {**params, "cursor": "next"}
        client.get = Mock(
            side_effect=[
                {"data": [{"n": 1}], "pagination": {"next_cursor": "next", "has_more": True}},
                OSError("timeout"),
            ]
        )
        result = client.page("/v2/trades", params)
        assert not result["coverage"]["complete"] and result["rows"] == [{"n": 1}]
        client.get = Mock(return_value={"data": [{"n": 1}], "pagination": {"next_cursor": "next", "has_more": True}})
        assert client.page("/v2/trades", params)["coverage"]["error"] == "CURSOR_STALLED"
    finally:
        client.close()


def native_history_sources(monkeypatch, facts):
    """Serve the captured source rows through the real paged acquisition contract."""

    def get(self, url, params):
        feed = (
            "open_positions"
            if params.get("status") == "OPEN"
            else "closed_positions"
            if url.endswith("positions")
            else url.rsplit("/", 1)[-1]
        )
        selected = set(params.get("condition", "").split(",")) if params.get("condition") else None
        rows = [
            r
            for r in facts[feed]
            if (selected is None or r.get("condition_id") in selected)
            and (
                feed.endswith("positions")
                or params.get("start", 0) <= r["timestamp"] <= params.get("end", float("inf"))
            )
        ]
        return {"data": deepcopy(rows), "pagination": {"has_more": False, "next_cursor": None}}

    def local(wallet, as_of, conditions, **kwargs):
        result = deepcopy(facts["local_history"])
        result.update(
            as_of=as_of,
            markets={c: facts["markets"][c] for c in conditions if c in facts["markets"]},
            resolutions={c: facts["resolutions"][c] for c in conditions if c in facts["resolutions"]},
            issues=[],
        )
        if kwargs.get("targets") == []:
            result["trades"] = []
        return result

    monkeypatch.setattr(PolymarketClient, "get", get)
    monkeypatch.setattr(
        "smart_money.infrastructure.polymarket.read_wallet_event_index",
        Mock(
            return_value={
                "conditions": sorted(facts["markets"]),
                "unmapped_tokens": 0,
                "source": "fixture:participation_index",
            }
        ),
    )
    reader = Mock(side_effect=local)
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_history", reader)
    monkeypatch.setattr(
        "smart_money.infrastructure.polymarket.read_local_operations",
        Mock(return_value=facts["local_history"]["operations"]),
    )
    monkeypatch.setattr(
        "smart_money.infrastructure.polymarket.read_cutoff_boundary",
        Mock(return_value=facts["local_history"]["coverage"]["boundary"]),
    )
    return reader


@pytest.mark.parametrize("case", ["complete", "local_failure", "yield"])
def test_history_reuses_local_metadata_and_records_source_failure(monkeypatch, case):
    facts = history()
    reader = native_history_sources(monkeypatch, facts)
    client = PolymarketClient()
    if case == "local_failure":
        reader.side_effect = OSError("unavailable")
        monkeypatch.setattr("smart_money.infrastructure.polymarket.time.time", lambda: NOW.timestamp())
    if case == "yield":
        from dataclasses import replace

        client.settings = replace(client.settings, slice_requests=2)
    try:
        captured = client.history(WALLET, AS_OF)
        if case == "local_failure":
            assert captured["work"]["outcome"] == "SOURCE_ERROR"
            assert (
                captured["work"]["next_attempt_at"]
                and not evaluate_history(captured, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]["historical_eligible"]
            )
            for attempt in (2, 3):
                captured = client.history(WALLET, AS_OF, previous=captured)
                assert captured["work"]["retries"] == attempt
            assert captured["work"]["next_attempt_at"] == (NOW + timedelta(days=1)).isoformat()
            return
        slices = 1
        while captured["work"]["phase"] != "DONE" and slices < 10:
            assert captured["work"]["outcome"] == "YIELDED"
            captured = client.history(WALLET, AS_OF, previous=captured)
            slices += 1
        assert captured["work"]["phase"] == "DONE"
        assert len(captured["trades"]) == 20
        assert evaluate_history(captured, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]["historical_eligible"]
        assert captured["selection"]["start"] == (NOW - timedelta(days=180)).isoformat()
        assert (slices > 1) is (case == "yield")
    finally:
        client.close()


@pytest.mark.parametrize("sqlstate,attempts", [("57014", 1), ("28P01", 1)])
def test_local_query_defers_retries_without_exposing_credentials(monkeypatch, capsys, sqlstate, attempts):
    payload = {"password": "private", "port": "45432", "user": "reader", "database": "facts", "sql": "SELECT 1"}
    run = Mock(
        side_effect=[
            Mock(returncode=1, stdout="", stderr=f"ERROR: {sqlstate}"),
            Mock(returncode=0, stdout="{}", stderr=""),
        ]
    )
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
    monkeypatch.setattr("subprocess.run", run)
    with pytest.raises(SystemExit) as ended:
        exec(_PSQL, {})
    assert run.call_count == attempts
    assert ended.value.code == (0 if attempts == 2 else 1)
    assert capsys.readouterr().out == ("{}" if attempts == 2 else sqlstate)
    assert "private" not in str(run.call_args.args)
    assert "default_transaction_read_only=on" in run.call_args.kwargs["env"]["PGOPTIONS"]


def test_discovery_budget_mapping_and_external_evaluation_exclusion():
    resolve = Mock(side_effect=lambda account: {"wallet": account, "evidence": "official:public-profile"})
    board = {
        "category": "crypto",
        "period": "week",
        "rows": [{"user_id": f"0x{i:040x}", "pnl": 10} for i in range(1, 52)],
    }
    rows = discover_candidates([], [board], [], as_of=AS_OF, resolve=resolve)
    assert {row["wallet"] for row in rows} == {f"0x{i:040x}" for i in range(1, 51)}
    assert resolve.call_count == 50
    assert "historical_eligible" not in rows[0]
    with pytest.raises(ValueError, match="PolyBeats"):
        discover_candidates(
            [], [], [{"account": ALIAS, "sector_id": "CRYPTO", "source": "polybeats"}], as_of=AS_OF, resolve=resolve
        )


def test_manual_pause_and_forward_state_survive_refresh(tmp_path):
    destination = tmp_path / "watchlist.json"
    first = refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    record = first["records"][f"{WALLET}:CRYPTO"]
    record.update(manual_paused=True, forward_status="FORWARD_VALIDATED")
    record["events"] = load_evaluation(first, record["evaluation_ref"], detail=True)["profiles"]["CRYPTO"]["events"]
    destination.write_text(json.dumps(first))
    second = refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    record = second["records"][f"{WALLET}:CRYPTO"]
    assert record["historical_eligible"] and record["forward_status"] == "FORWARD_VALIDATED"
    assert record["manual_paused"] and not record["monitor"]["enabled"]
    assert "events" not in record
    assert len(load_evaluation(second, record["evaluation_ref"], detail=True)["profiles"]["CRYPTO"]["events"]) == 20


def test_monitor_allocation_expiry_and_new_evidence_preserve_old_qualification(tmp_path):
    state = refresh_watchlist(tmp_path / "watchlist.json", inputs=packet(), offline=True, categories=["crypto"])
    candidate = state["candidates"][0]
    old_evaluations = {
        r["evaluation_ref"]: load_evaluation(state, r["evaluation_ref"], detail=True) for r in state["records"].values()
    }
    first_seen = candidate["first_discovered_at"]
    trade = deepcopy(history()["trades"][0])
    candidate["summary_ref"] = archive_for(state).put({"status": "READY", "recent_trade": trade, "as_of": AS_OF})
    expired = (NOW + timedelta(days=2)).isoformat()
    _allocate_monitoring(state, expired)
    assert all(r["stale"] and r["monitor"]["mode"] == "EXPLORE" for r in state["records"].values())
    until = candidate["exploration"]["until"]
    _allocate_monitoring(state, until)
    assert not any(r["monitor"]["enabled"] for r in state["records"].values())
    trade["timestamp"] = int(datetime.fromisoformat(until).timestamp()) - 1
    candidate["summary_ref"] = archive_for(state).put({"status": "READY", "recent_trade": trade, "as_of": until})
    _allocate_monitoring(state, until)
    assert all(r["monitor"]["mode"] == "EXPLORE" for r in state["records"].values())
    _allocate_monitoring(state, (NOW + timedelta(days=40)).isoformat())
    assert all(r["archived"] and not r["monitor"]["enabled"] for r in state["records"].values())
    assert all(load_evaluation(state, ref, detail=True) == value for ref, value in old_evaluations.items())
    assert candidate["first_discovered_at"] == first_seen


def test_unresolved_identity_merges_into_existing_wallet_and_weekly_recall(tmp_path):
    destination = tmp_path / "watchlist.json"
    data = packet()
    data["profiles"] = {}
    first = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    assert first["candidates"][0]["mapping_status"] == "UNRESOLVED"
    assert not first["summary"]["listening_wallets"]
    second = refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    assert len(second["candidates"]) == 1
    assert all(row["wallet"] == WALLET for row in second["records"].values())
    assert len(second["records"][f"{WALLET}:CRYPTO"]["changes"]) == 2
    later = packet()
    later["as_of"] = (NOW + timedelta(days=7)).isoformat()
    later["histories"] = {}
    third = refresh_watchlist(destination, inputs=later, offline=True, categories=["crypto"])
    assert {board["period"] for board in third["discovery"]} == {"week", "month", "all"}
    assert third["candidates"][0]["first_discovered_at"] == AS_OF


@pytest.mark.parametrize("seconds_outside,expected", [(0, 20), (1, 19)])
def test_recent_window_includes_exact_90_day_boundary(seconds_outside, expected):
    facts = history()
    condition = facts["trades"][-1]["condition_id"]
    settlement = NOW - timedelta(days=90, seconds=seconds_outside)
    facts["resolutions"][condition]["resolved_at"] = settlement.isoformat()
    facts["trades"][-1]["timestamp"] = int((settlement - timedelta(days=2)).timestamp())
    facts["closed_positions"][-1]["last_event_at"] = facts["trades"][-1]["timestamp"]
    facts["local_history"]["trades"][-1]["timestamp"] = facts["trades"][-1]["timestamp"]
    facts["activity"][-2]["timestamp"] = facts["trades"][-1]["timestamp"]
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]
    assert result["historical_eligible"]
    assert result["metrics"]["directional_events"] == 20
    assert result["metrics"]["recent_events"] == expected


def fee_history(version="v2", side="BUY", refund=0):
    facts = history()
    fill = facts["local_history"]["trades"][1]
    if side == "SELL":
        sale = {**facts["trades"][1], "side": "SELL", "price": 1, "transaction_hash": "0x" + "33" * 32, "log_index": 1}
        facts["trades"].append(sale)
        fill = {**fill, **sale, "trade_id": "sale:1", "notional": 20}
        facts["local_history"]["trades"].append(fill)
        facts["activity"][2] = {**sale, "type": "TRADE", "usdc_size": 19.75 + refund / 1000000}
        facts["local_history"]["operations"].pop(0)
    else:
        facts["activity"][1]["usdc_size"] = 10.25 - refund / 1000000 if version == "v2" else 10
    facts["closed_positions"][0]["realized_pnl"] = 9.75 + refund / 1000000
    share_fee = version == "legacy" and side == "BUY"
    if share_fee:
        facts["activity"][2].update(size=19.75 + refund / 1000000, usdc_size=19.75 + refund / 1000000)
        facts["local_history"]["operations"][0] = local_operation(facts["activity"][2])
    exchange = (
        "0xe111180000d2663c0091e4f400237545b87b996b"
        if version == "v2"
        else "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
    )
    module = "0xe3f18acc55091e2c48d883fc8c8413319d4ab7b0"
    fill.update(
        contract=exchange,
        order_hash="0x" + "44" * 32,
        fee_raw="250000",
        fee_asset_id="2" if share_fee else "0",
        event_topic=V2_FILL_TOPIC if version == "v2" else LEGACY_FILL_TOPIC,
        fee_usdc=None if share_fee else "0.25",
    )
    tx_hash = fill["transaction_hash"]
    identity = {
        "blockNumber": hex(fill["block_number"]),
        "blockHash": "0x" + "55" * 32,
        "transactionIndex": "0x0",
        "transactionHash": tx_hash,
    }
    tx = {**identity, "hash": tx_hash, "chainId": "0x89", "from": WALLET, "to": exchange if version == "v2" else module}
    first_words = [0 if side == "BUY" else 1, 2] if version == "v2" else [0, 2] if side == "BUY" else [2, 0]
    amounts = [10000000, 20000000] if side == "BUY" else [20000000, 20000000]
    data = first_words + amounts + [250000] + ([0, 0] if version == "v2" else [])
    logs = [
        {
            **identity,
            "removed": False,
            "logIndex": hex(fill["log_index"]),
            "address": exchange,
            "topics": [fill["event_topic"], fill["order_hash"], "0x" + WALLET[2:].zfill(64), "0x" + "00" * 32],
            "data": "0x" + "".join(f"{n:064x}" for n in data),
        }
    ]
    if refund:
        logs.append(
            {
                **identity,
                "removed": False,
                "logIndex": "0x2",
                "address": module,
                "topics": [REFUND_TOPIC, fill["order_hash"], "0x" + WALLET[2:].zfill(64), f"0x{250000 - refund:064x}"],
                "data": f"0x{2 if share_fee else 0:064x}{refund:064x}",
            }
        )
    facts["local_history"]["receipts"] = {
        tx_hash: {
            "transaction_hash": tx_hash,
            "chain_id": 137,
            "block_number": fill["block_number"],
            "block_time": datetime.fromtimestamp(fill["timestamp"], timezone.utc).isoformat(),
            "source": TRANSACTION_SOURCE,
            "transaction_json": tx,
            "receipt_json": {**tx, "status": "0x1", "logs": logs},
        }
    }
    return facts


@pytest.mark.parametrize(
    "version,side,refund",
    [("v2", "BUY", 0), ("v2", "SELL", 0), ("legacy", "SELL", 0), ("legacy", "SELL", 100000), ("legacy", "BUY", 100000)],
)
def test_receipt_backed_net_fees_match_cash_and_positions_once(version, side, refund):
    facts = fee_history(version, side, refund)
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert result["complete"] and result["fees_included"], result
    assert result["fee_transactions_required"] == result["fee_transactions_verified"] == 1
    if version == "legacy" and side == "BUY":
        assert result["net_fees_usdc"] == "0"
        assert result["fees_by_asset"]["2"] == {"charged": "0.25", "refunded": "0.1", "net": "0.15"}
    else:
        assert result["fees_charged_usdc"] == "0.25"
        assert result["net_fees_usdc"] == ("0.15" if refund else "0.25")
    facts["local_history"]["trades"].append(deepcopy(facts["local_history"]["trades"][1]))
    assert evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"] == result


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate_log",
        "duplicate_fill",
        "wrong_wallet",
        "refund_wallet",
        "wrong_asset",
        "wrong_module",
        "excess",
        "wrong_order",
        "failed",
        "time",
        "unknown_deployment",
        "missing_fill",
    ],
)
def test_bad_receipt_cannot_prove_refunds_or_qualification(fault):
    facts = fee_history("legacy", "SELL", 100000)
    receipts = facts["local_history"]["receipts"]
    tx, evidence = next(iter(receipts.items()))
    receipt = evidence["receipt_json"]
    refund = receipt["logs"][-1]
    if fault == "missing":
        receipts.clear()
    elif fault == "duplicate_log":
        receipt["logs"].append(deepcopy(refund))
    elif fault == "duplicate_fill":
        facts["local_history"]["trades"].append({**facts["local_history"]["trades"][-1], "trade_id": "duplicate"})
    elif fault == "wrong_wallet":
        receipt["logs"][0]["topics"][2] = "0x" + "00" * 32
    elif fault == "refund_wallet":
        refund["topics"][2] = "0x" + "00" * 32
    elif fault == "wrong_asset":
        refund["data"] = f"0x{2:064x}{100000:064x}"
    elif fault == "wrong_module":
        refund["address"] = WALLET
    elif fault == "excess":
        refund["data"] = f"0x{0:064x}{300000:064x}"
    elif fault == "wrong_order":
        refund["topics"][1] = "0x" + "00" * 32
    elif fault == "failed":
        receipt["status"] = "0x0"
    elif fault == "time":
        evidence["block_time"] = AS_OF
    elif fault == "unknown_deployment":
        evidence["transaction_json"]["to"] = WALLET
        receipt["to"] = WALLET
    else:
        receipt["logs"].pop(0)
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)
    ledger = result["reconciliation"]
    assert not ledger["complete"] and ledger["net_fees_usdc"] is None
    if fault == "missing":
        assert ledger["fees_charged_usdc"] == "0.25" and ledger["fees_refunded_usdc"] is None
        assert result["profiles"]["CRYPTO"]["metrics"]["sector_pnl"] == 109.85
    assert any(t.get("transaction_hash") == tx and t["feed"] == "fee_refunds" for t in ledger["repair_targets"])
    assert result["profiles"]["CRYPTO"]["status"] == "PENDING_REVIEW"


def test_native_receipt_reader_and_repair_reuse_success_and_preserve_facts_on_failure(monkeypatch):
    facts = fee_history()
    tx_hash, evidence = next(iter(facts["local_history"]["receipts"].items()))
    raw = {key: evidence[key] for key in ("transaction_hash", "chain_id", "block_number", "block_time")}
    raw["evidence"] = base64.b64encode(
        zstandard.ZstdCompressor().compress(
            json.dumps([1, evidence["transaction_json"], evidence["receipt_json"]]).encode()
        )
    ).decode()
    query = Mock(return_value=[raw])
    monkeypatch.setattr("smart_money.infrastructure.local_history._postgres_query", query)
    assert read_local_receipts([tx_hash, tx_hash], AS_OF) == {tx_hash: evidence}
    assert tx_hash[2:] in query.call_args.args[0] and "block_time <=" in query.call_args.args[0]
    query.return_value = [{**raw, "evidence": base64.b64encode(b"damaged").decode()}]
    assert "error" in read_local_receipts([tx_hash], AS_OF)[tx_hash]
    client = PolymarketClient()
    try:
        client.get = Mock(side_effect=AssertionError("Frozen HTTP facts must be reused"))
        reader = Mock(side_effect=OSError("offline"))
        monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_receipts", reader)
        saved = client.history(WALLET, AS_OF, previous=facts, repair_targets=[])
        reader.assert_not_called()
        assert saved["local_history"]["receipts"] == {tx_hash: evidence}
        failed = client.history(
            WALLET, AS_OF, previous=facts, repair_targets=[{"feed": "fee_refunds", "transaction_hash": tx_hash}]
        )
        assert failed["local_history"]["receipts"] == saved["local_history"]["receipts"]
        assert failed["work"]["outcome"] == "SOURCE_ERROR" and failed["work"]["error"] == "offline"
    finally:
        client.close()


def test_repair_enriches_old_fee_identity_without_duplicating_or_discarding_facts(monkeypatch):
    native = fee_history("legacy", "BUY", 100000)
    facts = deepcopy(native)
    local = facts["local_history"]
    local["receipts"] = {}
    local["issues"] = ["LEGACY_BUY_FEE_DENOMINATION_UNVERIFIED"]
    for key in ("order_hash", "event_topic", "fee_raw", "fee_asset_id"):
        local["trades"][1].pop(key)
    ledger = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert ledger["net_fees_usdc"] is None
    tx = local["trades"][1]["transaction_hash"]
    assert any(t["feed"] == "orderfilled" and t["transaction_hash"] == tx for t in ledger["repair_targets"])
    fetched = {
        **native["local_history"],
        "trades": [native["local_history"]["trades"][1]],
        "markets": native["markets"],
        "resolutions": native["resolutions"],
    }
    fetched.pop("receipts")
    reader = Mock(return_value=fetched)
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_history", reader)
    receipts = Mock(return_value=native["local_history"]["receipts"])
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_receipts", receipts)
    client = PolymarketClient()
    try:
        client.get = Mock(side_effect=AssertionError("Frozen HTTP facts must be reused"))
        repaired = client.history(WALLET, AS_OF, previous=facts, repair_targets=ledger["repair_targets"])
        assert len(repaired["local_history"]["trades"]) == 20
        assert evaluate_history(repaired, ["CRYPTO"], as_of=AS_OF)["reconciliation"]["complete"]
        assert facts["local_history"] == local and "order_hash" not in local["trades"][1]
        receipts.assert_called_once_with([tx], AS_OF)
        again = client.history(WALLET, AS_OF, previous=repaired, repair_targets=[])
        assert again == repaired and reader.call_count == receipts.call_count == 1
    finally:
        client.close()


@pytest.mark.parametrize("backed_by_chain", [False, True])
def test_identical_native_fills_require_matching_chain_quantity(backed_by_chain):
    facts = history()
    facts["trades"][0].pop("log_index")
    facts["trades"].append(deepcopy(facts["trades"][0]))
    facts["activity"].append(deepcopy(facts["activity"][0]))
    if backed_by_chain:
        facts["local_history"]["trades"].append(
            {**facts["local_history"]["trades"][0], "trade_id": "chain:extra", "log_index": 1}
        )
        facts["open_positions"][0].update(current_size=40, current_value=40, entry_cost_usdc=20, unrealized_pnl=20)
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert result["complete"] is backed_by_chain
    assert ("TRADE_QUANTITY_MISMATCH" in result["reasons"]) is not backed_by_chain


@pytest.mark.parametrize("ambiguous", [False, True])
def test_redemption_without_token_requires_unique_position_mapping(ambiguous):
    facts = history()
    redemption = facts["activity"][2]
    redemption.update(token_id="", outcome_index=0)
    if ambiguous:
        facts["closed_positions"].append({**facts["closed_positions"][0], "token_id": "3", "outcome_index": 1})
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert result["complete"] is not ambiguous
    assert ("REDEMPTION_TOKEN_MAPPING_UNRESOLVED" in result["reasons"]) is ambiguous


def test_rebates_and_rewards_do_not_count_as_trading_profit_or_unknown_operations():
    facts = history()
    facts["activity"].extend(
        {"proxy_wallet": WALLET, "timestamp": AS_OF, "type": kind, "usdc_size": 1000}
        for kind in ("REWARD", "MAKER_REBATE", "TAKER_REBATE", "YIELD")
    )
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)
    assert result["reconciliation"]["complete"]
    assert result["profiles"]["CRYPTO"]["historical_eligible"]
    assert result["profiles"]["CRYPTO"]["metrics"]["sector_pnl"] == 110


@pytest.mark.parametrize("case", ["paired", "missing", "duplicate", "conflict", "non_binary"])
def test_complete_set_operations_preserve_quantity_and_condition_cash_without_direction_promotion(case):
    facts = history()
    condition = facts["open_positions"][0]["condition_id"]
    operations = [
        {
            "proxy_wallet": WALLET,
            "timestamp": AS_OF,
            "condition_id": condition,
            "type": kind,
            "size": size,
            "usdc_size": size,
            "transaction_hash": tx,
        }
        for kind, size, tx in [("SPLIT", 10, "split"), ("MERGE", 4, "merge")]
    ]
    facts["activity"].extend(operations)
    facts["local_history"]["operations"].extend(map(local_operation, operations))
    facts["open_positions"][0].update(current_size=26, current_value=26, unrealized_pnl=13)
    facts["open_positions"].append(
        {
            **facts["open_positions"][0],
            "token_id": "1",
            "outcome_index": 1,
            "current_size": 6,
            "current_value": 0,
            "current_price": 0,
            "unrealized_pnl": -3,
        }
    )
    if case == "missing":
        facts["local_history"]["operations"].pop()
    elif case in {"duplicate", "conflict"}:
        facts["local_history"]["operations"].append(deepcopy(facts["local_history"]["operations"][-1]))
        if case == "conflict":
            facts["local_history"]["operations"][-1]["usdc_size"] = 99
    elif case == "non_binary":
        facts["local_history"]["operations"][-1]["partition"] = [1, 6]
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)
    ledger = result["reconciliation"]
    assert ledger["complete"] is (case in {"paired", "duplicate"})
    assert not any("BALANCE" in row["check"] or "PNL" in row["check"] for row in ledger["differences"])
    assert not result["profiles"]["CRYPTO"]["historical_eligible"]  # Accounting is not directional attribution.
    if case == "missing":
        assert {
            "feed": "operations",
            "transaction_hash": "merge",
            "condition_id": condition,
            "type": "MERGE",
            "missing_in": "local",
        } in ledger["repair_targets"]


def test_supplied_refund_fields_cannot_substitute_for_a_real_source():
    facts = history()
    fill = facts["local_history"]["trades"][1]
    fill["fee_usdc"] = "0.25"
    facts["activity"][1]["usdc_size"] = "10.15"
    facts["closed_positions"][0]["realized_pnl"] = "9.85"
    refund = local_operation(
        {
            **fill,
            "trade_transaction_hash": fill["transaction_hash"],
            "type": "FEE_REFUND",
            "fee_asset": "COLLATERAL",
            "usdc_size": "0.10",
        }
    )
    facts["local_history"]["operations"].append(refund)
    facts["local_history"]["coverage"]["fee_refunds"] = [[1, 1000]]
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert not result["complete"]
    assert {"FEE_REFUND_SOURCE_UNVERIFIED", "FEE_REFUND_COVERAGE_UNVERIFIED"} <= set(result["reasons"])
    assert result["fees_refunded_usdc"] is None and result["net_fees_usdc"] is None


def test_failed_cursor_page_resumes_without_fetching_or_appending_successful_pages_again():
    client = PolymarketClient()
    try:
        params = {"user": WALLET, "start": 1, "end": 123}
        client.get = Mock(
            side_effect=[
                {"data": [{"n": 1}], "pagination": {"next_cursor": "next", "has_more": True}},
                OSError("timeout"),
            ]
        )
        failed = client.page("/v2/trades", params)
        client.get = Mock(return_value={"data": [{"n": 2}], "pagination": {"next_cursor": None, "has_more": False}})
        done = client.page("/v2/trades", params, previous=failed)
        assert done["rows"] == [{"n": 1}, {"n": 2}] and done["coverage"]["complete"]
        client.get.assert_called_once_with("https://data-api.polymarket.com/v2/trades", {**params, "cursor": "next"})
        assert failed["rows"] == [{"n": 1}] and not failed["coverage"]["complete"]
    finally:
        client.close()


@pytest.mark.parametrize("case", ["gap", "resumed_page", "empty_local"])
def test_repair_reuses_frozen_official_facts_and_preserves_old_success_on_source_failure(tmp_path, monkeypatch, case):
    destination = tmp_path / "watchlist.json"
    first = refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    facts = history()
    local = {**facts["local_history"], "markets": facts["markets"], "resolutions": facts["resolutions"]}
    incomplete = packet()
    saved = incomplete["histories"][WALLET]["local_history"]
    missing = saved["trades"].pop()
    saved["coverage"]["orderfilled"] = [[1, 118], [120, 1000]]
    fetched_trades = [missing]
    http = Mock(side_effect=AssertionError("Successful HTTP facts must be reused"))
    if case == "resumed_page":
        frozen = incomplete["histories"][WALLET]
        last = frozen["trades"].pop()
        frozen["coverage"]["trades"] = {
            "complete": False,
            "endpoint": "/v2/trades",
            "pages": 1,
            "next_cursor": "resume",
            "cursors": ["resume"],
            "params": {"user": WALLET, "start": 1, "end": int(NOW.timestamp()), "taker_only": "false", "limit": 1000},
        }
        saved["coverage"]["orderfilled"] = [[1, 1000]]
        http = Mock(return_value={"data": [last], "pagination": {"next_cursor": None, "has_more": False}})
    elif case == "empty_local":
        saved["trades"] = []
        fetched_trades = local["trades"]
    pending = refresh_watchlist(destination, inputs=incomplete, offline=True, categories=["crypto"])
    old = pending["records"][f"{WALLET}:CRYPTO"]
    monkeypatch.setattr(PolymarketClient, "get", http)
    reader = Mock(side_effect=[OSError("unavailable"), {**local, "trades": fetched_trades}])
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_history", reader)
    monkeypatch.setattr(
        "smart_money.infrastructure.polymarket.read_local_operations",
        Mock(side_effect=AssertionError("No operation gap")),
    )
    failed = refresh_watchlist(destination, repair=True)
    row = failed["records"][f"{WALLET}:CRYPTO"]
    assert row["stale"] and row["metrics"] == old["metrics"]
    assert row["last_successful_evaluation_ref"] == old["last_successful_evaluation_ref"]
    repaired = refresh_watchlist(destination, repair=True)
    assert repaired["updated_at"] == AS_OF and repaired["summary"]["qualified"] == 2
    assert repaired["discovery"] == pending["discovery"] and repaired["candidates"] == pending["candidates"]
    assert all(load_evaluation(repaired, r["evaluation_ref"], detail=True) for r in first["records"].values())
    targets = reader.call_args.kwargs["targets"]
    if case == "gap":
        assert {"feed": "orderfilled", "from_block": 119, "to_block": 119, "reason": "LOCAL_SCAN_GAP"} in targets
    assert any(
        t.get("transaction_hash") == missing["transaction_hash"] or t.get("condition_id") == missing["condition_id"]
        for t in targets
    )
    reference = load_evaluation(repaired, repaired["records"][f"{WALLET}:CRYPTO"]["evaluation_ref"])["history_ref"]
    repaired_history = load_history(repaired, reference)
    assert len(repaired_history["local_history"]["trades"]) == 20
    assert repaired_history["trades"] == facts["trades"]
    before = destination.read_bytes()
    refresh_watchlist(destination, repair=True)
    assert destination.read_bytes() == before
    assert reader.call_count == 2
    assert http.call_count == (1 if case == "resumed_page" else 0)


def test_cutoff_boundary_is_explicit_and_future_progress_does_not_change_history(monkeypatch):
    monkeypatch.setattr(
        "smart_money.infrastructure.local_history._clickhouse_query",
        Mock(
            return_value=[
                {
                    "before": [1000, int(NOW.timestamp()), "11" * 32],
                    "after": [1001, int(NOW.timestamp()) + 2, "22" * 32],
                }
            ]
        ),
    )
    facts = history()
    coverage = facts["local_history"]["coverage"]
    coverage["boundary"] = read_cutoff_boundary(AS_OF)
    before = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert before["complete"]
    coverage.update(last_block=3000, end=(NOW + timedelta(days=1)).isoformat())
    for feed in ("orderfilled", "activity"):
        coverage[feed].append([2000, 3000])
    assert evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"] == before
    coverage["orderfilled"] = [[1, 998], [1001, 3000]]
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["reconciliation"]
    assert result["repair_targets"] == [
        {"feed": "orderfilled", "from_block": 999, "to_block": 1000, "reason": "LOCAL_SCAN_GAP"}
    ]


def test_repair_commits_each_wallet_and_skips_it_after_interruption(tmp_path, monkeypatch):
    second = "0x" + "ef" * 20
    complete = {WALLET: history(), second: json.loads(json.dumps(history()).replace(WALLET, second))}
    data = packet()
    data["profiles"][second] = {"wallet": second, "evidence": {"public_profile": {"proxyWallet": second}}}
    data["candidates"].append({"account": second, "source": "local", "sector_id": "CRYPTO"})
    data["histories"] = deepcopy(complete)
    for facts in data["histories"].values():
        facts["local_history"]["trades"].pop()
    destination = tmp_path / "watchlist.json"
    first = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    calls = []

    def fetch(self, wallet, as_of, *, previous, repair_targets):
        calls.append(wallet)
        assert as_of == AS_OF and previous["wallet"] == wallet and repair_targets
        if calls == [WALLET, second]:
            raise KeyboardInterrupt
        return complete[wallet]

    monkeypatch.setattr(PolymarketClient, "history", fetch)
    with pytest.raises(KeyboardInterrupt):
        refresh_watchlist(destination, repair=True)
    checkpoint = json.loads(destination.read_text())
    assert checkpoint["records"][f"{WALLET}:CRYPTO"]["historical_eligible"]
    assert checkpoint["records"][f"{second}:CRYPTO"] == first["records"][f"{second}:CRYPTO"]
    review = Mock(wraps=evaluate_history)
    monkeypatch.setattr("smart_money.watchlist.evaluate_history", review)
    done = refresh_watchlist(destination, repair=True)
    assert review.call_count == 3  # One saved complete wallet; the second before and after repair.
    assert calls == [WALLET, second, second] and done["summary"]["listening_wallets"] == [WALLET, second]
    assert all(load_evaluation(done, r["evaluation_ref"], detail=True) for r in first["records"].values())
    destination.write_text(json.dumps(first))
    calls.clear()
    selected = refresh_watchlist(destination, repair=True, repair_wallet=second)
    assert calls == [second] and selected["summary"]["listening_wallets"] == [second]
    assert selected["records"][f"{WALLET}:CRYPTO"] == first["records"][f"{WALLET}:CRYPTO"]
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="not in the saved watchlist"):
        refresh_watchlist(destination, repair=True, repair_wallet="0x" + "11" * 20)
    with pytest.raises(ValueError, match="requires repair"):
        refresh_watchlist(destination, inputs=data, offline=True, repair_wallet=second)
    assert destination.read_bytes() == before


@pytest.mark.parametrize("target", ["transaction", "range", "invalid"])
def test_local_operations_reader_normalizes_existing_projection_without_source_writes(monkeypatch, target):
    prefix = "POLYDATA_ORDERFILLED_CLICKHOUSE_"
    for key, value in {
        "xue_lab_ip": "host",
        "xue_lab_user": "user",
        "xue_lab_pwd": "secret",
        prefix + "HTTP_URL": "http://127.0.0.1:18123",
        prefix + "USER": "reader",
        prefix + "PASSWORD": "secret",
    }.items():
        monkeypatch.setenv(key, value)
    row = {
        "address": WALLET[2:],
        "condition_id": "00" * 32,
        "transaction_hash": "11" * 32,
        "source_contract": "4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "collateral_token": "22" * 20,
        "parent_collection_id": "00" * 32,
        "cashflow_type": "SPLIT",
        "partition_json": "[1,2]",
        "block_number": "500",
        "timestamp": int(NOW.timestamp()),
        "usdc_size": "10",
    }
    query = Mock(return_value={"data": [row]})
    monkeypatch.setattr("smart_money.infrastructure.local_history._remote_query", query)
    targets = [{"feed": "operations", "transaction_hash": "0x" + "11" * 32}]
    if target == "range":
        targets = [{"feed": "activity", "from_block": 500, "to_block": 501}]
    elif target == "invalid":
        targets[0]["transaction_hash"] = "' OR TRUE --"
        with pytest.raises(ValueError, match="INVALID_REPAIR_TRANSACTION"):
            read_local_operations(WALLET, AS_OF, targets=targets)
        query.assert_not_called()
        return
    result = read_local_operations(WALLET, AS_OF, targets=targets)
    assert result[0]["proxy_wallet"] == WALLET and result[0]["block_number"] == 500
    assert result[0]["partition"] == [1, 2] and result[0]["usdc_size"] == "10"
    assert "readonly=1" in query.call_args.args[1]["url"]
    sql = query.call_args.args[1]["sql"]
    assert ("block_number BETWEEN 500 AND 501" if target == "range" else "tx_hash IN ('0x" + "11" * 32) in sql


@pytest.mark.parametrize(
    "metric,value,reason",
    [
        ("directional_events", 19, "INSUFFICIENT_DIRECTIONAL_EVENTS"),
        ("win_rate", 0.649, "DIRECTIONAL_WIN_RATE_BELOW_MIN"),
        ("sector_pnl", 0, "DIRECTIONAL_PNL_NOT_POSITIVE"),
        ("profit_factor", 1.199, "DIRECTIONAL_PROFIT_FACTOR_BELOW_MIN"),
        ("two_sided_ratio", 0.101, "TWO_SIDED_EVENT_RATIO_ABOVE_MAX"),
        ("recent_events", 9, "RECENT_DIRECTIONAL_SAMPLE_INSUFFICIENT"),
        ("recent_win_rate", 0.599, "RECENT_DIRECTIONAL_WIN_RATE_BELOW_MIN"),
        ("recent_sector_pnl", 0, "RECENT_DIRECTIONAL_PNL_NOT_POSITIVE"),
        ("recent_profit_factor", 1.199, "RECENT_DIRECTIONAL_PROFIT_FACTOR_BELOW_MIN"),
        ("recent_inactivity_days", 30.001, "RECENT_DIRECTIONAL_ACTIVITY_STALE"),
        ("two_sided_ratio", None, "MISSING_OR_INVALID_METRIC:two_sided_ratio"),
        ("win_rate", float("nan"), "MISSING_OR_INVALID_METRIC:win_rate"),
    ],
)
def test_existing_thresholds_and_unknowns_remain_fail_closed(metric, value, reason):
    metrics = {
        "directional_events": 20,
        "win_rate": 0.65,
        "sector_pnl": 1,
        "profit_factor": 1.2,
        "two_sided_ratio": 0.1,
        "recent_events": 10,
        "recent_win_rate": 0.6,
        "recent_sector_pnl": 1,
        "recent_profit_factor": 1.2,
        "recent_inactivity_days": 30,
    }
    assert decide_directional_expert(metrics).eligible
    result = decide_directional_expert({**metrics, metric: value})
    assert not result.eligible and reason in result.reasons


@pytest.mark.parametrize("version", [1, 2, 4])
def test_unsupported_schema_is_rejected_without_rewriting(tmp_path, version):
    destination = tmp_path / "watchlist.json"
    destination.write_text(json.dumps({"schema_version": version}))
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="WATCHLIST_STORAGE_MIGRATION_REQUIRED"):
        refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    assert destination.read_bytes() == before


def test_failed_atomic_replace_preserves_the_file_and_cleans_temporary(tmp_path, monkeypatch):
    destination = tmp_path / "watchlist.json"
    refresh_watchlist(destination, inputs=packet(), offline=True, categories=["crypto"])
    before = destination.read_bytes()
    changed = packet()
    changed["histories"][WALLET]["coverage"]["trades"]["complete"] = False
    monkeypatch.setattr("smart_money.infrastructure.wallet_storage.os.replace", Mock(side_effect=OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        refresh_watchlist(destination, inputs=changed, offline=True, categories=["crypto"])
    assert destination.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {"watchlist.json", "watchlist.json.lock", "watchlist.archive"}


@pytest.mark.parametrize(
    "case",
    [
        "open",
        "add",
        "source",
        "balance",
        "reorg",
        "mas",
        "slow_mas",
        "dynamic",
        "explore",
        "unprofiled",
        "expired_profile",
        "other_sector",
        "unrelated_holding",
        "reduce",
        "exit",
        "suspended",
        "corrupt_watchlist",
        "paired",
        "transfer",
        "stale",
        "late_qualification",
        "paused",
        "baseline_retry",
        "baseline_pruned",
        "baseline_lag",
        "mapping",
        "seed",
    ],
)
def test_watchlist_monitor_to_existing_mas_preserves_baseline_cursor_and_retries(tmp_path, monkeypatch, case):
    facts = fee_history()
    tx = deepcopy(next(iter(facts["local_history"]["receipts"].values())))
    fill = deepcopy(facts["local_history"]["trades"][1])
    when = (NOW + timedelta(seconds=10)).isoformat()
    tx.update(
        block_number=1002, block_hash="0x" + f"{1002:064x}", block_time=when, source="polygon_rpc:confirmed_receipt"
    )
    fill.update(timestamp=when, block_number=1002)
    # Two distinct partial fills of one order are one observation, never one deduplicated fill.
    tx["fills"] = [
        dict(fill, size="8", notional="4", fee_raw="100000", fee_usdc="0.1", log_index=0),
        dict(fill, size="12", notional="6", fee_raw="150000", fee_usdc="0.15", log_index=1),
    ]
    selling = case in {"reduce", "exit"}
    if selling:
        for row in tx["fills"]:
            row["side"] = "SELL"
    first_log = tx["receipt_json"]["logs"][0]
    logs = [
        dict(
            first_log,
            logIndex=hex(i),
            data="0x"
            + "".join(
                f"{n:064x}" for n in ((1, 2, size, cash, fee, 0, 0) if selling else (0, 2, cash, size, fee, 0, 0))
            ),
        )
        for i, (cash, size, fee) in enumerate(((4000000, 8000000, 100000), (6000000, 12000000, 150000)))
    ]
    logs.append(
        dict(
            first_log,
            address=CTF,
            logIndex="0x2",
            topics=[TRANSFER_SINGLE, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + WALLET[2:].zfill(64)],
            data=f"0x{2:064x}{20000000:064x}",
        )
    )
    if case == "transfer":
        tx["fills"], logs = [], logs[-1:]
    if selling:
        logs[-1]["topics"][2], logs[-1]["topics"][3] = logs[-1]["topics"][3], logs[-1]["topics"][2]
    tx["receipt_json"]["logs"] = logs
    for obj in (tx["transaction_json"], tx["receipt_json"], *logs):
        obj.update(blockNumber=hex(1002), blockHash=tx["block_hash"])
    market = {**facts["markets"][fill["condition_id"]], "outcomes": ["Yes", "No"], "closed": False}
    snapshot = {"payload": market, "source": "fixture:market", "obtained_at": AS_OF}
    baseline = {"add": 5, "reduce": 30, "exit": 20}.get(case, 0)
    delta = -20 if selling else 20

    class Reader:
        height = 1000
        fault = None

        def preflight(self, max_age):
            return {"status": "READY", "finalized": self.finalized(max_age)}

        def finalized(self, max_age):
            return self.block(self.height)

        def block(self, n):
            return {
                "number": n,
                "hash": "0x" + ("ff" * 32 if self.fault == "reorg" and n == 1000 else f"{n:064x}"),
                "timestamp": when if n >= 1002 else AS_OF,
            }

        def seed_tokens(self, wallet):
            if self.fault == "seed":
                raise OSError("BASELINE_TOKEN_DISCOVERY_INCOMPLETE")
            if case == "unrelated_holding":
                return ["999"]
            return ["2"] if baseline or case == "paired" else []

        def markets(self, tokens):
            assert "999" not in tokens  # Unrelated inventory metadata must not delay a confirmed trade.
            if self.fault == "mapping":
                raise OSError("mapping unavailable")
            return {token: snapshot for token in tokens if token in {"2", "3"}}

        def balance(self, wallet, token, block):
            if token == "999":
                return Decimal(7)
            if self.fault in {"baseline_retry", "baseline_pruned", "baseline_lag"} and block < self.height:
                error = OSError if self.fault == "baseline_retry" else HistoricalStateUnavailable
                raise error("historical balance unavailable")
            if token == "3":
                return Decimal(5 if case == "paired" else 0)
            return Decimal(
                baseline + (delta if block >= 1002 else 0) + (1 if self.fault == "balance" and block >= 1002 else 0)
            )

        def transactions(self, wallets, start, end):
            if self.fault == "source":
                raise OSError("source unavailable")
            assert wallets == [WALLET]
            return [deepcopy(tx), deepcopy(tx)] if start <= 1002 <= end else []

    watchlist, output = tmp_path / "watchlist.json", tmp_path / "monitor.json"
    evaluation_clock = Mock(wraps=datetime)
    evaluation_clock.now.return_value = NOW
    monkeypatch.setattr("smart_money.watchlist.datetime", evaluation_clock)
    data = packet()
    if case == "explore":
        data["histories"][WALLET]["coverage"]["trades"]["complete"] = False
    screened = refresh_watchlist(watchlist, inputs=data, offline=True, categories=["crypto"])
    screened["candidates"][0]["username"] = "observed-public-name"
    write_json(watchlist, screened)
    if case == "explore":
        candidate = screened["candidates"][0]
        candidate["summary_ref"] = archive_for(screened).put(
            {
                "as_of": AS_OF,
                "status": "READY",
                "source": "fixture:official-trade",
                "recent_trade": data["histories"][WALLET]["trades"][0],
            },
        )
        _allocate_monitoring(screened, AS_OF)
        assert not any(r["historical_eligible"] for r in screened["records"].values())
        assert all(r["monitor"]["mode"] == "EXPLORE" for r in screened["records"].values())
        write_json(watchlist, screened)
    elif case == "suspended":
        for row in screened["records"].values():
            row["forward_status"] = "SUSPENDED"
        _allocate_monitoring(screened, AS_OF)
        write_json(watchlist, screened)
    elif case in {"unprofiled", "expired_profile", "other_sector"}:
        for row in screened["records"].values():
            if case == "unprofiled":
                row.update(
                    historical_eligible=False,
                    status="PENDING_REVIEW",
                    evaluation_ref=None,
                    metrics={"sector_pnl": 0, "directional_events": 0, "win_rate": 0},
                )
            elif case == "expired_profile":
                row.update(stale=True, valid_until=(NOW - timedelta(days=1)).isoformat())
            else:
                row["sector_id"] = "SPORTS"
        write_json(watchlist, screened)
    reader = Reader()
    if case == "seed":
        reader.fault = "seed"
    engine = MasEngine(llm_enabled=False, osint_enabled=False)
    engine.run = Mock(wraps=engine.run)
    monkeypatch.setattr(
        "smart_money.monitor._now",
        lambda: (NOW + timedelta(seconds=15 if case == "late_qualification" else 1)).isoformat(),
    )
    if case == "dynamic":
        qualified = watchlist.read_text()
        empty = json.loads(qualified)
        empty["records"] = {}
        watchlist.write_text(json.dumps(empty))
        waiting = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
        assert waiting["health"]["status"] == "NO_MONITORED_WALLETS" and waiting["cursor"] is None
        watchlist.write_text(qualified)
    assert run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)["health"]["status"] == (
        "RECOVERY_PENDING" if case == "seed" else "BASELINE_READY"
    )
    first = json.loads(output.read_text())
    assert first["observations"] == {} and first["cursor"]["number"] == 1000
    if case == "paused":
        paused = json.loads(watchlist.read_text())
        for record in paused["records"].values():
            record["manual_paused"] = True
        watchlist.write_text(json.dumps(paused))
        reader.height = 1001
        monkeypatch.setattr("smart_money.monitor._now", lambda: (NOW + timedelta(seconds=5)).isoformat())
        run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
    if case == "corrupt_watchlist":
        watchlist.write_text("{broken")
    reader.height = 1002
    monkeypatch.setattr(
        "smart_money.monitor._now", lambda: (NOW + timedelta(seconds=300 if case == "stale" else 20)).isoformat()
    )
    if case in {"source", "balance", "reorg"}:
        reader.fault = case
        blocked = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
        assert blocked["health"]["status"] == "BLOCKED" and blocked["cursor"] == first["cursor"]
        assert json.loads(output.read_text())["wallets"] == first["wallets"]
        reader.fault = None
    if case == "mas":
        engine.run.side_effect = OSError("model unavailable")
    if case in {"baseline_retry", "baseline_pruned", "baseline_lag", "mapping", "seed"}:
        reader.fault = case
        pending = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
        saved = json.loads(output.read_text())
        assert pending["health"]["status"] == "RECOVERY_PENDING" and pending["cursor"]["number"] == 1002
        assert len(saved["transactions"]) == len(saved["observations"]) == 1
        original = deepcopy(next(iter(saved["observations"].values())))
        if case == "mapping":
            engine.run.assert_not_called()
        else:
            engine.run.assert_called_once()
        if case.startswith("baseline"):
            assert saved["wallets"][WALLET]["positions"]["2"] is None
            assert original["evidence"]["trade"]["action"] == "UNKNOWN"
        if case == "baseline_retry":
            repeated = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
            assert repeated["health"]["status"] == "RECOVERY_PENDING"
            assert json.loads(output.read_text())["wallets"][WALLET]["positions"]["2"] is None
        if case == "baseline_lag":
            reader.height = 1004
            run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True, block_batch_size=1)
            lagged = json.loads(output.read_text())
            assert lagged["cursor"]["number"] == 1003 and lagged["wallets"][WALLET]["positions"]["2"] is None
            assert lagged["wallets"][WALLET]["baselines"]["2"]["block"]["number"] == 1004
        if case not in {"baseline_pruned", "baseline_lag"}:
            reader.fault = None
    if case == "slow_mas":
        started, release, finished = Event(), Event(), Event()
        original_run = engine.run

        def slow_run(*args, **kwargs):
            started.set()
            assert release.wait(5)
            result = original_run(*args, **kwargs)
            finished.set()
            return result

        engine.run = slow_run

        def next_tick(seconds):
            saved = json.loads(output.read_text())
            job = next(iter(saved["observations"].values()))["research"]
            if reader.height == 1002:
                assert started.wait(5) and job["status"] == "RUNNING"
                reader.height = 1003
            elif not release.is_set():
                assert saved["cursor"]["number"] == 1003 and job["status"] == "RUNNING"
                release.set()
                assert finished.wait(5)
            elif job["status"] == "OFFLINE_DONE":
                raise KeyboardInterrupt

        monkeypatch.setattr("smart_money.monitor.time.sleep", next_tick)
        try:
            with pytest.raises(KeyboardInterrupt):
                run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True, watch=True)
        finally:
            release.set()
        original_run.assert_called_once()
    done = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
    saved = json.loads(output.read_text())
    if case == "paused":
        assert done["cursor"]["number"] == reader.height and saved["observations"] == {}
        assert saved["wallets"][WALLET]["inactive_since"]
        engine.run.assert_not_called()
        return
    assert done["cursor"]["number"] == reader.height and len(saved["observations"]) == 1
    assert saved["wallets"][WALLET]["positions"]["2"] == str(baseline + delta)
    observation = next(iter(saved["observations"].values()))
    if case == "explore":
        assert done["health"]["status"] == "EXPLORING" and done["health"]["qualified_wallets"] == 0
    if case == "corrupt_watchlist":
        assert done["health"]["watchlist_error"] and done["health"]["active_wallets"] == 1
    if case in {"transfer", "late_qualification"}:
        assert observation["research"]["status"] == "NOT_SUBMITTED"
        engine.run.assert_not_called()
    else:
        evidence = observation.get("supplement", observation)["evidence"]
        trade = evidence["trade"]
        assert "market" not in observation["request"]["context"]
        assert "evidence" not in observation["request"]["candidate"]
        assert (
            trade["size"],
            trade["current_trade_notional"],
            trade["wallet_entry_price"],
            trade["net_fees_usdc"],
        ) == ("20", "10", "0.5", "0.25")
        expected_action = (
            "UNKNOWN"
            if case.startswith("baseline")
            else {"add": "ADD", "reduce": "REDUCE", "exit": "EXIT"}.get(case, "OPEN")
        )
        assert trade["action"] == expected_action and len(trade["source_refs"]) == 2
        if case.startswith("baseline"):
            gap = saved["wallets"][WALLET]["gaps"][0]
            assert gap["status"] == ("REPLAYED" if case == "baseline_retry" else "REBASED_WITH_HISTORY_GAP")
            assert observation == original  # Recovery must not rewrite an old unknown into a new OPEN.
        if case == "mapping":
            assert observation["evidence"] == original["evidence"] and "pending_context" not in observation
            assert observation["supplement"]["research_eligible"]
            assert evidence["signal"]["first_observed_at"] == original["evidence"]["signal"]["first_observed_at"]
        if case == "mas":
            assert observation["research"]["status"] == "FAILED"
            engine.run.side_effect = None
            run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
            observation = next(iter(json.loads(output.read_text())["observations"].values()))
        assert observation["research"]["status"] == "OFFLINE_DONE"
        result = observation["research"]["result"]
        assert result["candidate"]["candidate_id"] == observation["observation_id"]
        assert result["candidate"]["signal_type"] == "SMART_MONEY_TRADE"
        assert result["candidate"]["side"] == ("SELL" if selling else "BUY")
        profile = result["candidate"]["wallet_profiles"][0]
        assert profile["display_name"] == "observed-public-name"
        if case in {"explore", "unprofiled", "expired_profile", "other_sector"}:
            assert profile["wallet_validation_status"] == "UNPROFILED"
        if case == "unprofiled":
            assert profile["sector_win_rate"] is None and profile["sector_pnl"] is None
        if case == "other_sector":
            assert not profile["sector_match"]
        assert result["candidate"]["wallet_profiles"][0]["admission_status"] == (
            "SUSPENDED" if case == "suspended" else "SHADOW_OBSERVE"
        )
        assert result["policy"]["status"] != "READY_TO_PUBLISH"
    previous = json.loads(output.read_text())
    run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
    assert json.loads(output.read_text())["observations"] == previous["observations"]
    if case == "dynamic":
        watchlist.write_text(json.dumps(empty))
        removed = run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
        assert removed["health"]["active_wallets"] == 1  # Open inventory remains a follow-up responsibility.
        retained = json.loads(output.read_text())
        assert not retained["qualifications"][WALLET][-1]["record"]["monitor"]["enabled"]
        assert retained["observations"] == previous["observations"]
        engine.run.assert_called_once()
    if case == "paused":
        saved_cursor = previous["cursor"]
        write_json(watchlist, screened)
        reader.height = 1003
        run_monitor(watchlist, output, reader=reader, engine=engine, offline_mas=True)
        rejoined = json.loads(output.read_text())
        account = rejoined["wallets"][WALLET]
        assert account["baseline"]["number"] == 1003
        assert account["baseline_history"][0]["baseline"]["number"] == 1000
        assert rejoined["cursor"]["number"] > saved_cursor["number"]
        assert rejoined["observations"] == previous["observations"]
        engine.run.assert_not_called()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_daily_and_settlement_maintenance_is_scoped_durable_and_repeatable(tmp_path, monkeypatch, capsys, failure):
    second, other_condition = "0x" + "ef" * 20, f"0x{1000:064x}"
    condition = f"0x{0:064x}"
    data = packet()
    data["profiles"][second] = {"wallet": second, "evidence": {"account": second}}
    data["candidates"].append({"account": second, "source": "local", "sector_id": "CRYPTO"})
    data["histories"][second] = json.loads(
        json.dumps(history()).replace(WALLET, second).replace(condition, other_condition)
    )
    for operation in data["histories"][second]["local_history"]["operations"]:
        operation["parent_collection_id"] = "0x" + "0" * 64
    destination = tmp_path / "watchlist.json"
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr("smart_money.watchlist.datetime", clock)
    original = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    assert original["records"][f"{second}:CRYPTO"]["historical_eligible"]
    settlements = {key: value for h in data["histories"].values() for key, value in h["resolutions"].items()}
    probe = Mock(side_effect=lambda conditions, as_of: {c: deepcopy(settlements[c]) for c in conditions})
    monkeypatch.setattr("smart_money.watchlist.read_resolutions", probe)
    calls, fault = [], None

    def fetch(self, wallet, as_of, *, previous, repair_targets):
        calls.append(wallet)
        assert previous["wallet"] == wallet
        assert as_of == clock.now.return_value.isoformat()
        if fault:
            raise fault("interrupted refresh")
        facts = deepcopy(data["histories"][wallet])
        facts["as_of"] = facts["local_history"]["as_of"] = as_of
        facts["resolutions"] = {c: deepcopy(settlements[c]) for c in facts["markets"]}
        facts["local_history"]["coverage"]["boundary"].update(
            block_time=as_of, next_block_time=(datetime.fromisoformat(as_of) + timedelta(seconds=2)).isoformat()
        )
        clock.now.return_value += timedelta(minutes=1)  # The next wallet starts after this read finishes.
        return facts

    boards = Mock(
        side_effect=lambda category, period: deepcopy(next(b for b in data["boards"] if b["period"] == period))
    )
    monkeypatch.setattr(PolymarketClient, "history", fetch)
    monkeypatch.setattr(PolymarketClient, "leaderboard", boards)
    clock.now.return_value = NOW + timedelta(hours=1)
    first = refresh_watchlist(destination, due=True, categories=["crypto"])
    assert calls == [WALLET, second] and not first["maintenance"]["pending"]
    assert not boards.called  # Today's successful boards were already captured.
    assert main(["watchlist", "--due", "--output", str(destination), "--categories", "crypto"]) == 0
    assert calls == [WALLET, second] and not boards.called
    capsys.readouterr()

    # A late-arriving/revised result has an old settlement time, so timestamp-only polling would miss it.
    settlements[condition]["transaction_hash"] = "corrected-settlement"
    clock.now.return_value += timedelta(minutes=5)
    fault = failure
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            refresh_watchlist(destination, due=True, categories=["crypto"])
        failed = json.loads(destination.read_text())
    else:
        failed = refresh_watchlist(destination, due=True, categories=["crypto"])
        assert failed["records"][f"{WALLET}:CRYPTO"]["stale"]
    assert set(failed["maintenance"]["pending"]) == {WALLET}
    assert failed["records"][f"{WALLET}:CRYPTO"]["metrics"] == first["records"][f"{WALLET}:CRYPTO"]["metrics"]
    assert failed["records"][f"{second}:CRYPTO"] == first["records"][f"{second}:CRYPTO"]
    # A targeted settlement refresh gives wallets different cutoffs; repair must retain each one's cutoff.
    old_cutoff = first["records"][f"{second}:CRYPTO"]["data_cutoff"]
    scoped = refresh_watchlist(destination, repair=True, repair_wallet=second)
    assert scoped["records"][f"{second}:CRYPTO"]["data_cutoff"] == old_cutoff
    scoped = refresh_watchlist(destination, repair=True, repair_wallet=WALLET)
    assert scoped["records"][f"{WALLET}:CRYPTO"]["reasons"] == ["NEWER_REFRESH_PENDING"]
    assert WALLET in scoped["maintenance"]["pending"] and len(calls) == 3
    fault = None
    clock.now.return_value += timedelta(minutes=6)  # Durable retry backoff, not a busy loop.
    repaired = refresh_watchlist(destination, due=True, categories=["crypto"])
    assert calls == [WALLET, second, WALLET, WALLET] and not repaired["maintenance"]["pending"]
    ref = repaired["records"][f"{WALLET}:CRYPTO"]["evaluation_ref"]
    assert load_evaluation(repaired, ref, detail=True)["trigger"]["reason"] == "SETTLEMENT_CHANGED"
    assert all(load_evaluation(repaired, r["evaluation_ref"], detail=True) for r in original["records"].values())
    refresh_watchlist(destination, due=True, categories=["crypto"])
    assert len(calls) == 4

    probe.side_effect = OSError("settlement source unavailable")
    blocked = refresh_watchlist(destination, due=True, categories=["crypto"])
    assert blocked["maintenance"]["settlement_check"]["status"] == "FAILED"
    assert blocked["maintenance"]["settlements"] == repaired["maintenance"]["settlements"]
    assert len(calls) == 4
    probe.side_effect = lambda conditions, as_of: {c: deepcopy(settlements[c]) for c in conditions}
    blocked["records"][f"{WALLET}:CRYPTO"].update(manual_paused=True, forward_status="SUSPENDED")
    destination.write_text(json.dumps(blocked))
    clock.now.return_value = NOW + timedelta(days=1)
    daily = refresh_watchlist(destination, due=True, categories=["crypto"])
    assert calls[-2:] == [second, WALLET] and len(calls) == 6  # Oldest due review goes first.
    assert [c.args[1] for c in boards.call_args_list] == ["week", "month"]
    assert daily["records"][f"{WALLET}:CRYPTO"]["manual_paused"]
    assert daily["records"][f"{WALLET}:CRYPTO"]["forward_status"] == "SUSPENDED"
    clock.now.return_value = NOW + timedelta(days=7)
    refresh_watchlist(destination, due=True, categories=["crypto"])
    assert [c.args[1] for c in boards.call_args_list][-3:] == ["week", "month", "all"]


@pytest.mark.parametrize("page_failure", [False, True])
def test_incremental_history_reads_new_interval_reuses_facts_and_resumes_its_cursor(monkeypatch, page_failure):
    previous = history()
    before = deepcopy(previous)
    as_of = (NOW + timedelta(minutes=10)).isoformat()
    new_trade = dict(
        previous["trades"][0], timestamp=int(NOW.timestamp()) + 60, transaction_hash="0x" + "fa" * 32, size=2
    )
    new_trade.pop("log_index")  # Native API rows have no log identity; equal rows cannot be collapsed.
    new_activity = dict(new_trade, type="TRADE", usdc_size=1)
    local = deepcopy(previous["local_history"])
    local.update(as_of=as_of, markets=previous["markets"], resolutions=previous["resolutions"])
    local["trades"] = [
        dict(new_trade, notional=1, fee_usdc="0", block_number=1005, contract="0x" + "ef" * 20, trade_id="new-fill")
    ]
    boundary = dict(
        local["coverage"]["boundary"],
        block_number=1100,
        block_time=as_of,
        next_block_number=1101,
        next_block_time=(NOW + timedelta(minutes=10, seconds=2)).isoformat(),
    )
    reader = Mock(return_value=local)
    operations = Mock(return_value=[])
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_history", reader)
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_local_operations", operations)
    monkeypatch.setattr("smart_money.infrastructure.polymarket.read_cutoff_boundary", Mock(return_value=boundary))
    monkeypatch.setattr(
        "smart_money.infrastructure.polymarket.read_wallet_event_index",
        Mock(return_value={"conditions": sorted(previous["markets"]), "unmapped_tokens": 0}),
    )
    queries = []

    def get(url, params):
        queries.append((url, dict(params)))
        if url.endswith("/positions"):
            rows = deepcopy(previous["open_positions" if params["status"] == "OPEN" else "closed_positions"])
            if params["status"] == "OPEN":
                rows[0]["current_size"] = 22
        else:
            assert (
                params["start"] == int(NOW.timestamp()) - 1
                and params["end"] == int(datetime.fromisoformat(as_of).timestamp()) - 1
            )
            if params.get("cursor"):
                if page_failure:
                    raise OSError("page unavailable")
                rows = []
            else:
                rows = [new_trade, new_trade] if url.endswith("/trades") else [new_activity]
                if page_failure and url.endswith("/trades"):
                    return {"data": rows, "pagination": {"has_more": True, "next_cursor": "resume-delta"}}
        return {"data": rows, "pagination": {"has_more": False, "next_cursor": None}}

    client = PolymarketClient()
    client.get = get
    try:
        result = client.history(WALLET, as_of, previous=previous, repair_targets=[])
        assert (
            previous == before
            and len(result["trades"]) == 22
            and len(result["activity"]) == (39 if page_failure else 40)
        )
        assert result["open_positions"][0]["current_size"] == 22
        if not page_failure:
            assert len(result["local_history"]["trades"]) == 21
        if not page_failure:
            assert all(
                t["from_block"] == 1001 and t["to_block"] == 1100 and t.get("condition_id")
                for t in reader.call_args.kwargs["targets"]
            )
            assert all(t.get("condition_id") for t in operations.call_args.kwargs["targets"])
        assert result["coverage"]["trades"]["complete"] is not page_failure
        if page_failure:
            page_failure = False
            queries.clear()
            repaired = client.history(WALLET, as_of, previous=result, repair_targets=[])
            assert len(queries) == 2 and queries[0][1]["cursor"] == "resume-delta"
            assert queries[1][0].endswith("/activity")
            assert repaired["coverage"]["trades"]["complete"] and len(repaired["trades"]) == 22
            assert reader.call_count == 2 and operations.call_count == 1
    finally:
        client.close()


def test_settlement_poll_uses_existing_formal_fact_projection(monkeypatch):
    row = {
        "condition_id": "0x" + "12" * 32,
        "payout": "[1,0]",
        "event_time": AS_OF,
        "block_number": 900,
        "tx_hash": "0x" + "34" * 32,
    }
    query = Mock(return_value=[row])
    monkeypatch.setattr("smart_money.infrastructure.local_history._postgres_query", query)
    result = read_resolutions([row["condition_id"]], AS_OF)
    assert result[row["condition_id"]] == {
        "condition_id": row["condition_id"],
        "status": "RESOLVED",
        "payouts": [1, 0],
        "resolved_at": AS_OF,
        "resolved_block": 900,
        "transaction_hash": row["tx_hash"],
    }
    assert "event_status='settle'" in query.call_args.args[0] and CTF in query.call_args.args[0]
    assert read_resolutions([], AS_OF) == {} and query.call_count == 1
    with pytest.raises(ValueError, match="INVALID_CONDITION_ID"):
        read_resolutions(["untrusted condition"], AS_OF)


def test_transfer_batch_and_saved_replay_preserve_inventory_and_block_boundaries():
    words = [64, 160, 2, 2, 3, 2, 20000000, 5000000]
    log = {
        "address": CTF,
        "topics": [TRANSFER_BATCH, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + WALLET[2:].zfill(64)],
        "data": "0x" + "".join(f"{n:064x}" for n in words),
    }
    assert transfer_changes(log, WALLET) == {"2": Decimal(20), "3": Decimal(5)}
    saved = {
        "transactions": {
            str(index): {
                "block_number": block,
                "receipt_json": {
                    "transactionIndex": hex(index),
                    "logs": [
                        dict(
                            log,
                            topics=[TRANSFER_SINGLE, *log["topics"][1:]],
                            data=f"0x{2:064x}{amount:064x}",
                        )
                    ],
                },
            }
            for index, (block, amount) in enumerate(((1002, 5000000), (1002, 7000000), (1003, 11000000)))
        }
    }
    assert _replay_balance(saved, WALLET, "2", Decimal(20), 1001, 1002, before_index=1) == 25
    assert _replay_balance(saved, WALLET, "2", Decimal(20), 1001, 1002) == 32
    assert _replay_balance(saved, WALLET, "2", Decimal(20), 1001, 1003) == 43
    log["topics"][2] = log["topics"][3]
    assert transfer_changes(log, WALLET) == {}
    log["topics"][2] = "0x" + "00" * 32
    words[1] = 128
    log["data"] = "0x" + "".join(f"{n:064x}" for n in words)
    with pytest.raises(ValueError, match="INVALID_TRANSFER_BATCH"):
        transfer_changes(log, WALLET)


@pytest.mark.parametrize("case", ["missing_outcomes", "unavailable", "conflict", "complete"])
def test_chain_reader_completes_local_market_mapping_without_guessing_outcomes(monkeypatch, case):
    local = {"condition_id": "0x" + "12" * 32, "title": "Will Bitcoin exceed target?", "clob_token_ids": ["2", "3"]}
    official = {**local, "outcomes": ["Yes", "No"]}
    if case == "complete":
        local = official
    elif case == "conflict":
        official = {**official, "condition_id": "0x" + "34" * 32}
    monkeypatch.setattr("smart_money.infrastructure.chain.read_token_markets", lambda tokens: [local])
    reader = object.__new__(ChainReader)
    reader.official = SimpleNamespace(get=Mock(return_value=[official]))
    if case == "unavailable":
        reader.official.get.side_effect = OSError("metadata unavailable")
    if case == "conflict":
        with pytest.raises(ValueError, match="AMBIGUOUS_TOKEN_MARKET"):
            reader.markets(["2"])
        return
    result = reader.markets(["2"])
    if case == "unavailable":
        assert result == {}
    else:
        assert result["2"]["payload"]["outcomes"] == ["Yes", "No"]
        assert result["2"]["payload"]["condition_id"] == local["condition_id"]
        assert result["2"]["source"] == ("core.market_tokens+core.markets" if case == "complete" else "gamma:markets")
    if case == "complete":
        reader.official.get.assert_not_called()
    else:
        reader.official.get.assert_called_once_with("https://gamma-api.polymarket.com/markets", {"clob_token_ids": "2"})


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "removed",
        "block",
        "receipt",
        "duplicate",
        "wrong_chain",
        "syncing",
        "pruned",
        "state_unavailable",
        "transport",
    ],
)
def test_chain_reader_preserves_rpc_filter_scope_and_rejects_inconsistent_receipts(fault):
    facts = fee_history()
    evidence = deepcopy(next(iter(facts["local_history"]["receipts"].values())))
    fill = facts["local_history"]["trades"][1]
    log = deepcopy(evidence["receipt_json"]["logs"][0])
    number = evidence["block_number"]
    header = {
        "number": hex(number),
        "hash": log["blockHash"],
        "timestamp": hex(int(datetime.now(timezone.utc).timestamp())),
    }
    transfer = dict(
        log,
        address=CTF,
        logIndex="0x1",
        topics=[TRANSFER_SINGLE, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + WALLET[2:].zfill(64)],
        data=f"0x{2:064x}{20000000:064x}",
    )
    evidence["receipt_json"]["logs"].append(transfer)
    if fault == "removed":
        log["removed"] = True
    elif fault == "block":
        evidence["transaction_json"]["blockHash"] = "0x" + "99" * 32
    elif fault == "receipt":
        evidence["receipt_json"]["transactionHash"] = "0x" + "99" * 32
    elif fault == "duplicate":
        evidence["receipt_json"]["logs"].append(deepcopy(log))

    def rpc(method, params):
        if method == "eth_getLogs":
            query = params[0]
            if len(query["topics"]) == 1:
                assert query["address"] == CTF
                return [deepcopy(transfer)]
            assert query["fromBlock"] == query["toBlock"] == hex(number)
            assert query["topics"][1] is None
            position = 2 if len(query["topics"]) == 3 else 3
            return [
                deepcopy(row)
                for row in (log, log, transfer)
                if row["address"] in query["address"] and row["topics"][position] in query["topics"][position]
            ]
        if method == "eth_call":
            assert params[1] == hex(number - 1)
            if fault == "pruned":
                raise RuntimeError("missing trie node at private-endpoint")
            if fault == "state_unavailable":
                raise RuntimeError(
                    "historical state be345e3e2f7fe9b5ad77bd1ef1ef6d99dc9ba11fa3f8a9fb9c61780eb99ecb7d is not available"
                )
            if fault == "transport":
                raise ConnectionError("private-endpoint timed out")
            return "0x" + "00" * 32
        return {
            "eth_chainId": "0x1" if fault == "wrong_chain" else "0x89",
            "eth_syncing": fault == "syncing",
            "eth_getTransactionReceipt": evidence["receipt_json"],
            "eth_getTransactionByHash": evidence["transaction_json"],
            "eth_getBlockByNumber": header,
        }[method]

    reader = object.__new__(ChainReader)
    # Canonical decoded fields are the existing upstream adapter contract, not a second ABI decoder.
    decoded = {
        **fill,
        "maker": WALLET,
        "taker": "0x" + "00" * 20,
        "maker_asset_id": "0",
        "taker_asset_id": "2",
        "maker_amount": "10000000",
        "taker_amount": "20000000",
        "fee": "250000",
        "tx_hash": fill["transaction_hash"],
    }
    reader.collector = SimpleNamespace(
        EXCHANGES=(fill["contract"],),
        LEGACY_TOPIC=LEGACY_FILL_TOPIC,
        V2026_TOPIC=V2_FILL_TOPIC,
        decode_log=Mock(return_value=decoded),
    )
    reader.rpc = SimpleNamespace(call=Mock(side_effect=rpc))
    if fault in {"wrong_chain", "syncing", "pruned", "state_unavailable", "transport"}:
        expected = (
            HistoricalStateUnavailable
            if fault in {"pruned", "state_unavailable"}
            else OSError
            if fault == "transport"
            else ValueError
        )
        with pytest.raises(expected) as error:
            reader.preflight(300)
        assert type(error.value) is expected
        assert "private-endpoint" not in str(error.value)
        if fault == "wrong_chain":
            assert reader.rpc.call.call_count == 1
    elif fault:
        with pytest.raises(ValueError):
            reader.transactions([WALLET], number, number)
    else:
        txs = reader.transactions([WALLET], number, number)
        assert len(txs) == 1 and len(txs[0]["fills"]) == 1
        assert (txs[0]["fills"][0]["size"], txs[0]["fills"][0]["notional"]) == ("20", "10")
        assert sum(c.args[0] == "eth_getTransactionReceipt" for c in reader.rpc.call.call_args_list) == 1
        reader.collector.decode_log.assert_called_once()
        proof = reader.preflight(300)
        assert proof["status"] == "READY" and proof["balance_block"] == number - 1
        assert proof["receipt_ref"] == log["transactionHash"]


@pytest.mark.parametrize("age,expected", [(180, 20), (181, 19), (0, 19)])
def test_fixed_settlement_window_keeps_cross_period_costs(age, expected):
    facts = history()
    condition = facts["open_positions"][0]["condition_id"]
    # Entry precedes the window. Its cost remains required for a recently settled event.
    old_entry = int((NOW - timedelta(days=200)).timestamp())
    facts["open_positions"][0]["last_event_at"] = old_entry
    for rows in (facts["trades"], facts["activity"], facts["local_history"]["trades"]):
        for row in rows:
            if row["condition_id"] == condition and row.get("side") == "BUY":
                row["timestamp"] = old_entry
    facts["resolutions"][condition]["resolved_at"] = (NOW - timedelta(days=age)).isoformat()
    result = evaluate_history(facts, ["CRYPTO"], as_of=AS_OF)["profiles"]["CRYPTO"]
    assert result["metrics"]["directional_events"] == expected
    assert result["metrics"]["sector_pnl"] == (110 if expected == 20 else 100)
    if age == 180:
        assert next(e for e in result["events"] if condition in e["condition_ids"])["pnl"] == 10
        assert result["historical_eligible"]


def test_unrelated_sector_fee_gap_does_not_revoke_complete_crypto_scope():
    facts = history()
    other = deepcopy(history())
    old, new = other["closed_positions"][0]["condition_id"], "0x" + "aa" * 32
    market = other["markets"][old]
    market.update(
        conditionId=new,
        category="sports",
        question="Will the Lakers win the NBA Finals?",
        clobTokenIds=["2000", "2001"],
    )
    for event in market["events"]:
        event.update(id="sports-independent-event", slug="lakers-finals")
    facts["markets"][new] = market
    facts["resolutions"][new] = {**other["resolutions"][old], "condition_id": new}
    for feed in ("closed_positions", "trades", "activity"):
        for row in other[feed]:
            if row.get("condition_id") == old:
                facts[feed].append(
                    {**row, "condition_id": new, "token_id": "2000", "transaction_hash": "0x" + "bb" * 32}
                )
    row = next(r for r in other["local_history"]["trades"] if r["condition_id"] == old)
    facts["local_history"]["trades"].append(
        {
            **row,
            "condition_id": new,
            "token_id": "2000",
            "trade_id": "sports-fill",
            "transaction_hash": "0x" + "bb" * 32,
            "fee_usdc": None,
        }
    )
    result = evaluate_history(facts, ["CRYPTO", "SPORTS"], as_of=AS_OF)["profiles"]
    assert result["CRYPTO"]["historical_eligible"] and result["CRYPTO"]["metrics"]["sector_pnl"] == 110
    assert not result["SPORTS"]["historical_eligible"] and result["SPORTS"]["reviews"]["data"]["status"] == "UNRESOLVED"


@pytest.mark.parametrize("external_change", [False, True])
def test_wallet_slice_checkpoint_and_external_pause_are_preserved(tmp_path, monkeypatch, external_change):
    destination = tmp_path / "watchlist.json"
    data = packet()
    second = "0x" + "ef" * 20
    second_facts = json.loads(json.dumps(history()).replace(WALLET, second))
    data["candidates"].append({"account": second, "sector_id": "CRYPTO", "source": "local"})
    data["profiles"][second] = {"wallet": second, "evidence": {"account": second}}
    data["histories"][second] = second_facts
    original = refresh_watchlist(destination, inputs=data, offline=True, categories=["crypto"])
    monkeypatch.setattr("smart_money.watchlist.read_resolutions", Mock(return_value=history()["resolutions"]))
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW + timedelta(minutes=1)
    monkeypatch.setattr("smart_money.watchlist.datetime", clock)
    calls = []

    def fetch(self, wallet, as_of, **kwargs):
        calls.append(wallet)
        if external_change:
            changed = json.loads(destination.read_text())
            changed["records"][f"{wallet}:CRYPTO"]["manual_paused"] = True
            write_json(destination, changed)
            return deepcopy(data["histories"][wallet])
        facts = deepcopy(data["histories"][wallet])
        facts["as_of"] = facts["local_history"]["as_of"] = as_of
        facts["local_history"]["coverage"]["boundary"].update(
            block_time=as_of, next_block_time=(datetime.fromisoformat(as_of) + timedelta(seconds=2)).isoformat()
        )
        if wallet == WALLET:
            facts["coverage"]["trades"]["complete"] = False
            facts["work"] = {
                "phase": "FACTS",
                "index_complete": True,
                "outcome": "YIELDED",
                "pages": 10,
                "requests": 10,
                "batch": 0,
                "step": 0,
                "segments": {"0": {"trades": {"next_cursor": "page-11"}}},
            }
        else:
            saved = json.loads(destination.read_text())
            assert saved["tasks"][WALLET]["outcome"] == "YIELDED"
            assert (
                load_history(saved, saved["tasks"][WALLET]["history_ref"])["work"]["segments"]["0"]["trades"][
                    "next_cursor"
                ]
                == "page-11"
            )
        return facts

    monkeypatch.setattr(PolymarketClient, "history", fetch)
    if external_change:
        with pytest.raises(ValueError, match="WATCHLIST_CHANGED_DURING_REFRESH"):
            refresh_watchlist(destination, due=True, categories=["crypto"])
        assert json.loads(destination.read_text())["records"][f"{WALLET}:CRYPTO"]["manual_paused"]
    else:
        done = refresh_watchlist(destination, due=True, categories=["crypto"])
        assert calls == [WALLET, second]
        assert done["tasks"][WALLET]["outcome"] == "YIELDED"
        assert done["records"][f"{second}:CRYPTO"]["historical_eligible"]
        assert all(load_evaluation(done, r["evaluation_ref"], detail=True) for r in original["records"].values())


def test_storage_migration_is_lossless_resumable_and_keeps_hot_reads_small(tmp_path, monkeypatch):
    source, target = tmp_path / "old.json", tmp_path / "current.json"
    old = {"schema_version": 2, "candidates": [], "records": {}, "facts": {}, "histories": {}, "evaluations": {}}

    def intern(kind, value):
        key = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        old[kind][key] = value
        return key

    trades = [{"transaction_hash": "same", "log_index": i, "raw_amount": str(2**90 + i)} for i in range(501)]
    manifest = {
        "wallet": intern("facts", WALLET),
        "as_of": intern("facts", AS_OF),
        "trades": [intern("facts", r) for r in trades],
        "markets": {},
    }
    condition = "0x" + "12" * 32
    market = {"condition_id": condition}
    resolution = {"condition_id": condition, "resolved_at": AS_OF, "payouts": [1, 0]}
    manifest["markets"][condition] = intern("facts", market)
    old["maintenance"] = {
        "daily_date": NOW.date().isoformat(),
        "pending": {},
        "settlements": {condition: intern("facts", resolution)},
    }
    history_ref = intern("histories", manifest)
    reasons = [f"FEE_UNVERIFIED:tx:{i}" for i in range(10000)]
    profile = {
        "historical_eligible": False,
        "status": "PENDING_REVIEW",
        "reasons": reasons,
        "metrics": {"net_profit": "-0.1234567890123456789", "events": 501},
        "reviews": {"data": {"status": "UNRESOLVED", "reasons": reasons}},
    }
    evaluation_ref = intern(
        "evaluations",
        {
            "candidate_id": WALLET,
            "as_of": AS_OF,
            "history_ref": history_ref,
            "profiles": {"CRYPTO": profile},
            "failure": None,
            "reconciliation": None,
        },
    )
    row = {
        **profile,
        "wallet": WALLET,
        "sector_id": "CRYPTO",
        "candidate_id": WALLET,
        "forward_status": "SHADOW_OBSERVE",
        "manual_paused": True,
        "stale": True,
        "evaluation_ref": evaluation_ref,
        "eligibility_effective_at": AS_OF,
        "follow_up_refs": [str(i) for i in range(10)],
        "monitor": {"enabled": False, "mode": "DISABLED"},
        "changes": [{"at": AS_OF, "evaluation_ref": evaluation_ref, "after": profile}] * 7,
    }
    old["records"][WALLET + ":CRYPTO"] = row
    source.write_text(json.dumps(old))
    original = source.read_bytes()
    with source.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            migrate_watchlist(source, target)
    original_block = Archive._block
    attempts = 0

    def interrupt(archive, body):
        nonlocal attempts
        attempts += 1
        if attempts == 5:
            raise OSError("interrupted migration")
        return original_block(archive, body)

    monkeypatch.setattr(Archive, "_block", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        migrate_watchlist(source, target)
    assert not target.exists() and source.read_bytes() == original
    monkeypatch.setattr(Archive, "_block", original_block)
    result = migrate_watchlist(source, target)
    state = read_watchlist(target)
    assert result["facts"] == len(old["facts"])
    assert load_history(state, history_ref) == {
        "wallet": WALLET,
        "as_of": AS_OF,
        "trades": trades,
        "markets": {condition: market},
    }
    assert load_evaluation(state, evaluation_ref, detail=True) == old["evaluations"][evaluation_ref]
    current = state["records"][WALLET + ":CRYPTO"]
    assert current["manual_paused"] and current["follow_up_refs"] == row["follow_up_refs"]
    assert current["metrics"] == row["metrics"] and current["evaluation_ref"] == evaluation_ref
    assert current["reason_summary"]["counts"] == {"FEE_UNVERIFIED": 10000}
    assert len(json.dumps(current)) < 8192 and len(current["changes"]) == 5
    assert source.read_bytes() == original
    from smart_money.watchlist import _maintenance_scope

    monkeypatch.setattr("smart_money.watchlist.read_resolutions", Mock(return_value={condition: resolution}))
    _maintenance_scope(state, AS_OF)
    assert not state["maintenance"]["pending"]  # A storage encoding change is not a new settlement.
    before_blocks = set(archive_for(state).root.rglob("*.zst"))
    monkeypatch.setattr(Archive, "get", Mock(side_effect=AssertionError("Hot reads must not load archives")))
    current["manual_paused"] = False
    write_watchlist(target, state)
    assert read_watchlist(target)["records"][WALLET + ":CRYPTO"]["manual_paused"] is False
    assert set(archive_for(state).root.rglob("*.zst")) == before_blocks


@pytest.mark.parametrize("stage", ["evidence", "evaluation", "current"])
def test_storage_commit_interruption_never_advances_the_current_view(tmp_path, monkeypatch, stage):
    from smart_money.infrastructure import wallet_storage

    path = tmp_path / "list.json"
    before = refresh_watchlist(path, inputs=packet(), offline=True, categories=["crypto"])
    content = path.read_bytes()
    original = wallet_storage.atomic_bytes

    def fail(destination, body):
        selected = (
            (stage == "evidence" and "blocks" in destination.parts)
            or (stage == "evaluation" and "evaluations" in destination.parts)
            or (stage == "current" and destination == path)
        )
        if selected:
            raise OSError("commit interrupted")
        original(destination, body)

    monkeypatch.setattr(wallet_storage, "atomic_bytes", fail)
    data = packet()
    data["histories"][WALLET]["coverage"]["trades"]["complete"] = False
    with pytest.raises(OSError, match="commit interrupted"):
        refresh_watchlist(path, inputs=data, offline=True, categories=["crypto"])
    assert path.read_bytes() == content
    reference = before["records"][f"{WALLET}:CRYPTO"]["evaluation_ref"]
    assert load_evaluation(read_watchlist(path), reference)["profiles"]["CRYPTO"]["historical_eligible"]
