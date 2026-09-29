"""Read existing XUE-LAB wallet facts and coverage receipts without starting writers."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
from typing import Any
from urllib.parse import urlparse

import paramiko
import zstandard

from smart_money.markets.trades import (
    CTF,
    TRANSACTION_SOURCE,
    wallet_fill,
)
from smart_money.wallets.discovery import wallet_address
from smart_money.wallets.history import cutoff_block, instant

# Use the server's existing psql; credentials travel on SSH stdin, never argv.
_PSQL = """import json, os, re, subprocess, sys
p = json.load(sys.stdin)
env = dict(os.environ, PGPASSWORD=p['password'], PGCONNECT_TIMEOUT='10',
           PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=15000',
           PGAPPNAME='smart-money-history-reader')
r = subprocess.run(['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate',
                        '-h', '127.0.0.1', '-p', p['port'], '-U', p['user'], '-d', p['database']],
                       input=p['sql'], env=env, capture_output=True, text=True, timeout=18)
error = re.search(r'(?:ERROR|FATAL):\\s+([0-9A-Z]{5})', r.stderr)
sys.stdout.write(r.stdout if r.returncode == 0 else (error.group(1) if error else 'UNKNOWN'))
sys.exit(r.returncode)
"""


def _target_filter(targets: list[dict[str, Any]] | None, feeds: set[str], *, postgres: bool) -> str:
    """Validated repair identities become bounded predicates; [] reads no facts."""
    if targets is None:
        return "TRUE"
    clauses = []
    for target in targets:
        if target.get("feed") not in feeds or target.get("missing_in") == "official":
            continue
        if "condition_id" in target:
            condition = str(target["condition_id"]).lower()
            if not re.fullmatch(r"0x[0-9a-f]{64}", condition):
                raise ValueError("INVALID_REPAIR_CONDITION")
            clauses.append(
                f"(maker_asset_id IN (SELECT token_id FROM core.market_tokens WHERE condition_id='{condition}') "
                f"OR taker_asset_id IN (SELECT token_id FROM core.market_tokens WHERE condition_id='{condition}'))"
                if postgres
                else f"condition_id IN ('{condition}', '{condition[2:]}')"
            )
            if "from_block" in target:
                start, end = target["from_block"], target["to_block"]
                if type(start) is not int or type(end) is not int or start <= 0 or end < start:
                    raise ValueError("INVALID_REPAIR_BLOCK_RANGE")
                clauses[-1] = f"({clauses[-1]}) AND block_number BETWEEN {start} AND {end}"
        elif "transaction_hash" in target:
            tx = str(target["transaction_hash"]).lower()
            if not re.fullmatch(r"0x[0-9a-f]{64}", tx):
                raise ValueError("INVALID_REPAIR_TRANSACTION")
            clauses.append(f"tx_hash=decode('{tx[2:]}','hex')" if postgres else f"tx_hash IN ('{tx}', '{tx[2:]}')")
        elif "from_block" in target and "to_block" in target:
            start, end = target["from_block"], target["to_block"]
            if type(start) is not int or type(end) is not int or not 0 < start <= end:
                raise ValueError("INVALID_REPAIR_BLOCK_RANGE")
            clauses.append(f"block_number BETWEEN {start} AND {end}")
    return " OR ".join(dict.fromkeys(clauses)) or "FALSE"


def read_wallet_event_index(wallet: str, as_of: str, after: str = "") -> dict[str, Any]:
    """Page the existing wallet/token index, including exited losses, without exporting fills."""
    wallet = wallet_address(wallet) or ""
    if not wallet or (after and not re.fullmatch(r"0x[0-9a-f]{64}", after)):
        raise ValueError("INVALID_WALLET_EVENT_INDEX_CURSOR")
    cutoff = instant(as_of).isoformat()
    result: dict[str, Any] = _postgres_query(f"""
WITH participation AS (
 SELECT DISTINCT CASE WHEN maker_asset_id='0' THEN taker_asset_id ELSE maker_asset_id END AS token_id
 FROM core.orderfilled_raw
 WHERE (maker=decode('{wallet[2:]}','hex') OR taker=decode('{wallet[2:]}','hex'))
   AND (block_time < '{cutoff}'::timestamptz OR block_time IS NULL)
), conditions AS (
 SELECT DISTINCT t.condition_id FROM participation p LEFT JOIN core.market_tokens t USING (token_id)
)
SELECT json_build_object(
 'conditions', COALESCE((SELECT json_agg(condition_id) FROM (
   SELECT condition_id FROM conditions WHERE condition_id > '{after}' ORDER BY condition_id LIMIT 201
 ) c), '[]'::json),
 'unmapped_tokens', (SELECT count(*) FROM participation p
   WHERE NOT EXISTS (SELECT 1 FROM core.market_tokens t WHERE t.token_id=p.token_id)),
 'source', 'xue-lab:core.orderfilled_raw+core.market_tokens');
""")
    return result


def _resolution_query(selection: str, cutoff: str) -> str:
    return f"""
    SELECT DISTINCT ON (condition_id) condition_id, payout, event_time, block_number, tx_hash
    FROM oracle.oracle_events WHERE lower(source_oracle)='{CTF}' AND event_status='settle'
      AND condition_id IN ({selection}) AND event_time <= '{cutoff}'::timestamptz
    ORDER BY condition_id,block_number DESC,log_index DESC
    """


def _resolution_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        row["condition_id"]: {
            "condition_id": row["condition_id"],
            "status": "RESOLVED",
            "payouts": json.loads(row["payout"]),
            "resolved_at": row["event_time"],
            "resolved_block": row["block_number"],
            "transaction_hash": row["tx_hash"],
        }
        for row in rows
    }


def read_resolutions(conditions: list[str], as_of: str) -> dict[str, dict[str, Any]]:
    """Poll known conditions, including late-arriving settlements, without a timestamp watermark."""
    if any(not re.fullmatch(r"0x[0-9a-f]{64}", condition) for condition in conditions):
        raise ValueError("INVALID_CONDITION_ID")
    if not conditions:
        return {}
    selection = ",".join(f"'{condition}'" for condition in sorted(set(conditions)))
    query = _resolution_query(selection, instant(as_of).isoformat())
    return _resolution_rows(_postgres_query(f"SELECT COALESCE(json_agg(r),'[]'::json) FROM ({query}) r"))


def read_local_history(
    wallet: str,
    as_of: str,
    conditions: list[str],
    *,
    targets: list[dict[str, Any]] | None = None,
    cursor: list[int] | None = None,
    page_size: int = 1000,
) -> dict[str, Any]:
    wallet = wallet_address(wallet) or ""
    if not wallet:
        raise ValueError("INVALID_WALLET")
    cutoff = instant(as_of).isoformat()
    if any(
        len(c) != 66 or not c.startswith("0x") or any(x not in "0123456789abcdef" for x in c[2:]) for c in conditions
    ):
        raise ValueError("INVALID_CONDITION_ID")
    # Only validated addresses and a parsed timestamp enter SQL literals.
    selected = ",".join(f"'{c}'" for c in conditions)
    selection = _target_filter(targets, {"orderfilled"}, postgres=True)
    seek = _seek(cursor)
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH fills AS MATERIALIZED (
    SELECT contract,event_topic,order_hash,tx_hash,log_index,block_number,block_time,maker,taker,
           maker_asset_id,taker_asset_id,maker_amount,taker_amount,fee,
           CASE WHEN maker_asset_id='0' THEN taker_asset_id ELSE maker_asset_id END AS asset
    FROM core.orderfilled_raw
    WHERE maker=decode('{wallet[2:]}','hex') AND (block_time <= '{cutoff}'::timestamptz OR block_time IS NULL)
      AND ({selection})
      AND ({seek})
    ORDER BY block_number, log_index LIMIT {int(page_size) + 1}
), tokens AS (
    SELECT token_id, min(condition_id) AS condition_id, min(outcome_index) AS outcome_index,
           count(DISTINCT (condition_id,outcome_index)) AS mappings
    FROM core.market_tokens WHERE token_id IN (SELECT asset FROM fills) GROUP BY token_id
), conditions AS (
    SELECT condition_id FROM tokens UNION SELECT unnest(ARRAY[{selected}]::text[])
)
SELECT json_build_object(
 'fills', COALESCE((SELECT json_agg(r) FROM (
    SELECT '0x'||encode(f.contract,'hex') AS contract, '0x'||encode(f.event_topic,'hex') AS event_topic,
           '0x'||encode(f.order_hash,'hex') AS order_hash,
           '0x'||encode(f.tx_hash,'hex') AS transaction_hash, f.log_index, f.block_number, f.block_time,
           '0x'||encode(f.maker,'hex') AS maker, '0x'||encode(f.taker,'hex') AS taker,
           f.maker_asset_id, f.taker_asset_id, f.maker_amount AS maker_amount_filled,
           f.taker_amount AS taker_amount_filled, f.fee, t.condition_id, t.outcome_index, t.mappings
    FROM fills f LEFT JOIN tokens t ON t.token_id=f.asset ORDER BY f.block_number,f.log_index
 ) r),'[]'::json),
 'markets', COALESCE((SELECT json_agg(m) FROM core.markets m
                     WHERE condition_id IN (SELECT * FROM conditions)), '[]'::json),
 'resolutions', COALESCE((SELECT json_agg(r) FROM (
    {_resolution_query("SELECT * FROM conditions", cutoff)}
 ) r),'[]'::json),
 'coverage', json_build_object(
    'orderfilled', (SELECT json_agg(json_build_array(lower(r),upper(r)-1)) FROM (
        SELECT unnest(range_agg(int8range(from_block,to_block+1))) AS r FROM core.orderfilled_sync_windows
        WHERE status='complete' AND exchange_set='known_orderfilled'
          AND missing_count=0 AND chain_log_count=db_log_count
    ) windows),
    'activity', (SELECT json_agg(json_build_array(lower(r),upper(r)-1)) FROM (
        SELECT unnest(range_agg(int8range(from_block,to_block+1))) AS r FROM core.non_trade_sync_windows
        WHERE status='complete' AND event_set='ctf_non_trade_and_rebate'
          AND chain_log_count=db_row_count AND skipped_count=0
    ) windows)
 ));
ROLLBACK;
"""
    result = _postgres_query(sql)
    issues: list[str] = []
    rows = result.pop("fills")
    next_cursor = (
        [int(rows[page_size - 1][key]) for key in ("block_number", "log_index")] if len(rows) > page_size else None
    )
    trades = []
    for row in rows[:page_size]:
        try:
            if row["mappings"] != 1 or row["outcome_index"] not in (0, 1):
                raise ValueError("LOCAL_TOKEN_MAPPING_UNRESOLVED")
            trades.append(wallet_fill(row, wallet, instant(row["block_time"]).isoformat()))
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            issues.append(f"LOCAL_FILL_INVALID:{row['transaction_hash']}:{row['log_index']}:{exc}")
    result["resolutions"] = _resolution_rows(result["resolutions"])
    result["markets"] = {row["condition_id"]: row for row in result["markets"]}
    return {
        **result,
        "wallet": wallet,
        "as_of": cutoff,
        "trades": trades,
        "next_cursor": next_cursor,
        "issues": sorted(set(issues)),
        "source": "xue-lab:core.orderfilled_raw+core.*_sync_windows+oracle.oracle_events",
    }


def _postgres_query(sql: str) -> Any:
    required = ("xue_lab_ip", "xue_lab_user", "xue_lab_pwd", "POLYDATA_POSTGRES_USER", "POLYDATA_POSTGRES_PASSWORD")
    if any(not os.environ.get(key) for key in required):
        raise OSError("LOCAL_HISTORY_SOURCE_NOT_CONFIGURED")
    return _remote_query(
        _PSQL,
        {
            "sql": sql,
            "password": os.environ["POLYDATA_POSTGRES_PASSWORD"],
            "user": os.environ["POLYDATA_POSTGRES_USER"],
            "database": os.environ.get("POLYDATA_POSTGRES_DATABASE", "poly_data_core"),
            "port": str(int(os.environ.get("XUE_LAB_POSTGRES_PORT", "45432"))),
        },
    )


def read_token_markets(tokens: list[str]) -> list[dict[str, Any]]:
    """Reuse local identity mappings for a bounded set of observed tokens."""
    if not tokens or any(not re.fullmatch(r"[1-9][0-9]*", token) for token in tokens):
        raise ValueError("INVALID_TOKEN_IDS")
    selected = ",".join(f"'{token}'" for token in tokens)
    rows: list[dict[str, Any]] = _postgres_query(f"""
SELECT COALESCE(json_agg(m),'[]'::json) FROM core.markets m
WHERE m.condition_id IN (SELECT condition_id FROM core.market_tokens WHERE token_id IN ({selected}));
""")
    return rows


def read_local_receipts(transaction_hashes: list[str], as_of: str) -> dict[str, dict[str, Any]]:
    """Read only requested, already validated upstream transactions; missing rows stay missing."""
    hashes = sorted(set(transaction_hashes))
    if any(not re.fullmatch(r"0x[0-9a-f]{64}", tx) for tx in hashes):
        raise ValueError("INVALID_REPAIR_TRANSACTION")
    cutoff = instant(as_of)
    result = {}
    for start in range(0, len(hashes), 100):
        selected = ",".join(f"decode('{tx[2:]}','hex')" for tx in hashes[start : start + 100])
        rows = _postgres_query(f"""
SELECT COALESCE(json_agg(r),'[]'::json) FROM (
 SELECT '0x'||encode(tx_hash,'hex') AS transaction_hash,chain_id,block_number,block_time,
        encode(evidence_zstd,'base64') AS evidence
 FROM core.orderfilled_transactions
 WHERE tx_hash IN ({selected}) AND block_time <= '{cutoff.isoformat()}'::timestamptz
) r;
""")
        for row in rows:
            tx_hash = row["transaction_hash"]
            try:
                compressed = base64.b64decode(row.pop("evidence"))
                # Same v1 envelope and 64 MiB bound as market_data.orderfilled.storage.
                limit = 64 * 1024 * 1024
                if zstandard.frame_content_size(compressed) > limit:
                    raise ValueError("RECEIPT_SIZE_LIMIT")
                version, tx, receipt = json.loads(
                    zstandard.ZstdDecompressor().decompress(compressed, max_output_size=limit, allow_extra_data=False)
                )
                if version != 1 or not isinstance(tx, dict) or not isinstance(receipt, dict):
                    raise ValueError("RECEIPT_FORMAT_UNSUPPORTED")
                result[tx_hash] = {**row, "transaction_json": tx, "receipt_json": receipt, "source": TRANSACTION_SOURCE}
            except (ValueError, TypeError, zstandard.ZstdError) as exc:
                result[tx_hash] = {"error": f"RECEIPT_DECODE_FAILED:{type(exc).__name__}"}
    return result


def _remote_query(program: str, parameters: dict[str, Any]) -> Any:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    try:
        client.connect(
            os.environ["xue_lab_ip"],
            username=os.environ["xue_lab_user"],
            password=os.environ["xue_lab_pwd"],
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
            auth_timeout=5,
            banner_timeout=5,
        )
        stdin, stdout, stderr = client.exec_command("python3 -c " + shlex.quote(program), timeout=22)
        try:
            stdin.write(json.dumps(parameters))
            stdin.channel.shutdown_write()
            payload = stdout.read()
            if stdout.channel.recv_exit_status() != 0:
                raise OSError("LOCAL_HISTORY_QUERY_FAILED:" + payload.decode().strip())
        finally:
            stdin.close()
            stdout.close()
            stderr.close()
    except paramiko.SSHException as exc:
        raise OSError("LOCAL_HISTORY_SSH_FAILED") from exc
    finally:
        client.close()
    return json.loads(payload)


_CLICKHOUSE = """import base64, json, sys, urllib.request
p = json.load(sys.stdin)
request = urllib.request.Request(p['url'], data=(p['sql']+' FORMAT JSON').encode(),
    headers={'Authorization': 'Basic '+base64.b64encode((p['user']+':'+p['password']).encode()).decode()})
try:
    with urllib.request.urlopen(request, timeout=18) as response:
        sys.stdout.write(response.read().decode())
except Exception as exc:
    sys.stdout.write(type(exc).__name__)
    sys.exit(1)
"""


def _clickhouse_query(sql: str) -> list[dict[str, Any]]:
    prefix = "POLYDATA_ORDERFILLED_CLICKHOUSE_"
    required = ("xue_lab_ip", "xue_lab_user", "xue_lab_pwd", prefix + "HTTP_URL", prefix + "USER", prefix + "PASSWORD")
    if any(not os.environ.get(key) for key in required):
        raise OSError("LOCAL_OPERATIONS_SOURCE_NOT_CONFIGURED")
    port = urlparse(os.environ[prefix + "HTTP_URL"]).port or 8123
    body = _remote_query(
        _CLICKHOUSE,
        {
            "url": f"http://127.0.0.1:{port}/?readonly=1&max_execution_time=15",
            "user": os.environ[prefix + "USER"],
            "password": os.environ[prefix + "PASSWORD"],
            "sql": sql,
        },
    )
    rows: list[dict[str, Any]] = body["data"]
    return rows


def read_cutoff_boundary(as_of: str) -> dict[str, Any]:
    cutoff = instant(as_of).timestamp()
    rows = _clickhouse_query(f"""
SELECT argMaxIf(tuple(block_number,timestamp,hash),block_number,timestamp <= {cutoff}) AS before,
       argMinIf(tuple(block_number,timestamp,hash),block_number,timestamp > {cutoff}) AS after
FROM (
 SELECT block_number,toUnixTimestamp(any(block_time)) AS timestamp,any(block_hash) AS hash
 FROM poly_orderfilled.block_timestamps
 WHERE block_time BETWEEN toDateTime({int(cutoff)}-86400,'UTC') AND toDateTime({int(cutoff)}+86400,'UTC')
 GROUP BY block_number HAVING uniqExact(block_time)=1 AND uniqExact(block_hash)=1
)
""")
    before, after = rows[0]["before"], rows[0]["after"]
    boundary = {
        "block_number": int(before[0]),
        "block_time": instant(int(before[1])).isoformat(),
        "block_hash": "0x" + str(before[2]).lower().removeprefix("0x"),
        "next_block_number": int(after[0]),
        "next_block_time": instant(int(after[1])).isoformat(),
        "next_block_hash": "0x" + str(after[2]).lower().removeprefix("0x"),
        "source": "xue-lab:poly_orderfilled.block_timestamps",
    }
    cutoff_block(boundary, as_of)
    return boundary


def read_local_operations(
    wallet: str,
    as_of: str,
    *,
    targets: list[dict[str, Any]] | None = None,
    cursor: list[int] | None = None,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Read the existing non-trade owner projection; an empty result proves no coverage."""
    wallet = wallet_address(wallet) or ""
    if not wallet:
        raise ValueError("INVALID_WALLET")
    cutoff = int(instant(as_of).timestamp())
    selection = _target_filter(targets, {"activity", "operations"}, postgres=False)
    seek = _seek(cursor)
    rows = _clickhouse_query(f"""
SELECT n.*, toUnixTimestamp(b.block_time) AS timestamp FROM (
 SELECT event_key, address, cashflow_type, toString(usdc_amount) AS usdc_size,
        collateral_token, condition_id, parent_collection_id, partition_json,
        tx_hash AS transaction_hash, log_index, block_number, source_contract, source
 FROM poly_orderfilled.non_trade_cashflows FINAL
 WHERE address IN ('{wallet}', '{wallet[2:]}') AND ({selection}) AND ({seek})
 ORDER BY block_number, log_index LIMIT {int(page_size) + 1}
) n LEFT JOIN (
 SELECT block_number, argMax(block_time, ingested_at) AS block_time
 FROM poly_orderfilled.block_timestamps
 WHERE block_number IN (SELECT block_number FROM poly_orderfilled.non_trade_cashflows
                       WHERE address IN ('{wallet}', '{wallet[2:]}') AND ({selection}))
 GROUP BY block_number
) b USING block_number
WHERE timestamp <= {cutoff}
ORDER BY block_number, log_index
""")
    for row in rows:
        for key in (
            "address",
            "condition_id",
            "transaction_hash",
            "source_contract",
            "collateral_token",
            "parent_collection_id",
        ):
            row[key] = "0x" + str(row[key]).lower().removeprefix("0x")
        row["proxy_wallet"] = row.pop("address")
        row["type"] = row.pop("cashflow_type")
        row["partition"] = json.loads(row.pop("partition_json") or "[]")
        row["block_number"] = int(row["block_number"])
    return rows


def _seek(cursor: list[int] | None) -> str:
    if cursor is None:
        return "TRUE"
    if len(cursor) != 2 or any(type(n) is not int or n < 0 for n in cursor):
        raise ValueError("INVALID_LOCAL_PAGE_CURSOR")
    return f"(block_number,log_index) > ({cursor[0]},{cursor[1]})"
