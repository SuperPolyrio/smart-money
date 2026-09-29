"""Discovery, incremental review and durable maintenance of the unique watchlist."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smart_money.infrastructure.local_history import read_resolutions
from smart_money.infrastructure.polymarket import PolymarketClient
from smart_money.infrastructure.wallet_storage import (
    activity_summary,
    archive_for,
    atomic_bytes,
    compact_profile,
    history_header,
    load_evaluation,
    load_history,
    new_state,
    read_watchlist,
    repair_targets,
    store_evaluation,
    store_history,
    write_watchlist,
)
from smart_money.wallets.directional_expert_policy import DirectionalExpertPolicy, WalletRefreshSettings
from smart_money.wallets.discovery import LEADERBOARD_SECTORS, discover_candidates, wallet_address
from smart_money.wallets.history import evaluate_history, incomplete_feeds, instant

SETTINGS = WalletRefreshSettings()


def _content_ref(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _maintenance_scope(state: dict[str, Any], as_of: str) -> tuple[bool, set[str] | None]:
    """Persist triggers before processing; failed wallets stay pending across restarts."""
    maintenance = state.setdefault("maintenance", {"pending": {}, "settlements": {}})
    conditions: dict[str, set[str]] = {}
    for row in state["records"].values():
        if row.get("next_review_at") and instant(row["next_review_at"]) <= instant(as_of) and not row.get("archived"):
            maintenance["pending"].setdefault(
                row["candidate_id"], {"reason": "WINDOW_BOUNDARY", "requested_at": as_of, "settlement_refs": {}}
            )
        evaluation = load_evaluation(state, row.get("evaluation_ref"))
        if not evaluation.get("history_ref"):
            evaluation = load_evaluation(state, row.get("last_successful_evaluation_ref"))
        manifest = (
            history_header(archive_for(state), evaluation["history_ref"]) if evaluation.get("history_ref") else {}
        )
        conditions_list = archive_for(state).get(manifest["conditions_ref"]) if manifest else []
        for condition in conditions_list:
            conditions.setdefault(condition, set()).add(row["candidate_id"])
    try:
        resolutions: dict[str, Any] = {}
        keys = sorted(conditions)
        after = maintenance.get("settlement_cursor", "")
        selected = [c for c in keys if c > after][:200]
        if not selected and keys:
            selected = keys[:200]
        resolutions.update(read_resolutions(selected, as_of))
        references = {}
        for condition in selected:
            value = resolutions.get(condition)
            previous = maintenance["settlements"].get(condition)
            # The migration preserves old fingerprints; encoding alone is not a new settlement.
            references[condition] = previous if previous == _content_ref(value) else archive_for(state).put(value)
        for condition, reference in references.items():
            if maintenance["settlements"].get(condition) == reference:
                continue
            for candidate_id in conditions[condition]:
                trigger = maintenance["pending"].setdefault(
                    candidate_id, {"reason": "SETTLEMENT_CHANGED", "requested_at": as_of, "settlement_refs": {}}
                )
                trigger["settlement_refs"][condition] = reference
        maintenance["settlements"].update(references)
        more = bool(selected and selected[-1] != keys[-1])
        maintenance["settlement_cursor"] = selected[-1] if more else ""
        maintenance["settlement_check"] = {"status": "PARTIAL" if more else "OK", "at": as_of}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        maintenance["settlement_check"] = {"status": "FAILED", "at": as_of, "error": str(exc)}
    today = instant(as_of).date().isoformat()
    daily = maintenance.get("daily_date") != today
    maintenance["daily_date"] = today
    return daily or bool(maintenance.get("discovery_pending")), None if daily else set(maintenance["pending"])


def refresh_watchlist(
    destination: Path,
    *,
    inputs: dict[str, Any] | None = None,
    offline: bool = False,
    repair: bool = False,
    repair_wallet: str | None = None,
    as_of: str | None = None,
    categories: list[str] | None = None,
    due: bool = False,
) -> dict[str, Any]:
    """Refresh known candidates or one repair wallet, keeping failures and old evaluations.

    Offline inputs contain captured boards/profiles/histories at one cutoff;
    they use the same native rows and evaluator as live acquisition.
    """
    if due and (offline or inputs or as_of or repair or repair_wallet):
        raise ValueError("Scheduled refresh requires live current facts; omit offline, input, as-of and repair")
    if repair and (offline or inputs or as_of):
        raise ValueError("Repair uses the saved wallet identities and cutoff; omit input, offline and as_of")
    if repair_wallet is not None:
        if not repair or not wallet_address(repair_wallet):
            raise ValueError("A wallet scope requires repair and a valid trading wallet address")
        repair_wallet = wallet_address(repair_wallet)
    inputs = inputs or {}
    as_of = instant(as_of or inputs.get("as_of") or datetime.now(timezone.utc).isoformat()).isoformat()
    if offline and (not inputs.get("as_of") or instant(inputs["as_of"]) != instant(as_of)):
        raise ValueError("Offline input requires its matching as_of cutoff")
    if not offline and not repair and abs((instant(as_of) - datetime.now(timezone.utc)).total_seconds()) > 60:
        raise ValueError("Live current-position reads cannot reconstruct a past cutoff; use a captured offline input")
    categories = list(dict.fromkeys(categories if categories is not None else LEADERBOARD_SECTORS))
    if not categories or any(category not in LEADERBOARD_SECTORS for category in categories):
        raise ValueError("Select at least one supported leaderboard category")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    client = PolymarketClient()
    # Evidence is durable before the single writer publishes the small current view.
    try:
        with destination.with_suffix(destination.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def file_revision() -> tuple[int, int, int] | None:
                if not destination.exists():
                    return None
                st = destination.stat()
                return st.st_ino, st.st_size, st.st_mtime_ns

            revision = file_revision()

            def persist(current: dict[str, Any]) -> None:
                nonlocal revision
                actual = file_revision()
                if actual != revision:
                    raise ValueError("WATCHLIST_CHANGED_DURING_REFRESH")
                write_watchlist(destination, current)
                revision = file_revision()

            state = read_watchlist(destination) if destination.exists() else new_state(destination)
            if repair:
                if not state.get("updated_at"):
                    raise ValueError("Repair requires an existing watchlist")
                as_of = str(state["updated_at"])
            content = {"qualification": asdict(DirectionalExpertPolicy()), "refresh": asdict(SETTINGS)}
            prior_policy = state.get("policy", {}).get("reference")
            policy_ref = prior_policy if prior_policy == _content_ref(content) else archive_for(state).put(content)
            if state.get("policy", {}).get("reference") != policy_ref:
                state["policy"] = {
                    "reference": policy_ref,
                    "effective_at": datetime.now(timezone.utc).isoformat(),
                    "content": content,
                }
                for row in state["records"].values():
                    if row.get("policy_version") != content["qualification"]["version"]:
                        row["next_review_at"] = as_of
            state.setdefault("tasks", {})
            client.host_backoff = state.setdefault("host_backoff", {})
            if state.get("updated_at") and instant(state["updated_at"]) > instant(as_of):
                raise ValueError("Cannot replace a newer watchlist with an older cutoff")
            if repair_wallet and not any(row["wallet"] == repair_wallet for row in state["candidates"]):
                raise ValueError("Repair wallet is not in the saved watchlist")
            discover, candidate_ids = not repair, {repair_wallet} if repair_wallet else None
            if due:
                discover, candidate_ids = _maintenance_scope(state, as_of)
            _allocate_monitoring(state, as_of)
            state = _refresh(
                state,
                inputs,
                offline,
                as_of,
                categories,
                client,
                repair=repair,
                discover=discover,
                candidate_ids=candidate_ids,
                due=due,
                checkpoint=persist,
            )
            persist(state)
            return state
    finally:
        client.close()


def _refresh(
    state: dict[str, Any],
    inputs: dict[str, Any],
    offline: bool,
    as_of: str,
    categories: list[str],
    client: PolymarketClient,
    *,
    repair: bool = False,
    discover: bool = True,
    candidate_ids: set[str] | None = None,
    due: bool = False,
    checkpoint: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    boards = (
        {(row["category"], row["period"]): deepcopy(row) for row in state.get("discovery", [])} if repair or due else {}
    )
    for category in categories if discover else []:
        for period in ("week", "month", "all"):
            key = f"{category}:{period}"
            last = state["leaderboard_success"].get(key)
            if period == "all" and last and instant(as_of) - instant(last) < timedelta(days=7):
                continue
            if due and period != "all" and last and instant(as_of).date() == instant(last).date():
                continue
            if offline:
                board = next(
                    (
                        row
                        for row in inputs.get("boards", [])
                        if row["category"] == category and row["period"] == period
                    ),
                    None,
                )
                board = (
                    deepcopy(board)
                    if board is not None
                    else {
                        "category": category,
                        "period": period,
                        "rows": [],
                        "coverage": {"complete": False, "error": "MISSING_CAPTURED_BOARD"},
                    }
                )
            else:
                board = client.leaderboard(category, period)
            board["as_of"] = as_of
            boards[(category, period)] = board
            if board.get("coverage", {}).get("complete") is True:
                state["leaderboard_success"][key] = as_of

    def resolve(account: str) -> dict[str, Any]:
        mapping = inputs.get("profiles", {}).get(account)
        if mapping is not None:
            return dict(mapping)
        if offline:
            return {"error": "MISSING_CAPTURED_MAPPING"}
        return {"error": "MAPPING_QUEUED"}  # Mapping is a wallet task, never a discovery-wide prerequisite.

    if discover:
        for board in boards.values():
            board.setdefault("as_of", state["leaderboard_success"].get(f"{board['category']}:{board['period']}", as_of))
        known = {row["candidate_id"] for row in state["candidates"]}
        state["candidates"] = discover_candidates(
            state["candidates"], list(boards.values()), inputs.get("candidates", []), as_of=as_of, resolve=resolve
        )
        if candidate_ids is not None:
            candidate_ids.update(row["candidate_id"] for row in state["candidates"] if row["candidate_id"] not in known)
    # A newly resolved alias becomes the same record, preserving its audit trail.
    state["discovery"] = list(boards.values())
    candidates = [row for row in state["candidates"] if candidate_ids is None or row["candidate_id"] in candidate_ids]
    if candidates or discover:
        state["updated_at"] = as_of
    maintenance = state.get("maintenance")
    if due:
        assert maintenance is not None
        maintenance["discovery_pending"] = any(
            not b.get("coverage", {}).get("complete") for b in boards.values() if b["category"] in categories
        )
        for candidate in candidates:
            trigger = maintenance["pending"].setdefault(
                candidate["candidate_id"], {"reason": "DAILY", "requested_at": as_of, "settlement_refs": {}}
            )
            if candidate["wallet"]:
                for alias in candidate["aliases"]:
                    retired = maintenance["pending"].pop(f"unmapped:{alias}", None)
                    if retired:
                        trigger["settlement_refs"].update(retired["settlement_refs"])
        _summarize(state)
        checkpoint(state)
    for candidate in candidates:
        _merge_candidate_records(state, candidate)
    if not offline and not repair:
        candidates = _scheduled_candidates(state, candidates, as_of)
    if not offline and not due:
        _summarize(state)
        checkpoint(state)
    processed = set()
    # Old records also remain candidates, including manually paused addresses.
    for candidate in candidates:
        if candidate["candidate_id"] in processed:
            continue
        if not offline and candidate["mapping_status"] != "CONFIRMED":
            account = candidate["aliases"][0]
            try:
                mapping = client.resolve(account)
            except (OSError, ValueError) as exc:
                mapping = {"error": str(exc)}
            state["candidates"] = discover_candidates(
                state["candidates"],
                [],
                [
                    {
                        "account": account,
                        "source": "existing",
                        "sector_id": sector,
                        "trading_wallet": mapping.get("wallet"),
                        "mapping_evidence": mapping.get("evidence"),
                    }
                    for sector in candidate["sectors"]
                ],
                as_of=as_of,
                resolve=lambda requested: mapping if requested == account else {"error": "MAPPING_QUEUED"},
            )
            prior_id = candidate["candidate_id"]
            candidate = next(c for c in state["candidates"] if account in c["aliases"])
            if candidate["candidate_id"] != prior_id:
                _merge_candidate_records(state, candidate)
                state["tasks"].pop(prior_id, None)
                if maintenance:
                    trigger = maintenance["pending"].pop(prior_id, None)
                    if trigger:
                        maintenance["pending"].setdefault(candidate["candidate_id"], trigger)
            _summarize(state)
            checkpoint(state)
        if candidate["candidate_id"] in processed:
            continue
        processed.add(candidate["candidate_id"])
        previous = {
            key: row for key, row in state["records"].items() if row["candidate_id"] == candidate["candidate_id"]
        }
        sectors = sorted(set(candidate["sectors"]) | {row["sector_id"] for row in previous.values()})
        history = None
        cutoff = as_of
        reviewed = None
        failure = None
        profiles = {}
        reconciliation = None
        task = state["tasks"].setdefault(candidate["candidate_id"], {"queued_at": candidate["first_discovered_at"]})
        task["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        try:
            if candidate["mapping_status"] != "CONFIRMED":
                raise ValueError("TRADING_WALLET_MAPPING_UNRESOLVED")
            wallet = candidate["wallet"]
            history = inputs.get("histories", {}).get(wallet)
            if history is None:
                if offline:
                    raise ValueError("MISSING_CAPTURED_HISTORY")
                reference = next((row.get("evaluation_ref") for row in previous.values()), None)
                saved_evaluation = load_evaluation(state, reference)
                if not saved_evaluation.get("history_ref"):
                    reference = next((row.get("last_successful_evaluation_ref") for row in previous.values()), None)
                    saved_evaluation = load_evaluation(state, reference)
                captured = load_history(state, task.get("history_ref") or saved_evaluation.get("history_ref"))
                summary = activity_summary(state, candidate)
                if not repair and (not summary or instant(summary["as_of"]).date() < instant(as_of).date()):
                    try:
                        summary = client.summary(wallet, datetime.now(timezone.utc).isoformat(), previous=captured)
                        candidate["summary_ref"] = archive_for(state).put(summary)
                        task.pop("summary_error", None)
                    except (OSError, ValueError) as exc:
                        task["summary_error"] = str(exc)
                    for sector in sectors:
                        state["records"].setdefault(
                            f"{candidate['candidate_id']}:{sector}", _new_record(candidate, sector)
                        )
                    _allocate_monitoring(state, datetime.now(timezone.utc).isoformat())
                    _summarize(state)
                    checkpoint(state)
                if repair:
                    if captured is None:
                        raise ValueError("MISSING_CAPTURED_HISTORY_FOR_REPAIR")
                    # Re-evaluate saved evidence under current rules before deciding what is missing.
                    history = captured
                    cutoff = captured["as_of"]
                    trigger = (maintenance or {}).get("pending", {}).get(candidate["candidate_id"])
                    if trigger and instant(cutoff) < instant(trigger["requested_at"]):
                        raise ValueError("NEWER_REFRESH_PENDING")
                    reviewed = evaluate_history(captured, sectors, as_of=cutoff)
                    if not reviewed["reconciliation"]["complete"]:
                        history = client.history(
                            wallet,
                            cutoff,
                            previous=captured,
                            repair_targets=reviewed["reconciliation"]["repair_targets"],
                        )
                        reviewed = None
                else:
                    # Discovery and preceding wallets may take minutes; current positions need a fresh cutoff.
                    pending_work = (captured or {}).get("work", {})
                    cutoff = (
                        captured["as_of"]
                        if captured and pending_work.get("phase") not in (None, "DONE")
                        else datetime.now(timezone.utc).isoformat()
                    )
                    history = client.history(
                        wallet,
                        cutoff,
                        previous=captured,
                        repair_targets=repair_targets(state, reference),
                    )
            if history.get("wallet") != wallet:
                raise ValueError("HISTORY_WALLET_MISMATCH")
            reviewed = reviewed or evaluate_history(history, sectors, as_of=cutoff)
            profiles, reconciliation = reviewed["profiles"], reviewed["reconciliation"]
            incomplete = incomplete_feeds(history)
            if history.get("work"):
                work = history["work"]
                task.update(
                    {key: deepcopy(value) for key, value in work.items() if key not in {"segments", "batches", "index"}}
                )
                task["window"] = history.get("selection", {"end": cutoff})
                task["cursor"] = {
                    "index": work.get("index_cursor"),
                    "batch": work.get("batch"),
                    "step": work.get("step"),
                    "feeds": work.get("segments", {}).get(str(work.get("batch")), {}),
                }
                task["completed_sectors"] = [s for s, p in profiles.items() if p["reviews"]["data"]["status"] == "PASS"]
                # A different incomplete sector must not invalidate a completed profile.
            elif incomplete:
                failure = "HISTORY_FETCH_INCOMPLETE:" + ",".join(incomplete)
            elif history.get("fetch_errors"):
                failure = "HISTORY_METADATA_FETCH_FAILED:" + ",".join(history["fetch_errors"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failure = str(exc)
        evaluation = {
            "candidate_id": candidate["candidate_id"],
            "as_of": cutoff,
            "history": history,
            "profiles": profiles,
            "reconciliation": reconciliation,
            "failure": failure,
            "policy_ref": state["policy"]["reference"],
        }
        if maintenance and candidate["candidate_id"] in maintenance["pending"]:
            evaluation["trigger"] = deepcopy(maintenance["pending"][candidate["candidate_id"]])
        ref = _content_ref(evaluation)  # IDs depend on logical content, not its storage representation.
        evaluation["history_ref"] = store_history(
            archive_for(state), evaluation.pop("history"), previous=task.get("history_ref")
        )
        if evaluation["history_ref"]:
            task["history_ref"] = evaluation["history_ref"]
        attempted_at = datetime.now(timezone.utc).isoformat()
        store_evaluation(state, ref, {**evaluation, "evaluated_at": attempted_at})
        for sector in sorted(set(sectors) | set(profiles)):
            key = f"{candidate['candidate_id']}:{sector}"
            old = state["records"].get(key, {})
            evaluated_at = old["last_attempt_at"] if old.get("evaluation_ref") == ref else attempted_at
            row = deepcopy(old) if old else _new_record(candidate, sector)
            profile = profiles.get(sector)
            if failure:
                row.pop("reason_summary", None)
                row.update(status="PENDING_REVIEW", historical_eligible=False, stale=True, reasons=[failure])
                if not old and profile:
                    row.update(
                        metrics=profile["metrics"],
                        reviews={k: {"status": v["status"]} for k, v in profile["reviews"].items()},
                    )
            elif profile is not None:
                row.update(compact_profile(profile, ref))
                data_complete = profile["reviews"]["data"]["status"] == "PASS"
                source_failed = bool(history and history.get("work", {}).get("outcome") == "SOURCE_ERROR")
                acquisition_complete = not history or history.get("work", {}).get("phase", "DONE") == "DONE"
                row.update(
                    stale=source_failed and not data_complete,
                    data_cutoff=cutoff,
                    data_status="COMPLETE" if data_complete else "PARTIAL",
                )
                if not data_complete and (source_failed or not acquisition_complete) and old.get("metrics"):
                    row["metrics"] = old["metrics"]
                    row.update(
                        data_cutoff=old.get("data_cutoff"),
                        pending_cutoff=cutoff,
                        metrics_ref=old.get("metrics_ref", old.get("evaluation_ref")),
                    )
                else:
                    row["metrics_ref"] = ref
                if not source_failed and (acquisition_complete or data_complete):
                    row.update(last_successful_check_at=evaluated_at, last_successful_evaluation_ref=ref)
                row["valid_until"] = min(
                    [
                        instant(cutoff) + timedelta(hours=SETTINGS.validity_hours),
                        *(
                            instant(e["settled_at"]) + timedelta(days=days)
                            for e in profile["events"]
                            if e.get("settled_at")
                            for days in (30, 90, DirectionalExpertPolicy().history_window_days)
                            if instant(e["settled_at"]) + timedelta(days=days) > instant(cutoff)
                        ),
                    ]
                ).isoformat()
                row["next_review_at"] = min(
                    instant(cutoff).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
                    instant(row["valid_until"]),
                ).isoformat()
            else:
                row.update(
                    status="PENDING_REVIEW", historical_eligible=False, stale=True, reasons=["SECTOR_NOT_EVALUATED"]
                )
            row.pop("events", None)  # Detailed events remain in the immutable evaluation referenced below.
            if row["historical_eligible"] and not row["first_qualified_at"]:
                row["first_qualified_at"] = evaluated_at
            row.update(
                evaluation_ref=ref,
                last_attempt_at=evaluated_at,
                sources=candidate["sources"],
            )
            change = {name: row.get(name) for name in ("status", "historical_eligible", "stale", "monitor")}
            prior = {name: old.get(name) for name in change}
            if change != prior:
                row["eligibility_effective_at"] = evaluated_at
                row["changes"].append(
                    {
                        "at": evaluated_at,
                        "before": prior,
                        "after": change,
                        "previous_evaluation_ref": old.get("evaluation_ref"),
                        "evaluation_ref": ref,
                    }
                )
            state["records"][key] = row
        complete = not failure and (not history or history.get("work", {}).get("phase", "DONE") == "DONE")
        if complete:
            next_review = min(
                (
                    r["next_review_at"]
                    for r in state["records"].values()
                    if r["candidate_id"] == candidate["candidate_id"] and r.get("next_review_at")
                ),
                default=(instant(cutoff) + timedelta(days=1)).isoformat(),
            )
            task.update(outcome="COMPLETE", next_attempt_at=next_review)
            task.pop("error", None)
            task.pop("failures", None)
        elif failure:
            task["failures"] = task.get("failures", 0) + 1
            retry_at = datetime.now(timezone.utc) + (
                timedelta(minutes=5) if task["failures"] <= SETTINGS.extra_retries else timedelta(days=1)
            )
            task.update(
                outcome="SOURCE_ERROR",
                error=failure,
                next_attempt_at=datetime.fromtimestamp(
                    max([retry_at.timestamp(), *client.host_backoff.values()]), timezone.utc
                ).isoformat(),
            )
        if maintenance and complete and not repair:
            maintenance["pending"].pop(candidate["candidate_id"], None)
        _allocate_monitoring(state, datetime.now(timezone.utc).isoformat() if not offline else as_of)
        if candidate is not candidates[-1]:
            _summarize(state)
            checkpoint(state)  # Commit all sectors and their evidence before advancing to another wallet.
    _summarize(state)
    return state


def _merge_candidate_records(state: dict[str, Any], candidate: dict[str, Any]) -> None:
    for alias in candidate["aliases"]:
        for key, old in list(state["records"].items()):
            if old["candidate_id"] == f"unmapped:{alias}" and candidate["wallet"]:
                target = f"{candidate['candidate_id']}:{old['sector_id']}"
                if target not in state["records"]:
                    state["records"][target] = old
                    old.update(candidate_id=candidate["candidate_id"], wallet=candidate["wallet"])
                else:
                    current = state["records"][target]
                    current["changes"] = old["changes"] + current["changes"]
                    current["first_discovered_at"] = min(old["first_discovered_at"], current["first_discovered_at"])
                    current["manual_paused"] = old["manual_paused"] or current["manual_paused"]
                del state["records"][key]


def _new_record(candidate: dict[str, Any], sector: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "wallet": candidate["wallet"],
        "sector_id": sector,
        "first_discovered_at": candidate["first_discovered_at"],
        "forward_status": "SHADOW_OBSERVE",
        "manual_paused": False,
        "changes": [],
        "first_qualified_at": None,
        "historical_eligible": False,
        "status": "PENDING_REVIEW",
        "stale": True,
        "reasons": ["HISTORY_NOT_EVALUATED"],
        "monitor": {"enabled": False, "mode": "DISABLED", "wallet": candidate["wallet"]},
    }


def _scheduled_candidates(state: dict[str, Any], candidates: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Old due reviews and new wallets get separate capacity; rotate sectors and task age."""
    selected = []
    known = {r["candidate_id"] for r in state["records"].values() if r.get("evaluation_ref")}
    for existing, capacity in ((True, SETTINGS.existing_wallets_per_round), (False, SETTINGS.new_wallets_per_round)):
        queues: dict[str, list[dict[str, Any]]] = {}
        for c in candidates:
            if (c["candidate_id"] in known) != existing:
                continue
            task = state["tasks"].get(c["candidate_id"], {})
            owned = [r for r in state["records"].values() if r["candidate_id"] == c["candidate_id"]]
            latest_source = max((s["last_seen_at"] for s in c["sources"]), default=c["first_discovered_at"])
            if (
                owned
                and all(r.get("archived") for r in owned)
                and instant(latest_source) + timedelta(days=SETTINGS.archive_days) <= instant(as_of)
            ):
                continue
            if task.get("next_attempt_at") and instant(task["next_attempt_at"]) > instant(as_of):
                trigger = state.get("maintenance", {}).get("pending", {}).get(c["candidate_id"], {})
                if trigger.get("reason") != "SETTLEMENT_CHANGED" or trigger.get("requested_at", "") <= task.get(
                    "last_attempt_at", ""
                ):
                    continue
            queues.setdefault(min(c["sectors"]), []).append(c)
        for queue in queues.values():
            queue.sort(
                key=lambda c: (
                    state["tasks"].get(c["candidate_id"], {}).get("last_attempt_at", ""),
                    c["first_discovered_at"],
                    c["candidate_id"],
                )
            )
        count = 0
        while any(queues.values()) and count < capacity:
            for sector in sorted(queues):
                if queues[sector] and count < capacity:
                    selected.append(queues[sector].pop(0))
                    count += 1
    return selected


def _allocate_monitoring(state: dict[str, Any], as_of: str) -> None:
    now = instant(as_of)
    candidates = {c["candidate_id"]: c for c in state["candidates"]}
    rows = list(state["records"].values())
    normal: set[str] = set()
    exploring: set[str] = set()
    for row in sorted(
        rows,
        key=lambda r: (
            r.get("monitor", {}).get("mode") != "NORMAL",
            r["forward_status"] != "FORWARD_VALIDATED",
            -r.get("metrics", {}).get("recent_events", 0),
        ),
    ):
        candidate = candidates.get(row["candidate_id"], {})
        version = state.get("policy", {}).get("content", {}).get("qualification", {}).get("version")
        if version and row.get("policy_version") and row["policy_version"] != version:
            row.update(stale=True, data_status="STALE")
            policy_reason = "POLICY_CHANGED_REVIEW_REQUIRED"
            summary = row.get("reason_summary", {})
            if policy_reason not in summary.get("counts", {}) and policy_reason not in row["reasons"]:
                row["reasons"].append(policy_reason)
                if summary:
                    summary["counts"][policy_reason] = 1
                    summary["total"] += 1
        if row.get("valid_until") and instant(row["valid_until"]) <= now:
            row["stale"] = True
            row["data_status"] = "STALE"
        valid = (
            row["historical_eligible"]
            and not row["stale"]
            and not row["manual_paused"]
            and candidate.get("mapping_status") == "CONFIRMED"
        )
        if valid and (row["wallet"] in normal or len(normal) < SETTINGS.normal_capacity):
            normal.add(row["wallet"])
    for row in rows:
        c = candidates.get(row["candidate_id"], {})
        summary = activity_summary(state, c)
        trade = summary.get("recent_trade")
        mode = "DISABLED"
        if not row["manual_paused"] and c.get("mapping_status") == "CONFIRMED":
            if row["wallet"] in normal and row["historical_eligible"] and not row["stale"]:
                mode = "NORMAL"
            elif trade and now - timedelta(days=30) <= instant(trade["timestamp"]) <= now:
                reason = any(
                    s["source"].startswith("official:")
                    and s["source"].rsplit(":", 1)[-1] in {"week", "month"}
                    and float(s.get("pnl") or 0) > 0
                    and instant(s["last_seen_at"]) >= now - timedelta(days=30)
                    for s in c["sources"]
                )
                exploration = c.get("exploration", {})
                renewable = (
                    not exploration
                    or instant(exploration["until"]) > now
                    or instant(trade["timestamp"]) > instant(exploration["activity_at"])
                )
                if reason and renewable and (row["wallet"] in exploring or len(exploring) < SETTINGS.explore_capacity):
                    mode = "EXPLORE"
                    exploring.add(row["wallet"])
                    if not exploration or instant(exploration["until"]) <= now:
                        c["exploration"] = {
                            "started_at": as_of,
                            "until": (now + timedelta(days=SETTINGS.explore_days)).isoformat(),
                            "activity_at": instant(trade["timestamp"]).isoformat(),
                        }
            if mode == "DISABLED" and row.get("follow_up_refs"):
                mode = "UPDATE_ONLY"
        monitor = {"enabled": mode != "DISABLED", "mode": mode, "wallet": row["wallet"]}
        if monitor != row["monitor"]:
            if row["changes"] and row["changes"][-1]["at"] == as_of and "evaluation_ref" in row["changes"][-1]:
                row["changes"][-1]["after"]["monitor"] = monitor
            else:
                row["changes"].append(
                    {
                        "at": as_of,
                        "before": {"monitor": row["monitor"]},
                        "after": {"monitor": monitor},
                        "reason": "MONITOR_ALLOCATION",
                    }
                )
            row["monitor"] = monitor
            row["monitor_effective_at"] = as_of
        last_seen = max((s["last_seen_at"] for s in c.get("sources", [])), default=c.get("first_discovered_at", as_of))
        row["archived"] = (
            mode == "DISABLED"
            and not row.get("follow_up_refs")
            and instant(last_seen) + timedelta(days=SETTINGS.archive_days) <= now
        )


def _summarize(state: dict[str, Any]) -> None:
    state["summary"] = {
        "candidates": len(state["candidates"]),
        "unresolved_mappings": sum(row["mapping_status"] != "CONFIRMED" for row in state["candidates"]),
        "wallet_sector_records": len(state["records"]),
        "qualified": sum(bool(row["historical_eligible"] and not row["stale"]) for row in state["records"].values()),
        "rejected": sum(row["status"] == "REJECTED" for row in state["records"].values()),
        "pending_review": sum(row["status"] == "PENDING_REVIEW" for row in state["records"].values()),
        "stale": sum(bool(row["stale"]) for row in state["records"].values()),
        "discovery_failures": sum(
            board.get("coverage", {}).get("complete") is not True for board in state["discovery"]
        ),
        "listening_wallets": sorted({row["wallet"] for row in state["records"].values() if row["monitor"]["enabled"]}),
        "exploring": len({r["wallet"] for r in state["records"].values() if r["monitor"]["mode"] == "EXPLORE"}),
        "summary_ready": sum(activity_summary(state, c).get("status") == "READY" for c in state["candidates"]),
        "task_outcomes": {
            name: sum(t.get("outcome") == name for t in state.get("tasks", {}).values())
            for name in ("COMPLETE", "YIELDED", "SOURCE_ERROR")
        },
    }


def write_json(destination: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(destination, (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
