"""Merge discovery evidence; leaderboard performance never grants qualification."""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from smart_money.markets.taxonomy import node_ids

# Official category names are separate from our profile taxonomy.
LEADERBOARD_SECTORS = {
    "crypto": "CRYPTO",
    "politics": "POLITICS",
    "sports": "SPORTS",
    "esports": "SPORTS.ESPORTS",
    "weather": "WEATHER",
    "economics": "ECONOMICS",
    "finance": "FINANCE",
    "tech": "TECH",
    "mentions": "MENTIONS",
    "pop-culture": "ENTERTAINMENT",
}


def wallet_address(value: Any) -> str | None:
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"0x[0-9a-f]{40}", value) else None


def discover_candidates(
    existing: list[dict[str, Any]],
    boards: list[dict[str, Any]],
    local: list[dict[str, Any]],
    *,
    as_of: str,
    resolve: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = {row["candidate_id"]: deepcopy(row) for row in existing}
    aliases = {alias: key for key, row in candidates.items() for alias in row.get("aliases", [])}
    seeds = list(local)
    for board in boards:
        for row in board["rows"][:50]:
            seeds.append(
                {
                    "account": row.get("user_id"),
                    "username": row.get("user_name"),
                    "sector_id": LEADERBOARD_SECTORS[board["category"]],
                    "source": f"official:{board['category']}:{board['period']}",
                    "pnl": row.get("pnl"),
                    "rank": row.get("rank"),
                    "observed_at": board.get("as_of", as_of),
                }
            )
    # Revisit unresolved identities even after they leave the boards.
    seeds.extend(
        {"account": row["aliases"][0], "source": "existing", "sector_id": sector}
        for row in existing
        for sector in row["sectors"]
        if row.get("mapping_status") != "CONFIRMED"
    )
    mappings: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        source = str(seed.get("source") or "local")
        if "polybeats" in source.lower():
            raise ValueError("PolyBeats evaluation addresses cannot be discovery inputs")
        sector = str(seed.get("sector_id") or "").upper()
        if sector not in node_ids() - {"OTHER"}:
            raise ValueError(f"Unsupported candidate sector: {sector}")
        account = str(seed.get("account") or seed.get("wallet") or "").strip().lower()
        if not account:
            raise ValueError("Candidate account is required")
        known = candidates.get(aliases.get(account, account))
        if known and known.get("mapping_status") == "CONFIRMED":
            mapping = {"wallet": known["wallet"], "evidence": known["mapping_evidence"]}
        elif seed.get("trading_wallet") and seed.get("mapping_evidence"):
            mapping = {"wallet": seed["trading_wallet"], "evidence": seed["mapping_evidence"]}
        else:
            if account not in mappings:
                try:
                    mappings[account] = resolve(account)
                except (OSError, ValueError) as exc:
                    mappings[account] = {"error": str(exc)}
            mapping = mappings[account]
        wallet = wallet_address(mapping.get("wallet")) if mapping.get("evidence") else None
        key = wallet or f"unmapped:{account}"
        row = candidates.setdefault(
            key,
            {
                "candidate_id": key,
                "wallet": wallet,
                "aliases": [],
                "sectors": [],
                "sources": [],
                "first_discovered_at": as_of,
            },
        )
        old_key = aliases.get(account)
        if old_key and old_key != key:
            old = candidates.pop(old_key)
            row["first_discovered_at"] = min(row["first_discovered_at"], old["first_discovered_at"])
            for field in ("aliases", "sectors", "sources"):
                row[field].extend(item for item in old[field] if item not in row[field])
        row.update(
            mapping_status="CONFIRMED" if wallet else "UNRESOLVED",
            mapping_evidence=mapping.get("evidence"),
            mapping_error=mapping.get("error") if not wallet else None,
            username=seed.get("username") or row.get("username"),
        )
        for alias in [account, *row["aliases"]]:
            aliases[alias] = key
            if alias not in row["aliases"]:
                row["aliases"].append(alias)
        if sector not in row["sectors"]:
            row["sectors"].append(sector)
        if source != "existing":
            found = next((s for s in row["sources"] if s["source"] == source and s["sector_id"] == sector), None)
            if found is None:
                found = {"source": source, "sector_id": sector, "first_seen_at": as_of}
                row["sources"].append(found)
            observed_at = str(seed.get("observed_at") or as_of)
            if observed_at >= found.get("last_seen_at", ""):
                found.update(last_seen_at=observed_at, pnl=seed.get("pnl"), rank=seed.get("rank"))
    return sorted(
        candidates.values(),
        key=lambda row: (not any(float(s.get("pnl") or 0) > 0 for s in row["sources"]), row["candidate_id"]),
    )
