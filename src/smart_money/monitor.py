"""Persist confirmed observations and deliver them to the existing MAS independently."""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from smart_money.infrastructure.chain import ChainReader, HistoricalStateUnavailable
from smart_money.infrastructure.wallet_storage import read_watchlist
from smart_money.markets.taxonomy import normalize_market
from smart_money.markets.trades import decimal_value, transfer_changes
from smart_money.research.engine import MasEngine
from smart_money.research.models import AnalysisRequest, SignalCandidate, WalletProfilePacket
from smart_money.signals.realtime import transaction_observations
from smart_money.wallets.history import instant
from smart_money.watchlist import write_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refresh_qualifications(state: dict[str, Any], watchlist: dict[str, Any], now: str) -> set[str]:
    if watchlist.get("schema_version") != 3:
        raise ValueError("Unsupported watchlist schema")
    confirmed = {c["wallet"]: c for c in watchlist["candidates"] if c["mapping_status"] == "CONFIRMED"}
    enabled = set()
    seen = set()
    fields = (
        "wallet",
        "sector_id",
        "status",
        "historical_eligible",
        "forward_status",
        "stale",
        "manual_paused",
        "monitor",
        "data_cutoff",
        "eligibility_effective_at",
        "metrics",
        "policy_version",
        "evaluation_ref",
        "valid_until",
    )
    for row in watchlist["records"].values():
        wallet = row["wallet"]
        active = bool(
            wallet in confirmed
            and not row["manual_paused"]
            and row["monitor"]["enabled"]
            and row["monitor"]["mode"] in {"NORMAL", "EXPLORE"}
        )
        if row["monitor"]["mode"] == "UPDATE_ONLY" and row.get("follow_up_refs") and wallet in confirmed:
            enabled.add(wallet)
        if not active and wallet not in state["qualifications"] and wallet not in enabled:
            continue
        if active:
            enabled.add(wallet)
        record = {key: deepcopy(row.get(key)) for key in fields}
        record["display_name"] = confirmed.get(wallet, {}).get("username")
        record["discovery_sources"] = [
            deepcopy(source)
            for source in confirmed.get(wallet, {}).get("sources", [])
            if source.get("source", "").startswith("official:")
            and source.get("sector_id") in {row["sector_id"], row["sector_id"].split(".", 1)[0]}
            and source.get("last_seen_at")
            and instant(source["last_seen_at"]) <= instant(now)
        ]
        record["monitor"]["enabled"] = active
        versions = state["qualifications"].setdefault(wallet, [])
        prior = next((v for v in reversed(versions) if v["record"]["sector_id"] == row["sector_id"]), None)
        if prior is None or prior["record"] != record:
            ref = hashlib.sha256(json.dumps([now, record], sort_keys=True).encode()).hexdigest()
            versions.append({"reference": ref, "record": record, "available_at": now})
        seen.add((wallet, row["sector_id"]))
    # Removing a record revokes future use; prior point-in-time qualification stays immutable.
    for wallet, versions in state["qualifications"].items():
        for sector in {v["record"]["sector_id"] for v in versions}:
            latest = next(v for v in reversed(versions) if v["record"]["sector_id"] == sector)
            if (wallet, sector) not in seen and latest["record"]["monitor"]["enabled"]:
                retired = deepcopy(latest)
                retired["record"]["monitor"]["enabled"] = False
                retired.update(available_at=now, reference=latest["reference"] + ":removed:" + now)
                versions.append(retired)
    for wallet, account in state.get("wallets", {}).items():
        inventory = any(n is None or decimal_value(n) > 0 for n in account["positions"].values())
        jobs = [o for o in state.get("observations", {}).values() if o["wallet"] == wallet]
        outstanding = any(
            o.get("research", {}).get("status") in {"PENDING", "RUNNING", "FAILED"} or o.get("follow_up_refs")
            for o in jobs
        )
        if inventory or outstanding:
            enabled.add(wallet)
        account["desired_monitoring"] = wallet in enabled
    return enabled


def _cache_markets(state: dict[str, Any], reader: ChainReader, tokens: set[str]) -> set[str]:
    missing = sorted(tokens - state["token_markets"].keys())
    pending = state.setdefault("pending_markets", {})
    for offset in range(0, len(missing), 20):
        batch = missing[offset : offset + 20]
        try:
            snapshots = reader.markets(batch)
        except OSError as exc:
            snapshots = {}
            for token in batch:
                pending[token] = str(exc)
        for snapshot in snapshots.values():
            market = normalize_market(snapshot["payload"])
            if market:
                state["markets"].setdefault(market.condition_id, snapshot)
                for token in market.token_ids:
                    if token in state["token_markets"] and state["token_markets"][token] != market.condition_id:
                        raise ValueError("CONFLICTING_TOKEN_MARKET")
                    state["token_markets"][token] = market.condition_id
                    tokens.add(token)
        for token in batch:
            if token in state["token_markets"]:
                pending.pop(token, None)
            else:
                pending.setdefault(token, "MARKET_MAPPING_UNVERIFIED")
    for token in list(tokens):
        if token in state["token_markets"]:
            market = normalize_market(state["markets"][state["token_markets"][token]]["payload"])
            if market:
                tokens.update(market.token_ids)
    return tokens


def _replay_balance(
    state: dict[str, Any],
    wallet: str,
    token: str,
    size: Decimal,
    start: int,
    end: int,
    before_index: int | None = None,
) -> Decimal:
    """Replay only saved, confirmed transfers, including earlier transactions within a block."""
    for tx in state["transactions"].values():
        if not start < tx["block_number"] <= end:
            continue
        if (
            before_index is not None
            and tx["block_number"] == end
            and int(tx["receipt_json"]["transactionIndex"], 16) >= before_index
        ):
            continue
        for log in tx["receipt_json"]["logs"]:
            size += transfer_changes(log, wallet).get(token, Decimal(0))
    if size < 0:
        raise ValueError("NEGATIVE_POSITION_REQUIRES_RECONCILIATION")
    return size


def _baseline_tokens(
    state: dict[str, Any],
    reader: ChainReader,
    wallet: str,
    tokens: set[str],
    boundary: dict[str, Any],
) -> None:
    account = state["wallets"][wallet]
    for token in sorted(tokens - account["positions"].keys()):
        baseline = {"block": boundary, "size": None, "cost": None, "kind": "BASELINE_POSITION"}
        account["baselines"][token] = baseline
        try:
            baseline["size"] = str(reader.balance(wallet, token, boundary["number"]))
        except OSError as exc:
            account.setdefault("gaps", []).append(
                {
                    "token": token,
                    "from_block": boundary["number"],
                    "status": "PENDING",
                    "detected_at": _now(),
                    "error": str(exc),
                    "sample_eligible": False,
                }
            )
        account["positions"][token] = baseline["size"]
        if baseline["size"] is not None and state.get("cursor"):
            account["positions"][token] = str(
                _replay_balance(
                    state,
                    wallet,
                    token,
                    decimal_value(baseline["size"]),
                    boundary["number"],
                    state["cursor"]["number"],
                )
            )


def _recover_baselines(state: dict[str, Any], reader: ChainReader, wallet: str, head: dict[str, Any]) -> None:
    account = state["wallets"][wallet]
    for gap in account.get("gaps", []):
        if gap["status"] != "PENDING":
            continue
        token = gap["token"]
        baseline = account["baselines"][token]
        boundary = baseline["block"]
        try:
            if reader.block(boundary["number"])["hash"] != boundary["hash"]:
                raise ValueError("CONFIRMED_BASELINE_CHANGED")
            try:
                size = reader.balance(wallet, token, boundary["number"])
                status = "REPLAYED"
            except HistoricalStateUnavailable:
                size = reader.balance(wallet, token, head["number"])
                boundary, status = head, "REBASED_WITH_HISTORY_GAP"
        except OSError as exc:
            gap["error"] = str(exc)
            continue
        gap.update(
            status=status,
            to_block=max(boundary["number"], state["cursor"]["number"]),
            recovered_at=_now(),
            baseline=baseline,
        )
        account["baselines"][token] = {**baseline, "block": boundary, "size": str(size)}
        if boundary["number"] > state["cursor"]["number"]:
            # Install only when the global cursor reaches this baseline; intervening facts stay unknown.
            continue
        account["positions"][token] = str(
            _replay_balance(
                state,
                wallet,
                token,
                size,
                boundary["number"],
                state["cursor"]["number"],
            )
        )


def _install_baselines(
    state: dict[str, Any],
    reader: ChainReader,
    wallet: str,
    end: int,
    before_index: int | None = None,
) -> None:
    account = state["wallets"][wallet]
    for token, baseline in account["baselines"].items():
        boundary = baseline["block"]
        if account["positions"][token] is not None or baseline["size"] is None:
            continue
        if boundary["number"] > end or (before_index is not None and boundary["number"] == end):
            continue
        if reader.block(boundary["number"])["hash"] != boundary["hash"]:
            raise ValueError("CONFIRMED_BASELINE_CHANGED")
        account["positions"][token] = str(
            _replay_balance(
                state,
                wallet,
                token,
                decimal_value(baseline["size"]),
                boundary["number"],
                end,
                before_index,
            )
        )


def _recover_observations(state: dict[str, Any], reader: ChainReader, live_max_delay_seconds: int) -> None:
    for observation in state["observations"].values():
        context = observation.get("pending_context")
        if context is None:
            continue
        tokens = set(context["before"])
        expanded = _cache_markets(state, reader, {observation["token_id"]})
        if observation["token_id"] not in state["token_markets"]:
            continue
        tx, wallet = state["transactions"][observation["transaction_ref"]], observation["wallet"]
        before = {t: decimal_value(n) if n is not None else None for t, n in context["before"].items()}
        after = {t: decimal_value(n) if n is not None else None for t, n in context["after"].items()}
        try:
            for token in expanded - tokens:
                try:
                    size = reader.balance(wallet, token, tx["block_number"] - 1)
                except HistoricalStateUnavailable:
                    before[token] = after[token] = None
                    continue
                size = _replay_balance(
                    state,
                    wallet,
                    token,
                    size,
                    tx["block_number"] - 1,
                    tx["block_number"],
                    int(tx["receipt_json"]["transactionIndex"], 16),
                )
                before[token] = size
                for log in tx["receipt_json"]["logs"]:
                    size += transfer_changes(log, wallet).get(token, Decimal(0))
                after[token] = size
        except OSError as exc:
            context["error"] = str(exc)
            continue
        revised = transaction_observations(
            tx,
            wallet,
            before=before,
            after=after,
            changed_tokens=set(context["changed_tokens"]),
            markets={t: state["markets"][c] for t, c in state["token_markets"].items()},
            qualifications=state["qualifications"][wallet],
            observed_at=observation["evidence"]["signal"]["first_observed_at"],
            live_max_delay_seconds=live_max_delay_seconds,
            inventory_complete=context["inventory_complete"],
        )
        supplement = next(o for o in revised if o["observation_id"] == observation["observation_id"])
        # Complete the same observation; preserve first detection and never enqueue it twice.
        observation["supplement"] = {
            "at": _now(),
            "evidence": supplement["evidence"],
            "research_eligible": supplement["research_eligible"],
        }
        if supplement["research_eligible"] and observation["research"]["status"] == "NOT_SUBMITTED":
            request = _analysis_request(
                supplement, state["markets"][state["token_markets"][observation["token_id"]]], _now()
            ).model_dump(mode="json", exclude={"candidate": {"evidence"}, "context": {"market"}})
            observation["request"] = _link_research_update(state, supplement, request)
            observation["research"].update(status="PENDING")
            observation["research_eligible"] = True
        del observation["pending_context"]


def _analysis_request(observation: dict[str, Any], snapshot: dict[str, Any], now: str) -> AnalysisRequest:
    evidence = observation["evidence"]
    market, trade = evidence["market"], evidence["trade"]
    version = observation["qualification"]
    row = version["record"]
    # Incomplete history can contain empty-sample zeros; those are not verified wallet statistics.
    metrics = (row.get("metrics") or {}) if row["status"] != "PENDING_REVIEW" and not row["stale"] else {}
    qualified = (
        row["historical_eligible"]
        and row["status"] == "QUALIFIED"
        and not row["stale"]
        and (not row.get("valid_until") or instant(evidence["signal"]["trade_at"]) < instant(row["valid_until"]))
        and evidence["wallet"]["sector_match"]
    )
    profile = WalletProfilePacket(
        wallet=observation["wallet"],
        display_name=row.get("display_name"),
        discovery_sources=row.get("discovery_sources", []),
        wallet_validation_status="QUALIFIED_OBSERVER" if qualified else "UNPROFILED",
        profile_role="MONITORED_WALLET",
        profile_snapshot_at=instant(version["available_at"]),
        profile_source_version=row["policy_version"],
        market_sector=market["primary_sector"],
        source_profile_sector=row["sector_id"],
        sector_match=evidence["wallet"]["sector_match"],
        sector_pnl=metrics.get("sector_pnl"),
        sector_resolved_count=metrics.get("directional_events"),
        sector_win_rate=metrics.get("win_rate"),
        sector_profit_factor=metrics.get("profit_factor"),
        recent_window_days=90,
        recent_sector_pnl=metrics.get("recent_sector_pnl"),
        recent_sector_resolved_count=metrics.get("recent_events"),
        recent_sector_win_rate=metrics.get("recent_win_rate"),
        current_trade_size=float(trade["current_trade_notional"]),
        position_change=trade["action"],
        position_before=trade["position_before"],
        position_after=trade["position_after"],
        admission_status=row["forward_status"],
        evidence_ids=[version["reference"]],
        two_sided_ratio=metrics.get("two_sided_ratio"),
    )
    return AnalysisRequest(
        candidate=SignalCandidate(
            candidate_id=observation["observation_id"],
            as_of=instant(evidence["signal"]["trade_at"]),
            wallet=observation["wallet"],
            market_id=market["condition_id"],
            event_cluster_id=market["event_cluster_id"],
            sector_id=market["primary_sector"],
            side=trade["side"],
            outcome=trade["outcome"],
            entry_price=float(trade["wallet_entry_price"]),
            notional=float(trade["current_trade_notional"]),
            signal_type=evidence["signal"]["signal_type"],
            wallet_profiles=[profile],
            evidence=evidence,
            source_snapshot_ids=[observation["transaction_ref"], version["reference"]],
        ),
        context={
            "market": snapshot["payload"],
            "market_obtained_at": snapshot["obtained_at"],
            "trade_at": evidence["signal"]["trade_at"],
            "research_requested_at": now,
        },
    )


def _link_research_update(
    state: dict[str, Any], observation: dict[str, Any], request: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Link a new frozen observation without overwriting the parent research or its evidence."""
    trade = observation["evidence"]["trade"]
    action = trade.get("action")
    if action not in {"OPEN", "ADD", "REDUCE", "EXIT", "NON_TRADE"}:
        return request
    parents = [
        o
        for o in state["observations"].values()
        if o["wallet"] == observation["wallet"] and o["token_id"] == observation["token_id"] and o.get("request")
    ]
    if not parents:
        return request
    parent = parents[-1]
    if request is None:
        allowed = {"NOT_A_CONFIRMED_TRADE", "NOT_MONITORED_AT_TRADE"}
        if set(observation["evidence"]["signal"].get("suppression_reasons", [])) - allowed:
            return None
        if action not in {"REDUCE", "EXIT", "NON_TRADE"}:
            return None
        request = deepcopy(parent["request"])
        candidate = request["candidate"]
        candidate.update(
            candidate_id=observation["observation_id"],
            as_of=observation["evidence"]["signal"]["trade_at"],
            side=trade.get("side"),
            outcome=trade.get("outcome"),
            entry_price=trade.get("wallet_entry_price"),
            notional=trade.get("current_trade_notional"),
            signal_type="RESEARCH_UPDATE",
            source_snapshot_ids=[observation["transaction_ref"]],
        )
        for profile in candidate.get("wallet_profiles", []):
            profile.update(
                position_change=action,
                position_before=trade.get("position_before"),
                position_after=trade.get("position_after"),
                current_trade_size=trade.get("current_trade_notional"),
                current_trade_size_multiple=None,
            )
        request["context"].pop("research_cutoff", None)
        market_snapshot = state.get("markets", {}).get(candidate["market_id"], {})
        request["context"]["market_obtained_at"] = market_snapshot.get("obtained_at")
        request["context"]["trade_at"] = candidate["as_of"]
    request["context"]["research_requested_at"] = _now()
    request["context"]["parent_research"] = {
        "observation_id": parent["observation_id"],
        "run_id": (parent["research"].get("result") or {}).get("run_id"),
        "reason": action,
    }
    linked = parent["research"].setdefault("update_observation_ids", [])
    if observation["observation_id"] not in linked:
        linked.append(observation["observation_id"])
    return request


def scan_once(
    state: dict[str, Any],
    reader: ChainReader,
    enabled: set[str],
    *,
    block_batch_size: int,
    max_block_age_seconds: int,
    live_max_delay_seconds: int,
) -> dict[str, Any]:
    """Stage a complete block batch. The caller commits it before advancing or invoking MAS."""
    state = deepcopy(state)
    head = reader.finalized(max_block_age_seconds)
    cursor = state.get("cursor")
    if cursor and (head["number"] < cursor["number"] or reader.block(cursor["number"])["hash"] != cursor["hash"]):
        raise ValueError("CONFIRMED_CURSOR_CONFLICT")
    for wallet in sorted(enabled - state["wallets"].keys()):
        state["wallets"][wallet] = {
            "baseline": head,
            "positions": {},
            "baselines": {},
            "seed_pending": True,
            "applied_at": _now(),
            "desired_monitoring": True,
        }
    for wallet, account in state["wallets"].items():
        if wallet not in enabled:
            account["inactive_since"] = account.get("inactive_since") or _now()
            continue
        if account.pop("inactive_since", None):
            account.setdefault("baseline_history", []).append(
                {"baseline": account["baseline"], "positions": account["positions"]}
            )
            account.update(baseline=head, positions={}, baselines={}, seed_pending=True, applied_at=_now())
        if cursor:
            _recover_baselines(state, reader, wallet, head)
        tokens = set(account["positions"])
        if account.get("seed_pending"):
            try:
                tokens.update(reader.seed_tokens(wallet))
                account["seed_pending"] = False
                account.pop("seed_error", None)
            except OSError as exc:
                account["seed_error"] = str(exc)
        # Inventory needs token balances, not metadata for every unrelated holding.
        _baseline_tokens(state, reader, wallet, tokens, account["baseline"])
    _recover_observations(state, reader, live_max_delay_seconds)
    if cursor is None:
        if reader.block(head["number"])["hash"] != head["hash"]:
            raise ValueError("CONFIRMED_BASELINE_CHANGED")
        state["cursor"] = head
        state["health"] = {"status": "BASELINE_READY", "finalized": head, "known_token_scope_only": True}
        return state
    end = min(cursor["number"] + block_batch_size, head["number"])
    # Keep inventory continuous even while research is disabled; re-enabling must not reuse a stale zero.
    tracked = sorted(enabled)
    end_header = reader.block(end)
    transactions = reader.transactions(tracked, cursor["number"] + 1, end) if tracked and end > cursor["number"] else []
    for tx in transactions:
        tx_hash = tx["transaction_hash"]
        if tx_hash in state["transactions"]:
            if state["transactions"][tx_hash] != tx:
                raise ValueError("CONFLICTING_TRANSACTION")
            continue
        state["transactions"][tx_hash] = tx
        observed_at = _now()
        for wallet in tracked:
            account = state["wallets"][wallet]
            if tx["block_number"] <= account["baseline"]["number"]:
                continue
            _install_baselines(
                state, reader, wallet, tx["block_number"], int(tx["receipt_json"]["transactionIndex"], 16)
            )
            changes: dict[str, Decimal] = {}
            for log in tx["receipt_json"]["logs"]:
                for token, delta in transfer_changes(log, wallet).items():
                    changes[token] = changes.get(token, Decimal(0)) + delta
            tokens = set(changes) | {f["token_id"] for f in tx["fills"] if f["proxy_wallet"] == wallet}
            if not tokens:
                continue
            tokens = _cache_markets(state, reader, tokens)
            missing = tokens - account["positions"].keys()
            if missing:
                boundary = reader.block(tx["block_number"] - 1)
                _baseline_tokens(state, reader, wallet, missing, boundary)
                for token in sorted(missing):
                    if account["positions"][token] is not None:
                        account["positions"][token] = str(
                            _replay_balance(
                                state,
                                wallet,
                                token,
                                decimal_value(account["positions"][token]),
                                boundary["number"],
                                tx["block_number"],
                                int(tx["receipt_json"]["transactionIndex"], 16),
                            )
                        )
            before = {t: decimal_value(n) if n is not None else None for t, n in account["positions"].items()}
            after = {t: n + changes.get(t, Decimal(0)) if n is not None else None for t, n in before.items()}
            if any(n is not None and n < 0 for n in after.values()):
                raise ValueError("NEGATIVE_POSITION_REQUIRES_RECONCILIATION")
            observations = transaction_observations(
                tx,
                wallet,
                before=before,
                after=after,
                changed_tokens=set(changes),
                markets={t: state["markets"][c] for t, c in state["token_markets"].items()},
                qualifications=state["qualifications"][wallet],
                observed_at=observed_at,
                live_max_delay_seconds=live_max_delay_seconds,
                inventory_complete=not account.get("seed_pending"),
            )
            for observation in observations:
                identifier = observation["observation_id"]
                request = (
                    _analysis_request(
                        observation, state["markets"][state["token_markets"][observation["token_id"]]], _now()
                    ).model_dump(mode="json", exclude={"candidate": {"evidence"}, "context": {"market"}})
                    if observation["research_eligible"]
                    else None
                )
                qualification = observation.pop("qualification")
                request = _link_research_update(state, observation, request)
                state["observations"][identifier] = {
                    **observation,
                    "qualification_ref": qualification["reference"] if qualification else None,
                    "request": request,
                    "research": {"status": "PENDING" if request else "NOT_SUBMITTED", "attempts": 0},
                }
                if observation["token_id"] not in state["token_markets"]:
                    state["observations"][identifier]["pending_context"] = {
                        "before": {t: str(n) if n is not None else None for t, n in before.items()},
                        "after": {t: str(n) if n is not None else None for t, n in after.items()},
                        "changed_tokens": sorted(changes),
                        "inventory_complete": not account.get("seed_pending"),
                    }
            account["positions"] = {t: str(n) if n is not None else None for t, n in after.items()}
    for wallet in tracked:
        account = state["wallets"][wallet]
        if end <= account["baseline"]["number"]:
            continue
        _install_baselines(state, reader, wallet, end)
        for token, expected in account["positions"].items():
            if expected is not None and reader.balance(wallet, token, end) != decimal_value(expected):
                raise ValueError("POSITION_BALANCE_MISMATCH")
    if reader.block(end)["hash"] != end_header["hash"] or reader.block(head["number"])["hash"] != head["hash"]:
        raise ValueError("CONFIRMED_BATCH_CHANGED")
    needed_markets = {o["token_id"] for o in state["observations"].values() if "pending_context" in o}
    state["pending_markets"] = {
        token: error for token, error in state.get("pending_markets", {}).items() if token in needed_markets
    }
    state["cursor"] = end_header
    state["health"] = {
        "status": "CAUGHT_UP" if end == head["number"] else "CATCHING_UP",
        "finalized": head,
        "lag_blocks": head["number"] - end,
    }
    return state


def _research(engine: MasEngine | None, request: dict[str, Any], offline: bool) -> dict[str, Any]:
    engine = engine or MasEngine(llm_enabled=not offline, osint_enabled=not offline)
    parsed = AnalysisRequest.model_validate(request)
    return engine.run(parsed.candidate, context=parsed.context, evidence=parsed.evidence).model_dump(mode="json")


def _finish_research(job: dict[str, Any], future: Future[dict[str, Any]], offline: bool) -> None:
    try:
        result = future.result()
        stages = result.get("agent_runtime", {}).get("agents", {})
        retryable = not offline and any(s.get("retryable") for s in stages.values())
        if job.get("result"):
            job.setdefault("previous_results", []).append(job.pop("result"))
        job.update(
            status="OFFLINE_DONE" if offline else "FAILED" if retryable else "DONE", result=result, completed_at=_now()
        )
        job.pop("error", None)
        job.setdefault("attempt_history", []).append(
            {
                "attempt": job["attempts"],
                "status": job["status"],
                "run_id": result.get("run_id"),
                "completed_at": job["completed_at"],
            }
        )
    except Exception as exc:
        job.update(status="FAILED", error="MAS_FAILED:" + type(exc).__name__)
        job.setdefault("attempt_history", []).append(
            {"attempt": job["attempts"], "status": "FAILED", "error": job["error"]}
        )


def run_monitor(
    watchlist_path: Path,
    destination: Path,
    *,
    watch: bool = False,
    offline_mas: bool = False,
    block_batch_size: int = 100,
    poll_seconds: float = 5,
    max_block_age_seconds: int = 300,
    live_max_delay_seconds: int = 120,
    reader: ChainReader | None = None,
    engine: MasEngine | None = None,
) -> dict[str, Any]:
    if not 1 <= block_batch_size <= 1000 or min(poll_seconds, max_block_age_seconds, live_max_delay_seconds) <= 0:
        raise ValueError("Monitor limits must be positive; block batch must be <= 1000")
    watchlist_path, destination = watchlist_path.resolve(), destination.resolve()
    if watchlist_path == destination:
        raise ValueError("Monitor state must not overwrite the watchlist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    owned_reader = reader is None
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smart-money-research")
    future: Future[dict[str, Any]] | None = None
    running: str | None = None
    node_check = None
    reported_status = None
    try:
        with destination.with_suffix(destination.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state: dict[str, Any] = (
                json.loads(destination.read_text())
                if destination.exists()
                else {
                    "schema_version": 1,
                    "watchlist": str(watchlist_path),
                    "wallets": {},
                    "qualifications": {},
                    "markets": {},
                    "token_markets": {},
                    "transactions": {},
                    "observations": {},
                    "cursor": None,
                    "health": {},
                }
            )
            if state.get("schema_version") != 1 or state.get("watchlist") != str(watchlist_path):
                raise ValueError("Monitor state identity mismatch")
            for observation in state["observations"].values():
                if observation["research"]["status"] == "RUNNING":
                    interrupted = observation["research"]
                    interrupted.update(status="FAILED", error="INTERRUPTED_BEFORE_RESULT_COMMIT")
                    if observation.get("request", {}).get("context", {}).get("research_limits"):
                        interrupted.update(status="REVIEW_REQUIRED", error="UNKNOWN_BUDGET_AFTER_INTERRUPTION")
                    interrupted.setdefault("attempt_history", []).append(
                        {
                            "attempt": interrupted["attempts"],
                            "status": interrupted["status"],
                            "error": interrupted["error"],
                        }
                    )
            while True:
                if running is not None and future is not None and future.done():
                    job = state["observations"][running]["research"]
                    _finish_research(job, future, offline_mas)
                    write_json(destination, state)
                    running = future = None
                try:
                    updated = deepcopy(state)
                    watchlist_error = None
                    try:
                        enabled = _refresh_qualifications(updated, read_watchlist(watchlist_path), _now())
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        if not state["wallets"]:
                            raise
                        updated = deepcopy(state)
                        enabled = {
                            wallet
                            for wallet, account in state["wallets"].items()
                            if account.get("desired_monitoring", True)
                        }
                        watchlist_error = str(exc)
                    if enabled or updated["wallets"]:
                        reader = reader or ChainReader()
                        if node_check is None:
                            node_check = reader.preflight(max_block_age_seconds)
                        updated["node_check"] = node_check
                        updated = scan_once(
                            updated,
                            reader,
                            enabled,
                            block_batch_size=block_batch_size,
                            max_block_age_seconds=max_block_age_seconds,
                            live_max_delay_seconds=live_max_delay_seconds,
                        )
                        pending_baselines = sum(
                            size is None
                            for account in updated["wallets"].values()
                            for size in account["positions"].values()
                        )
                        pending_seeds = sum(bool(a.get("seed_pending")) for a in updated["wallets"].values())
                        pending_observations = sum("pending_context" in o for o in updated["observations"].values())
                        updated["health"].update(
                            pending_baselines=pending_baselines,
                            pending_token_lists=pending_seeds,
                            pending_markets=len(updated.get("pending_markets", {})),
                            pending_observations=pending_observations,
                        )
                        if pending_baselines or pending_seeds or pending_observations or updated.get("pending_markets"):
                            updated["health"]["status"] = "RECOVERY_PENDING"
                    else:
                        updated["health"] = {"status": "NO_MONITORED_WALLETS"}
                    updated["health"].update(at=_now(), active_wallets=len(enabled))
                    latest = {
                        (w, v["record"]["sector_id"]): v["record"]
                        for w, versions in updated["qualifications"].items()
                        for v in versions
                    }
                    qualified = {
                        w
                        for (w, _), r in latest.items()
                        if r["historical_eligible"]
                        and r["status"] == "QUALIFIED"
                        and not r["stale"]
                        and r["monitor"]["enabled"]
                        and (not r.get("valid_until") or instant(_now()) < instant(r["valid_until"]))
                    }
                    exploring = {
                        w
                        for (w, _), r in latest.items()
                        if r["monitor"]["enabled"] and r["monitor"]["mode"] == "EXPLORE"
                    }
                    updated["health"].update(
                        qualified_wallets=len(qualified),
                        exploring_wallets=len(exploring),
                        update_only_wallets=len(enabled - qualified - exploring),
                    )
                    if not qualified and exploring and updated["health"]["status"] == "CAUGHT_UP":
                        updated["health"]["status"] = "EXPLORING"
                    if watchlist_error:
                        updated["health"]["watchlist_error"] = watchlist_error
                    write_json(destination, updated)
                    state = updated
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    state["health"] = {"status": "BLOCKED", "error": type(exc).__name__ + ":" + str(exc), "at": _now()}
                    write_json(destination, state)
                if watch and state["health"]["status"] != reported_status:
                    print(json.dumps({"monitor": state["health"]}, ensure_ascii=False), flush=True)
                    reported_status = state["health"]["status"]
                if future is None and state["health"].get("status") != "BLOCKED":
                    for identifier, observation in state["observations"].items():
                        job = observation["research"]
                        if job["status"] not in {"PENDING", "FAILED"}:
                            continue
                        if job["attempts"] >= 3:
                            job["status"] = "REVIEW_REQUIRED"
                            continue
                        running = identifier
                        job.update(status="RUNNING", attempts=job["attempts"] + 1, started_at=_now())
                        write_json(destination, state)
                        request = deepcopy(observation["request"])
                        previous_budget = (job.get("result") or {}).get("agent_runtime", {}).get("sharedBudget")
                        if previous_budget:
                            request["context"]["research_budget_state"] = previous_budget
                        request["candidate"]["evidence"] = deepcopy(
                            observation.get("supplement", observation)["evidence"]
                        )
                        request["context"]["market"] = deepcopy(
                            state["markets"][request["candidate"]["market_id"]]["payload"]
                        )
                        future = pool.submit(_research, engine, request, offline_mas)
                        break
                if not watch:
                    if future is not None and running is not None:
                        job = state["observations"][running]["research"]
                        _finish_research(job, future, offline_mas)
                    write_json(destination, state)
                    return {
                        "health": state["health"],
                        "cursor": state["cursor"],
                        "wallets": len(state["wallets"]),
                        "observations": len(state["observations"]),
                        "research": {
                            status: sum(o["research"]["status"] == status for o in state["observations"].values())
                            for status in (
                                "PENDING",
                                "RUNNING",
                                "DONE",
                                "OFFLINE_DONE",
                                "FAILED",
                                "REVIEW_REQUIRED",
                                "NOT_SUBMITTED",
                            )
                        },
                    }
                time.sleep(poll_seconds)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if owned_reader and reader is not None:
            reader.close()
