"""Address-scoped official reads, with explicit cursor and coverage evidence."""

from __future__ import annotations

import json
import math
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlparse

import requests
from urllib3.util import Timeout

from smart_money.infrastructure.local_history import (
    read_cutoff_boundary,
    read_local_history,
    read_local_operations,
    read_local_receipts,
    read_wallet_event_index,
)
from smart_money.markets.trades import decimal_value
from smart_money.wallets.directional_expert_policy import DirectionalExpertPolicy, WalletRefreshSettings
from smart_money.wallets.discovery import wallet_address
from smart_money.wallets.history import cutoff_block, instant

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


class PolymarketClient:
    def __init__(self, *, timeout: float = 20, max_pages: int = 1000) -> None:
        if timeout <= 0 or max_pages <= 0:
            raise ValueError("timeout and max_pages must be positive")
        self.timeout = timeout
        self.max_pages = max_pages
        self.session = requests.Session()
        self.settings = WalletRefreshSettings()
        self.host_backoff: dict[str, float] = {}

    def get(self, url: str, params: dict[str, Any]) -> Any:
        host = urlparse(url).netloc
        if self.host_backoff.get(host, 0) > time.time():
            raise OSError("HOST_BACKOFF")
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=cast(Any, Timeout(total=self.timeout, connect=self.settings.connect_seconds)),
            )
            if response.status_code in (429, 503):
                value = response.headers.get("Retry-After", "30")
                try:
                    until = time.time() + max(0, float(value))
                except ValueError:
                    until = parsedate_to_datetime(value).timestamp()
                self.host_backoff[host] = max(time.time() + 1, until)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OSError(f"{url}: {type(exc).__name__}") from exc

    def page(
        self,
        route: str,
        params: dict[str, Any],
        *,
        budget: int | None = None,
        previous: dict[str, Any] | None = None,
        pages: int | None = None,
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        cursors: set[str] = set()
        coverage: dict[str, Any] = {"complete": False, "pages": 0, "endpoint": route, "params": params}
        query = dict(params)
        if (
            previous
            and previous.get("coverage", {}).get("params") == params
            and previous["coverage"].get("endpoint") == route
        ):
            if previous["coverage"].get("complete"):
                return deepcopy(previous)
            if previous["coverage"].get("next_cursor"):
                rows = deepcopy(previous["rows"])
                coverage = deepcopy(previous["coverage"])
                coverage.pop("error", None)
                cursors = set(coverage.get("cursors", []))
                query["cursor"] = coverage["next_cursor"]
        try:
            for _ in range(pages or self.max_pages):
                body = self.get(DATA_API + route, query)
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise ValueError("INVALID_PAGE")
                page = body["data"]
                if any(not isinstance(row, dict) for row in page):
                    raise ValueError("INVALID_ROW")
                for row in page:
                    if (
                        params.get("user")
                        and row.get("proxy_wallet")
                        and wallet_address(row["proxy_wallet"]) != wallet_address(params["user"])
                    ):
                        raise ValueError("PAGE_WALLET_MISMATCH")
                    if params.get("condition") and row.get("condition_id") not in params["condition"].split(","):
                        raise ValueError("PAGE_CONDITION_SCOPE_MISMATCH")
                    if route in {"/v2/trades", "/v2/activity"} and row.get("timestamp") is not None:
                        when = instant(row["timestamp"]).timestamp()
                        if (params.get("start") and when < params["start"]) or (
                            params.get("end") and when > params["end"]
                        ):
                            raise ValueError("PAGE_TIME_SCOPE_MISMATCH")
                pagination = body.get("pagination", {})
                if "next_cursor" not in pagination or not isinstance(pagination.get("has_more"), bool):
                    raise ValueError("MISSING_PAGINATION")
                cursor = pagination["next_cursor"]
                if bool(cursor) != pagination["has_more"]:
                    raise ValueError("INCONSISTENT_PAGINATION")
                if cursor and (not isinstance(cursor, str) or cursor in cursors):
                    raise ValueError("CURSOR_STALLED")
                rows.extend(page)
                coverage["pages"] += 1
                coverage["next_cursor"] = cursor
                if budget is not None and len(rows) >= budget:
                    rows = rows[:budget]
                    coverage.update(complete=True, scope=f"top_{budget}")
                    break
                if not cursor:
                    coverage["complete"] = True
                    break
                cursors.add(cursor)
                query = {**params, "cursor": cursor}
            else:
                coverage["error"] = "PAGE_BUDGET_EXHAUSTED"
        except (OSError, ValueError) as exc:
            coverage["error"] = str(exc)
        coverage["cursors"] = sorted(cursors)
        coverage["rows"] = len(rows)
        return {"rows": rows, "coverage": coverage}

    def summary(self, wallet: str, as_of: str, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """One confirmed recent buy/sell is enough for activity, never for expertise."""
        cutoff = instant(as_of)
        rows = [*(previous or {}).get("trades", []), *(previous or {}).get("local_history", {}).get("trades", [])]
        valid = [r for r in rows if _recent_trade(r, wallet, cutoff)]
        source = "saved_history"
        if not valid:
            page = self.page(
                "/v2/trades",
                {
                    "user": wallet,
                    "start": int((cutoff - timedelta(days=30)).timestamp()),
                    "end": math.ceil(cutoff.timestamp()) - 1,
                    "taker_only": "false",
                    "limit": 1,
                },
                budget=1,
                pages=1,
            )
            if page["coverage"].get("error"):
                raise OSError(page["coverage"]["error"])
            valid = [r for r in page["rows"] if _recent_trade(r, wallet, cutoff)]
            source = DATA_API + "/v2/trades"
        trade = max(valid, key=lambda r: instant(r["timestamp"]), default=None)
        return {"as_of": as_of, "source": source, "recent_trade": trade, "status": "READY" if trade else "INACTIVE"}

    def leaderboard(self, category: str, period: str) -> dict[str, Any]:
        return {
            "category": category,
            "period": period,
            **self.page(
                "/v2/leaderboard",
                {"category": category, "time_period": period, "sort_by": "PNL", "limit": 50},
                budget=50,
            ),
        }

    def resolve(self, account: str) -> dict[str, Any]:
        if not wallet_address(account):
            return {"error": "ACCOUNT_IS_NOT_A_WALLET_ADDRESS"}
        body = self.get(GAMMA_API + "/public-profile", {"address": account})
        if not isinstance(body, dict) or not wallet_address(body.get("proxyWallet")):
            return {"error": "TRADING_WALLET_MAPPING_MISSING"}
        return {"wallet": body["proxyWallet"], "evidence": {"account": account, "public_profile": body}}

    def history(
        self,
        wallet: str,
        as_of: str,
        *,
        previous: dict[str, Any] | None = None,
        repair_targets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Advance one bounded slice; saved facts and cursors are returned together."""
        if previous and (previous.get("wallet") != wallet or instant(previous["as_of"]) > instant(as_of)):
            raise ValueError("REPAIR_IDENTITY_OR_CUTOFF_MISMATCH")
        frozen = bool(previous and previous["as_of"] == as_of)
        if (
            frozen
            and previous
            and not previous.get("work")
            and repair_targets == []
            and all(
                previous.get("coverage", {}).get(feed, {}).get("complete")
                for feed in ("closed_positions", "open_positions", "trades", "activity")
            )
        ):
            return deepcopy(previous)
        history: dict[str, Any] = (
            deepcopy(previous)
            if previous
            else {
                "wallet": wallet,
                "closed_positions": [],
                "open_positions": [],
                "trades": [],
                "activity": [],
                "coverage": {},
                "markets": {},
                "resolutions": {},
                "local_history": {},
                "gaps": [],
            }
        )
        history["as_of"] = as_of
        retry_complete = repair_targets is not None and bool(
            repair_targets or history.get("condition_errors") or history.get("request_errors")
        )
        if not frozen or "work" not in history or (history["work"]["phase"] == "DONE" and retry_complete):
            history["fetch_errors"] = []
            history["condition_errors"] = {}
            history["work"] = {
                "phase": "POSITIONS",
                "feed": 0,
                "batch": 0,
                "step": 0,
                "segments": {},
                "index": [],
                "index_cursor": "",
                "index_complete": False,
                "completed_conditions": [],
                "requests": 0,
                "pages": 0,
                "retries": 0,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "base_cutoff": previous["as_of"] if previous and not frozen else None,
                "base_conditions": list(previous.get("selection", {}).get("conditions", previous.get("markets", {})))
                if previous
                else [],
                "base_complete": [
                    feed for feed, coverage in previous.get("coverage", {}).items() if coverage.get("complete")
                ]
                if previous
                else [],
                "base_block": previous.get("local_history", {})
                .get("coverage", {})
                .get("boundary", {})
                .get("block_number")
                if previous
                else None,
                "frozen_coverage": deepcopy(previous.get("coverage", {})) if frozen and previous else {},
            }
            if not frozen:
                for feed in ("closed_positions", "open_positions"):
                    history[feed] = []
                    history["coverage"][feed] = {"complete": False}
            elif previous and all(
                previous.get("coverage", {}).get(feed, {}).get("complete")
                for feed in ("closed_positions", "open_positions")
            ):
                history["work"].update(
                    phase="METADATA",
                    index_complete=True,
                    index=sorted(set(previous.get("work", {}).get("index", [])) | set(history["markets"])),
                )
            if frozen and repair_targets is not None:
                history["work"]["repair_targets"] = deepcopy(repair_targets)
        work: dict[str, Any] = history["work"]
        if work["phase"] == "DONE" and not repair_targets:
            return history
        started = time.monotonic()
        used = 0

        def read(call: Any, *, local: bool = False) -> Any:
            nonlocal used
            # Reserve time for the bounded remote read, rather than starting it at the deadline.
            if used >= self.settings.slice_requests or time.monotonic() - started >= self.settings.slice_seconds - (
                30 if local else self.timeout
            ):
                raise _SliceEnded
            used += 1
            work["requests"] += 1
            value = call()
            if not isinstance(value, dict) or value.get("coverage", {}).get("error") in (None, "PAGE_BUDGET_EXHAUSTED"):
                work["last_progress_at"] = datetime.now(timezone.utc).isoformat()
            return value

        try:
            while work["phase"] != "DONE":
                phase = work["phase"]
                if phase == "POSITIONS":
                    feed = ("closed_positions", "open_positions")[work["feed"]]
                    params = {"user": wallet, "status": "CLOSED" if work["feed"] == 0 else "OPEN", "limit": 1000}
                    if work["feed"] == 1:
                        params.update(include_archived="true", filter_amount=0)
                    saved = {"rows": history[feed], "coverage": history["coverage"].get(feed, {})}
                    if not saved["coverage"].get("complete"):
                        page = read(lambda: self.page("/v2/positions", params, previous=saved, pages=1))
                        history[feed], history["coverage"][feed] = page["rows"], page["coverage"]
                        work["pages"] += max(0, page["coverage"]["pages"] - saved["coverage"].get("pages", 0))
                        _check_page(page)
                        if not page["coverage"]["complete"]:
                            continue
                    work["feed"] += 1
                    if work["feed"] == 2:
                        work["phase"] = "EVENT_INDEX"
                elif phase == "EVENT_INDEX":
                    indexed = read(lambda: read_wallet_event_index(wallet, as_of, work["index_cursor"]), local=True)
                    rows = indexed["conditions"]
                    if rows != sorted(set(rows)) or any(
                        not re.fullmatch(r"0x[0-9a-f]{64}", c) or c <= work["index_cursor"] for c in rows
                    ):
                        raise ValueError("INVALID_EVENT_INDEX_PAGE")
                    work["index"] = sorted(set(work["index"]) | set(rows[:200]))
                    if indexed["unmapped_tokens"]:
                        history["gaps"] = sorted(set(history["gaps"]) | {"EVENT_INDEX_TOKEN_MAPPING_UNRESOLVED"})
                    if len(rows) > 200:
                        work["index_cursor"] = rows[199]
                    else:
                        work["index_complete"] = True
                        work["index"] = sorted(
                            set(work["index"])
                            | {
                                row["condition_id"]
                                for feed in ("closed_positions", "open_positions")
                                for row in history[feed]
                                if row.get("condition_id")
                            }
                        )
                        work["phase"] = "METADATA"
                elif phase == "METADATA":
                    conditions = work["index"]
                    if work["batch"] * 20 >= len(conditions):
                        work.update(phase="SELECT", batch=0)
                        continue
                    batch = conditions[work["batch"] * 20 : (work["batch"] + 1) * 20]
                    missing = (
                        batch
                        if work["base_cutoff"]
                        else [c for c in batch if c not in history["markets"] or c not in history["resolutions"]]
                    )
                    if missing and work["step"] == 0:
                        local = read(lambda: read_local_history(wallet, as_of, missing, targets=[]), local=True)
                        history["markets"].update(local["markets"])
                        history["resolutions"].update(local["resolutions"])
                    work["step"] = max(1, work["step"])
                    for step, field, endpoint, parameter, identity in (
                        (1, "markets", GAMMA_API + "/markets", "condition_ids", "conditionId"),
                        (2, "resolutions", DATA_API + "/v2/resolutions", "condition", "condition_id"),
                    ):
                        if work["step"] != step:
                            continue
                        missing = [c for c in batch if c not in history[field]]
                        if missing:
                            try:
                                body = read(
                                    lambda: self.get(
                                        endpoint,
                                        {parameter: missing if parameter == "condition_ids" else ",".join(missing)},
                                    )
                                )
                                rows = body if isinstance(body, list) else body["data"]
                                for condition in missing:
                                    matches = [r for r in rows if str(r.get(identity, "")).lower() == condition]
                                    if len(matches) == 1:
                                        history[field][condition] = matches[0]
                                    elif len(matches) > 1:
                                        history["condition_errors"].setdefault(condition, []).append(
                                            "AMBIGUOUS_METADATA"
                                        )
                            except OSError as exc:
                                for condition in missing:
                                    history["condition_errors"].setdefault(condition, []).append(str(exc))
                        work["step"] += 1
                    for c in batch:
                        if c not in history["markets"]:
                            history["condition_errors"].setdefault(c, []).append(
                                "MARKET_OR_EVENT_INDEX_METADATA_MISSING"
                            )
                        elif history["markets"][c].get("closed") and c not in history["resolutions"]:
                            history["condition_errors"].setdefault(c, []).append("FORMAL_SETTLEMENT_UNAVAILABLE")
                    work["batch"] += 1
                    work["step"] = 0
                elif phase == "SELECT":
                    from smart_money.wallets.history import event_scopes

                    scopes, unknown = event_scopes(history, as_of=as_of)
                    selected = sorted(set().union(*scopes.values(), set(unknown)))
                    history["selection"] = {
                        "start": (
                            instant(as_of) - timedelta(days=DirectionalExpertPolicy().history_window_days)
                        ).isoformat(),
                        "end": as_of,
                        "conditions": selected,
                        "index_source": "wallet_positions+local_participation",
                        "index_complete": work["index_complete"],
                    }
                    for feed in ("closed_positions", "open_positions", "trades", "activity"):
                        history[feed] = [
                            r for r in history[feed] if r.get("condition_id") in selected or not r.get("condition_id")
                        ]
                    for feed in ("trades", "operations"):
                        local = history["local_history"]
                        if feed in local:
                            local[feed] = [
                                r for r in local[feed] if not r.get("condition_id") or r["condition_id"] in selected
                            ]
                    batches: list[list[str]] = []
                    remaining = set(selected)
                    for sector in sorted(scopes, key=lambda s: (s.count("."), s)):
                        members = sorted(scopes[sector] & remaining)
                        batches.extend(members[i : i + 20] for i in range(0, len(members), 20))
                        remaining.difference_update(members)
                    rest = sorted(remaining)
                    batches.extend(rest[i : i + 20] for i in range(0, len(rest), 20))
                    work.update(phase="BOUNDARY", batches=batches, batch=0)
                    for feed in ("trades", "activity"):
                        history["coverage"][feed] = {"complete": False, "conditions": {}}
                elif phase == "BOUNDARY":
                    local = history["local_history"]
                    boundary = local.get("coverage", {}).get("boundary", {})
                    try:
                        cutoff_block(boundary, as_of)
                    except ValueError:
                        boundary = read(lambda: read_cutoff_boundary(as_of), local=True)
                    local.update(wallet=wallet, as_of=as_of)
                    local.setdefault("coverage", {})["boundary"] = boundary
                    work["phase"] = "FACTS"
                elif phase == "FACTS":
                    if work["batch"] >= len(work["batches"]):
                        failed = work.setdefault("failed_batches", {})
                        if failed:
                            retry_batch = min(failed, key=int)
                            work.update(batch=int(retry_batch), step=failed.pop(retry_batch))
                        else:
                            work["phase"] = "DONE"
                            continue
                    batch = work["batches"][work["batch"]]
                    if all(c in work["completed_conditions"] for c in batch):
                        work["batch"] += 1
                        continue
                    step = work["step"]
                    key = str(work["batch"])
                    segment = work["segments"].setdefault(key, {})
                    targets = [
                        {"feed": feed, "condition_id": c} for c in batch for feed in ("orderfilled", "operations")
                    ]
                    if work["base_cutoff"] and work["base_block"]:
                        for target in targets:
                            if target["condition_id"] in work["base_conditions"]:
                                target.update(
                                    from_block=work["base_block"] + 1,
                                    to_block=cutoff_block(history["local_history"]["coverage"]["boundary"], as_of),
                                )
                    if work["frozen_coverage"] and history["local_history"].get("trades") and "repair_targets" in work:
                        targets = work["repair_targets"]
                    local = history["local_history"]
                    if step < 2:
                        feed = ("trades", "activity")[step]
                        if work["frozen_coverage"].get(feed, {}).get("complete"):
                            work["step"] += 1
                            continue
                        # A condition is fixed for every page. Earlier costs are needed only for these events.
                        start = 1
                        if (
                            work["base_cutoff"]
                            and all(c in work["base_conditions"] for c in batch)
                            and feed in work["base_complete"]
                        ):
                            start = max(1, int(instant(work["base_cutoff"]).timestamp()) - 1)
                        params = {
                            "user": wallet,
                            "condition": ",".join(batch),
                            "start": start,
                            "end": math.ceil(instant(as_of).timestamp()) - 1,
                            "limit": 1000,
                        }
                        if feed == "trades":
                            params["taker_only"] = "false"
                        saved = {"rows": [], "coverage": segment.get(feed, {})}
                        if saved["coverage"]:
                            params = saved["coverage"]["params"]
                        if not saved["coverage"] and work["frozen_coverage"].get(feed, {}).get("next_cursor"):
                            saved["coverage"] = work["frozen_coverage"][feed]
                            params = saved["coverage"]["params"]
                        page = read(lambda: self.page("/v2/" + feed, params, previous=saved, pages=1))
                        if page["coverage"]["pages"] > saved["coverage"].get("pages", 0):
                            if not saved["coverage"].get("next_cursor"):
                                # Replace the reread interval once; identical native rows may be distinct fills.
                                history[feed] = [
                                    r
                                    for r in history[feed]
                                    if r.get("condition_id") not in batch
                                    or not int(params["start"])
                                    <= instant(r["timestamp"]).timestamp()
                                    <= int(params["end"])
                                ]
                            history[feed].extend(page["rows"])
                        if work["frozen_coverage"]:
                            known = {r["transaction_hash"] for r in local.get("trades", [])}
                            for row in page["rows"]:
                                if (feed == "trades" or row.get("type") == "TRADE") and row.get(
                                    "transaction_hash"
                                ) not in known:
                                    work.setdefault("repair_targets", []).append(
                                        {"feed": "orderfilled", "transaction_hash": row["transaction_hash"]}
                                    )
                        segment[feed] = page["coverage"]
                        work["pages"] += max(0, page["coverage"]["pages"] - saved["coverage"].get("pages", 0))
                        _check_page(page)
                        if not page["coverage"]["complete"]:
                            continue
                    elif step == 2:
                        if (
                            work["frozen_coverage"]
                            and local.get("source")
                            and not any(t["feed"] in {"orderfilled", "activity", "boundary"} for t in targets)
                        ):
                            work["step"] += 1
                            continue
                        fetched = read(
                            lambda: read_local_history(
                                wallet,
                                as_of,
                                batch,
                                targets=targets,
                                cursor=segment.get("local_cursor"),
                                page_size=1000,
                            ),
                            local=True,
                        )
                        fetched = deepcopy(fetched)
                        enriched = {r["trade_id"]: r for r in fetched["trades"]}
                        saved_fills = [
                            r
                            for r in local.get("trades", [])
                            if not (r.get("trade_id") in enriched and r.items() <= enriched[r["trade_id"]].items())
                        ]
                        local["trades"] = _merge_rows(saved_fills, fetched.pop("trades"))
                        boundary = local["coverage"]["boundary"]
                        local["coverage"].update(fetched["coverage"])
                        local["coverage"]["boundary"] = boundary
                        local["source"] = fetched["source"]
                        local["issues"] = []  # Batch errors belong to their conditions, not every sector.
                        for c in batch:
                            if fetched.get("issues"):
                                history["condition_errors"][c] = fetched["issues"]
                        segment["local_cursor"] = fetched.get("next_cursor")
                        if segment["local_cursor"]:
                            continue
                    elif step == 3:
                        if (
                            work["frozen_coverage"]
                            and "operations" in local
                            and not any(t["feed"] in {"activity", "operations"} for t in targets)
                        ):
                            work["step"] += 1
                            continue
                        operations = read(
                            lambda: read_local_operations(
                                wallet, as_of, targets=targets, cursor=segment.get("operation_cursor"), page_size=1000
                            ),
                            local=True,
                        )
                        local["operations"] = _merge_rows(local.get("operations", []), operations[:1000])
                        if len(operations) > 1000:
                            last = operations[999]
                            segment["operation_cursor"] = [last["block_number"], last["log_index"]]
                            continue
                    else:
                        receipts = local.setdefault("receipts", {})
                        required = sorted(
                            {
                                r["transaction_hash"].lower()
                                for r in local.get("trades", [])
                                if r.get("condition_id") in batch
                                and (r.get("fee_usdc") is None or decimal_value(r.get("fee_raw", r["fee_usdc"])) > 0)
                            }
                            - receipts.keys()
                        )
                        requested = [
                            t["transaction_hash"]
                            for t in targets
                            if t["feed"] == "fee_refunds"
                            and t.get("transaction_hash")
                            and t["transaction_hash"] not in segment.get("receipt_repairs", [])
                        ]
                        required = sorted(set(required) | set(requested))
                        if required:
                            result = read(lambda: read_local_receipts(required[:100], as_of), local=True)
                            receipts.update(result)
                            segment.setdefault("receipt_repairs", []).extend(result)
                            if set(required[:100]) - result.keys():
                                raise OSError("REQUIRED_FEE_RECEIPTS_UNAVAILABLE")
                            continue
                        work["completed_conditions"].extend(c for c in batch if c not in work["completed_conditions"])
                        for c in batch:
                            history.get("request_errors", {}).pop(c, None)
                        for feed in ("trades", "activity"):
                            history["coverage"][feed]["conditions"].update(dict.fromkeys(batch, True))
                        work.update(batch=work["batch"] + 1, step=0)
                        continue
                    work["step"] += 1
                else:
                    raise ValueError("UNKNOWN_HISTORY_PHASE")
            work.update(outcome="COMPLETE", next_attempt_at=None)
            for feed in ("trades", "activity"):
                history["coverage"][feed]["complete"] = all(
                    history["coverage"][feed]["conditions"].get(c) for c in history["selection"]["conditions"]
                )
            work.pop("error", None)
            work["retries"] = 0
            work["retry_counts"] = {}
        except _SliceEnded:
            work.update(outcome="YIELDED", next_attempt_at=datetime.now(timezone.utc).isoformat())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            retry_key = f"{work['phase']}:{work['batch']}:{work.get('feed')}:{work['step']}"
            counts = work.setdefault("retry_counts", {})
            counts[retry_key] = counts.get(retry_key, 0) + 1
            work["retries"] = counts[retry_key]
            delay = 30 if work["retries"] <= self.settings.extra_retries else 86400
            retry_at = (
                max(time.time() + delay, *self.host_backoff.values()) if self.host_backoff else time.time() + delay
            )
            work.update(
                outcome="SOURCE_ERROR",
                error=str(exc),
                next_attempt_at=datetime.fromtimestamp(retry_at, timezone.utc).isoformat(),
            )
            if work["phase"] == "FACTS" and not work["frozen_coverage"]:
                for condition in work["batches"][work["batch"]]:
                    history.setdefault("request_errors", {})[condition] = [str(exc)]
                work.setdefault("failed_batches", {})[str(work["batch"])] = work["step"]
                work.update(batch=work["batch"] + 1, step=0)
        work["elapsed_seconds"] = work.get("elapsed_seconds", 0) + time.monotonic() - started
        work["records"] = sum(len(history[f]) for f in ("closed_positions", "open_positions", "trades", "activity"))
        return history

    def close(self) -> None:
        self.session.close()


def _merge_rows(saved: list[dict[str, Any]], fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Equal copies collapse; conflicting versions remain visible to reconciliation.
    return list({json.dumps(row, sort_keys=True): row for row in [*saved, *fetched]}.values())


class _SliceEnded(Exception):
    pass


def _check_page(page: dict[str, Any]) -> None:
    error = page["coverage"].get("error")
    if error and error != "PAGE_BUDGET_EXHAUSTED":
        raise OSError(error)


def _recent_trade(row: dict[str, Any], wallet: str, cutoff: datetime) -> bool:
    try:
        return bool(
            wallet_address(row.get("proxy_wallet")) == wallet
            and row.get("side") in {"BUY", "SELL"}
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", str(row.get("transaction_hash", "")))
            and row.get("condition_id")
            and row.get("token_id") is not None
            and decimal_value(row["size"]) > 0
            and 0 < decimal_value(row["price"]) <= 1
            and cutoff - timedelta(days=30) <= instant(row["timestamp"]) < cutoff
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
