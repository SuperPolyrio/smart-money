"""Small watchlist state and bounded, immutable JSON evidence blocks.

Only explicit history/repair reads expand evidence. The listener reads evaluation
summaries; publishing a watchlist never serializes the archive.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter, OrderedDict, deque
from collections.abc import Iterable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import ijson  # type: ignore[import-untyped]
import zstandard

BLOCK_BYTES = 2 * 1024 * 1024
HOT_BYTES = 10 * 1024 * 1024
ROW_BYTES = 8 * 1024
DISPLAY_FIELDS = {
    "bio",
    "name",
    "pseudonym",
    "profile_image",
    "profile_image_optimized",
    "icon",
    "title",
    "slug",
    "event_slug",
}
ROW_FIELDS = set(
    "candidate_id wallet sector_id first_discovered_at forward_status manual_paused first_qualified_at "
    "historical_eligible status stale data_status data_cutoff pending_cutoff metrics_ref last_successful_check_at "
    "last_successful_evaluation_ref valid_until next_review_at evaluation_ref last_attempt_at eligibility_effective_at "
    "monitor_effective_at archived policy_version metrics monitor window follow_up_refs".split()
)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class Archive:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.written_bytes = 0
        self._index: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._packs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("INVALID_ARCHIVE_REFERENCE")
        return self.root / "blocks" / digest[:2] / (digest + ".zst")

    def _block(self, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        path = self._path(digest)
        if path.exists():
            self._read_block(digest)
        else:
            compressed = zstandard.ZstdCompressor(level=6).compress(body)
            atomic_bytes(path, compressed)
            self.written_bytes += len(compressed)
        return digest

    def _read_block(self, digest: str) -> bytes:
        body = zstandard.ZstdDecompressor().decompress(self._path(digest).read_bytes(), max_output_size=BLOCK_BYTES)
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("CORRUPT_ARCHIVE_BLOCK")
        return body

    def put_stream(self, pieces: Iterable[bytes]) -> str:
        buffer = bytearray()
        blocks = []
        size = 0
        checksum = hashlib.sha256()
        for piece in pieces:
            checksum.update(piece)
            size += len(piece)
            offset = 0
            while offset < len(piece):
                take = min(BLOCK_BYTES - len(buffer), len(piece) - offset)
                buffer.extend(piece[offset : offset + take])
                offset += take
                if len(buffer) == BLOCK_BYTES:
                    blocks.append(self._block(bytes(buffer)))
                    buffer.clear()
        if buffer or not blocks:
            blocks.append(self._block(bytes(buffer)))
        if len(blocks) == 1:
            return "block:" + blocks[0]
        return "span:" + self._block(encoded({"blocks": blocks, "bytes": size, "sha256": checksum.hexdigest()}))

    def put(self, value: Any) -> str:
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        return self.put_stream(piece.encode() for piece in encoder.iterencode(value))

    def chunks(self, reference: str) -> Iterator[bytes]:
        kind, digest = reference.split(":", 1)
        if kind == "block":
            yield self._read_block(digest)
        elif kind == "span":
            manifest = json.loads(self._read_block(digest))
            checksum = hashlib.sha256()
            size = 0
            for block in manifest["blocks"]:
                part = self._read_block(block)
                checksum.update(part)
                size += len(part)
                yield part
            if size != manifest["bytes"] or checksum.hexdigest() != manifest["sha256"]:
                raise ValueError("CORRUPT_ARCHIVE_SPAN")
        else:
            raise ValueError("INVALID_ARCHIVE_REFERENCE")

    def get(self, reference: str) -> Any:
        return json.loads(b"".join(self.chunks(reference)))

    def events(self, reference: str) -> Iterator[tuple[str, str, Any]]:
        return iter(ijson.parse(_Chunks(self.chunks(reference))))

    def _index_path(self, kind: str, key: str) -> Path:
        if kind not in {"facts", "histories", "evaluations"} or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("INVALID_LEGACY_REFERENCE")
        return self.root / "index" / kind / (key[:2] + ".zst")

    def index(self, kind: str, key: str) -> dict[str, Any]:
        identity = (kind, key[:2])
        if identity not in self._index:
            path = self._index_path(kind, key)
            self._index[identity] = (
                json.loads(zstandard.ZstdDecompressor().decompress(path.read_bytes())) if path.exists() else {}
            )
            if len(self._index) > 8:
                self._index.popitem(last=False)
        self._index.move_to_end(identity)
        return self._index[identity]

    def locate(self, kind: str, key: str) -> Any:
        try:
            return self.index(kind, key)[key]
        except KeyError:
            raise ValueError(f"MISSING_ARCHIVE_REFERENCE:{kind}:{key}") from None

    def bind(self, kind: str, key: str, target: Any) -> None:
        index = self.index(kind, key)
        if key in index:
            if index[key] != target:
                raise ValueError("CONFLICTING_ARCHIVE_IDENTITY")
            return
        index[key] = target
        compressed = zstandard.ZstdCompressor(level=6).compress(encoded(index))
        atomic_bytes(self._index_path(kind, key), compressed)
        self.written_bytes += len(compressed)

    def fact(self, reference: str) -> Any:
        if ":" in reference:
            return self.get(reference)
        pack = self.locate("facts", reference)
        if pack not in self._packs:
            self._packs[pack] = self.get(pack)
            if len(self._packs) > 8:
                self._packs.popitem(last=False)
        self._packs.move_to_end(pack)
        return self._packs[pack][reference]


class _Chunks(io.RawIOBase):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks
        self.buffer = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("Unbounded archive read is not supported")
        while len(self.buffer) < size:
            part = next(self.chunks, None)
            if part is None:
                break
            self.buffer.extend(part)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


def archive_for(state: dict[str, Any]) -> Archive:
    return Archive(Path(state["archive"]["path"]))


def new_state(path: Path) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "archive": {"path": str(path.with_suffix(".archive").resolve())},
        "candidates": [],
        "records": {},
        "leaderboard_success": {},
        "tasks": {},
    }


def read_watchlist(path: Path) -> dict[str, Any]:
    if path.stat().st_size > HOT_BYTES:
        raise ValueError("WATCHLIST_STORAGE_MIGRATION_REQUIRED")
    state = json.loads(path.read_bytes())
    if state.get("schema_version") != 3 or any(k in state for k in ("facts", "histories", "evaluations")):
        raise ValueError("WATCHLIST_STORAGE_MIGRATION_REQUIRED")
    if not isinstance(state.get("archive"), dict) or not isinstance(state.get("records"), dict):
        raise ValueError("Unsupported watchlist schema")
    return dict(state)


def store_history(archive: Archive, history: dict[str, Any] | None, *, previous: str | None = None) -> str | None:
    if history is None:
        return None
    previous_fields = archive.get(previous).get("fields", {}) if previous and ":" in previous else {}

    def fields(data: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for key, value in data.items():
            if key == "local_history" and isinstance(value, dict):
                result[key] = {"kind": "nested", "fields": fields(value, before.get(key, {}).get("fields", {}))}
            elif isinstance(value, list) and key in {
                "closed_positions",
                "open_positions",
                "trades",
                "activity",
                "operations",
            }:
                result[key] = {
                    "kind": "rows",
                    "blocks": [
                        archive.put(
                            [{k: v for k, v in row.items() if k not in DISPLAY_FIELDS} for row in value[i : i + 500]]
                        )
                        for i in range(0, len(value), 500)
                    ],
                }
            elif isinstance(value, dict) and key in {"markets", "resolutions", "receipts"}:
                prior = before.get(key, {})
                members = archive.get(prior["members_ref"]) if prior.get("members_ref") else {}
                current = {}
                pending: dict[str, Any] = {}

                def flush() -> None:
                    if pending:
                        block = archive.put(pending)
                        for identity, row in pending.items():
                            current[identity] = [hashlib.sha256(encoded(row)).hexdigest(), block]
                        pending.clear()

                for identity, row in sorted(value.items()):
                    digest = hashlib.sha256(encoded(row)).hexdigest()
                    if identity in members and members[identity][0] == digest:
                        current[identity] = members[identity]
                    else:
                        pending[identity] = row
                        if len(pending) == 500:
                            flush()
                flush()
                result[key] = {"kind": "map", "members_ref": archive.put(current)}
            else:
                result[key] = {"kind": "value", "ref": archive.put(value)}
        return result

    return archive.put(
        {
            "wallet": history["wallet"],
            "as_of": history["as_of"],
            "conditions_ref": archive.put(sorted(history.get("markets", {}))),
            "fields": fields(history, previous_fields),
        }
    )


def history_header(archive: Archive, reference: str) -> dict[str, Any]:
    if ":" in reference:
        return dict(archive.get(reference))
    return dict(archive.get(archive.locate("histories", reference)["header"]))


def load_history(state: dict[str, Any], reference: str | None) -> dict[str, Any] | None:
    if reference is None:
        return None
    archive = archive_for(state)
    if ":" not in reference:
        manifest = archive.get(archive.locate("histories", reference)["detail"])
        # Resolve aliases bucket by bucket, then decode each batch once. Explicit repair only.
        grouped_refs: dict[str, set[str]] = {}

        def collect(value: Any) -> None:
            if isinstance(value, str):
                grouped_refs.setdefault(value[:2], set()).add(value)
            elif isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(manifest)
        packs: dict[str, set[str]] = {}
        for prefix, references in grouped_refs.items():
            index = archive.index("facts", prefix + "0" * 62)
            for member in references:
                if member not in index:
                    raise ValueError("MISSING_ARCHIVE_REFERENCE:fact:" + member)
                packs.setdefault(index[member], set()).add(member)
        values = {}
        for pack, members in packs.items():
            content = archive.get(pack)
            values.update({member: content[member] for member in members})

        # Old identities are retained by the migration index, never silently re-numbered.
        def restore(data: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in data.items():
                if key == "local_history":
                    result[key] = restore(value)
                elif isinstance(value, list):
                    result[key] = [values[r] for r in value]
                elif isinstance(value, dict):
                    result[key] = {k: values[r] for k, r in value.items()}
                else:
                    result[key] = values[value]
            return result

        return restore(manifest)
    manifest = archive.get(reference)

    def expand(fields: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, spec in fields.items():
            if spec["kind"] == "nested":
                result[key] = expand(spec["fields"])
            elif spec["kind"] == "rows":
                result[key] = [row for block in spec["blocks"] for row in archive.get(block)]
            elif spec["kind"] == "map":
                members = archive.get(spec["members_ref"])
                grouped: dict[str, list[str]] = {}
                for identity, (_, block) in members.items():
                    grouped.setdefault(block, []).append(identity)
                result[key] = {}
                for block, identities in grouped.items():
                    values = archive.get(block)
                    result[key].update({identity: values[identity] for identity in identities})
            else:
                result[key] = archive.get(spec["ref"])
        return result

    return expand(manifest["fields"])


def load_evaluation(state: dict[str, Any], reference: str | None, *, detail: bool = False) -> dict[str, Any]:
    if not reference:
        return {}
    archive = archive_for(state)
    return dict(archive.get(archive.locate("evaluations", reference)["detail" if detail else "summary"]))


def reason_summary(reasons: Iterable[str]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    examples: list[str] = []
    for reason in reasons:
        code = reason.split(":", 1)[0]
        counts[code[:120]] += 1
        if len(examples) < 3:
            examples.append(reason[:240])
    return {
        "unit": "reason_occurrence",
        "total": sum(counts.values()),
        "counts": dict(counts.most_common(20)),
        "other": sum(n for _, n in counts.most_common()[20:]),
        "examples": examples,
    }


def compact_profile(profile: dict[str, Any], detail_ref: str) -> dict[str, Any]:
    result = {key: value for key, value in profile.items() if key in ROW_FIELDS}
    summary = profile.get("reason_summary") or reason_summary(profile.get("reasons", []))
    result.update(reasons=summary["examples"], reason_summary=summary, details_ref=detail_ref)
    result["reviews"] = {key: {"status": value["status"]} for key, value in profile.get("reviews", {}).items()}
    return result


def store_evaluation(state: dict[str, Any], identity: str, evaluation: dict[str, Any]) -> None:
    archive = archive_for(state)
    if identity in archive.index("evaluations", identity):
        return
    detail = archive.put(evaluation)
    summary = {k: v for k, v in evaluation.items() if k not in {"profiles", "reconciliation", "trigger"}}
    summary["profiles"] = {s: compact_profile(p, detail) for s, p in evaluation["profiles"].items()}
    summary["reconciliation"] = {"complete": (evaluation.get("reconciliation") or {}).get("complete", False)}
    archive.bind("evaluations", identity, {"detail": detail, "summary": archive.put(summary)})


def repair_targets(state: dict[str, Any], identity: str | None) -> list[dict[str, Any]]:
    if not identity:
        return []
    archive = archive_for(state)
    detail = archive.locate("evaluations", identity)["detail"]
    return list(ijson.items(_Chunks(archive.chunks(detail)), "reconciliation.repair_targets.item", use_float=True))


def activity_summary(state: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    reference = candidate.get("summary_ref")
    return archive_for(state).fact(reference) if reference else {}


def compact_record(archive: Archive, row: dict[str, Any]) -> dict[str, Any]:
    # Evaluation owns detailed reviews. Only changes have their own append-only audit.
    result = compact_profile(row, row.get("details_ref") or row.get("evaluation_ref", ""))
    changes = row.get("changes", [])
    prior = row.get("changes_ref")
    if len(changes) > 5:
        prior = archive.put({"previous": prior, "changes": changes[:-5]})
    if prior:
        result["changes_ref"] = prior
    result["changes"] = [
        {
            k: (
                {n: v for n, v in value.items() if n in {"status", "historical_eligible", "stale", "monitor"}}
                if k in {"before", "after"}
                else value
            )
            for k, value in change.items()
        }
        for change in changes[-5:]
    ]
    if len(encoded(result)) > ROW_BYTES:
        raise ValueError("WATCHLIST_RECORD_BUDGET_EXCEEDED")
    return result


def write_watchlist(path: Path, state: dict[str, Any]) -> None:
    if any(key in state for key in ("facts", "histories", "evaluations")):
        raise ValueError("INLINE_HISTORY_FORBIDDEN")
    archive = archive_for(state)
    state["records"] = {key: compact_record(archive, row) for key, row in state["records"].items()}
    for task in state.get("tasks", {}).values():
        for key in list(task):
            if key not in {
                "queued_at",
                "last_attempt_at",
                "next_attempt_at",
                "history_ref",
                "outcome",
                "phase",
                "failures",
                "error",
                "summary_error",
                "window",
                "cursor",
                "completed_sectors",
            }:
                del task[key]
        if "window" in task:
            task["window"] = {
                key: value
                for key, value in task["window"].items()
                if key in {"start", "end", "index_source", "index_complete"}
            }
        for feed, cursor in task.get("cursor", {}).get("feeds", {}).items():
            task["cursor"]["feeds"][feed] = {
                key: value for key, value in cursor.items() if key in {"complete", "pages", "next_cursor", "error"}
            }
    body = encoded(state)
    if len(body) > HOT_BYTES:
        raise ValueError("WATCHLIST_SIZE_BUDGET_EXCEEDED")
    if not path.exists() or path.stat().st_size != len(body) or path.read_bytes() != body:
        atomic_bytes(path, body)


class _Preview:
    """Bounded preview while raw JSON is streamed, including giant nested evaluations."""

    def __init__(self) -> None:
        self.stack: list[dict[str, Any]] = []
        self.value: Any = None

    def event(self, event: str, value: Any) -> None:
        if event in {"start_map", "start_array"}:
            exact = bool(self.stack and (self.stack[-1]["exact"] or self.stack[-1]["key"] in ROW_FIELDS))
            self.stack.append(
                {
                    "exact": exact,
                    "value": {} if event == "start_map" else [] if exact else deque(maxlen=5),
                    "key": None,
                    "reasons": Counter(),
                    "examples": [],
                }
            )
        elif event == "map_key":
            self.stack[-1]["key"] = value
        elif event in {"end_map", "end_array"}:
            frame = self.stack.pop()
            result = frame["value"]
            if isinstance(result, deque):
                result = list(result)
            if frame["reasons"] and self.stack and self.stack[-1]["key"] == "reasons":
                counts = frame["reasons"]
                self.stack[-1]["value"]["reason_summary"] = {
                    "unit": "reason_occurrence",
                    "total": sum(counts.values()),
                    "counts": dict(counts.most_common(20)),
                    "other": sum(n for _, n in counts.most_common()[20:]),
                    "examples": frame["examples"],
                }
            self._add(result)
        else:
            self._add(float(value) if isinstance(value, Decimal) else value)

    def _add(self, value: Any) -> None:
        if not self.stack:
            self.value = value
            return
        frame = self.stack[-1]
        if isinstance(frame["value"], dict):
            if frame["exact"] or (
                len(frame["value"]) < 100 and frame["key"] not in {"events", "repair_targets", "differences"}
            ):
                frame["value"][frame["key"]] = value
        else:
            if isinstance(value, str) and len(self.stack) > 1 and self.stack[-2]["key"] == "reasons":
                frame["reasons"][value.split(":", 1)[0][:120]] += 1
                if len(frame["examples"]) < 3:
                    frame["examples"].append(value[:240])
            frame["value"].append(value if frame["exact"] or not isinstance(value, str) else value[:240])
        if frame["exact"] and len(encoded(frame["value"])) > 16 * 1024:
            raise ValueError("MIGRATION_SUMMARY_FIELD_TOO_LARGE")


def _value_stream(
    events: Iterator[tuple[str, str, Any]], first: tuple[str, str, Any], preview: _Preview | None = None
) -> Iterator[bytes]:
    """Re-encode tokens without converting JSON decimals to binary floating point."""
    stack: list[list[Any]] = []
    current = first
    while True:
        _, event, value = current
        if preview:
            preview.event(event, value)
        if event == "map_key":
            if stack[-1][1]:
                yield b","
            stack[-1][1] = True
            yield json.encoder.encode_basestring_ascii(value).encode() + b":"
        elif event in {"end_map", "end_array"}:
            yield b"}" if event == "end_map" else b"]"
            stack.pop()
        else:
            if stack and stack[-1][0] == "array":
                if stack[-1][1]:
                    yield b","
                stack[-1][1] = True
            if event in {"start_map", "start_array"}:
                yield b"{" if event == "start_map" else b"["
                stack.append(["map" if event == "start_map" else "array", False])
            else:
                if isinstance(value, str):
                    yield json.encoder.encode_basestring_ascii(value).encode()
                elif value is None:
                    yield b"null"
                elif isinstance(value, bool):
                    yield b"true" if value else b"false"
                else:
                    yield str(value).encode()
        if not stack:
            break
        current = next(events)


def _verified_store(archive: Archive, chunks: Iterable[bytes]) -> str:
    expected = hashlib.sha256()

    def hashed() -> Iterator[bytes]:
        for part in chunks:
            expected.update(part)
            yield part

    reference = archive.put_stream(hashed())
    actual = hashlib.sha256()
    for part in archive.chunks(reference):
        actual.update(part)
    if expected.digest() != actual.digest():
        raise ValueError("MIGRATION_VALUE_MISMATCH")
    return reference


def migrate_watchlist(source: Path, destination: Path) -> dict[str, Any]:
    """Explicit, resumable, isolated migration; source is never modified or deleted here."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination.exists():
        raise ValueError("Migration requires a new isolated output")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        source.with_suffix(source.suffix + ".lock").open("a") as source_lock,
        destination.with_suffix(destination.suffix + ".lock").open("a") as target_lock,
    ):
        fcntl.flock(source_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(target_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _migrate_locked(source, destination)


def _migration_fields(archive: Archive, reference: str) -> dict[str, Any]:
    events = archive.events(reference)
    next(events)
    result = {}
    while (token := next(events))[1] != "end_map":
        key = token[2]
        chunks = _value_stream(events, next(events))
        if key in ROW_FIELDS:
            body = bytearray()
            for chunk in chunks:
                body.extend(chunk)
                if len(body) > 16 * 1024:
                    raise ValueError("MIGRATION_SUMMARY_FIELD_TOO_LARGE:" + key)
            result[key] = json.loads(body)
        else:
            for _ in chunks:
                pass
    return result


def _migrate_locked(source: Path, destination: Path) -> dict[str, Any]:
    state = new_state(destination)
    archive = archive_for(state)
    revision = (source.stat().st_ino, source.stat().st_size, source.stat().st_mtime_ns)
    progress_path = archive.root / "migration.json"
    progress: dict[str, Any] = (
        json.loads(progress_path.read_bytes())
        if progress_path.exists()
        else {"revision": list(revision), "sections": {}}
    )
    if progress["revision"] != list(revision):
        raise ValueError("MIGRATION_SOURCE_CHANGED")
    sections = progress["sections"]
    counts: Counter[str] = Counter()
    with source.open("rb") as f:
        events = iter(ijson.parse(f))
        if next(events)[1] != "start_map":
            raise ValueError("Invalid watchlist")
        while (item := next(events))[1] != "end_map":
            section = item[2]
            first = next(events)
            if section in sections:
                # A restart rescans input, but completed immutable sections are not rewritten.
                depth = int(first[1] in {"start_map", "start_array"})
                while depth:
                    event = next(events)[1]
                    depth += int(event in {"start_map", "start_array"}) - int(event in {"end_map", "end_array"})
                continue
            if section not in {"facts", "histories", "evaluations", "records"}:
                sections[section] = _verified_store(archive, _value_stream(events, first))
            elif section == "facts":
                spool = archive.root / "spool"
                spool.mkdir(parents=True, exist_ok=True)
                pending: list[str] = []

                def packs() -> Iterator[bytes]:
                    yield b"{"
                    for number in range(500):
                        token = next(events)
                        if token[1] == "end_map":
                            return
                        key = token[2]
                        pending.append(key)
                        if number:
                            yield b","
                        yield encoded(key) + b":"
                        yield from _value_stream(events, next(events))
                    yield b"}"

                saved = progress.get("facts_checkpoint", {"count": 0, "packs": []})
                for _ in range(saved["count"]):
                    if next(events)[1] != "map_key":
                        raise ValueError("MIGRATION_CHECKPOINT_MISMATCH")
                    first_value = next(events)
                    depth = int(first_value[1] in {"start_map", "start_array"})
                    while depth:
                        event = next(events)[1]
                        depth += int(event in {"start_map", "start_array"}) - int(event in {"end_map", "end_array"})
                counts[section] = saved["count"]
                ended = False
                pack_refs = list(saved["packs"])
                while not ended:
                    pending.clear()

                    def closed_pack() -> Iterator[bytes]:
                        nonlocal ended
                        # packs omits its closing brace when the facts map ends.
                        yield from packs()
                        if len(pending) < 500:
                            ended = True
                            yield b"}"

                    ref = _verified_store(archive, closed_pack())
                    if pending:
                        pack_refs.append(ref)
                    grouped: dict[str, list[bytes]] = {}
                    for key in pending:
                        grouped.setdefault(key[:2], []).append(encoded([key, ref]) + b"\n")
                    for prefix, lines in grouped.items():
                        with (spool / (prefix + ".jsonl")).open("ab") as output:
                            output.writelines(lines)
                    counts[section] += len(pending)
                    if counts[section] % 100000 == 0:
                        for spool_file in spool.glob("*.jsonl"):
                            with spool_file.open("rb") as durable:
                                os.fsync(durable.fileno())
                        progress["facts_checkpoint"] = {"count": counts[section], "packs": pack_refs}
                        atomic_bytes(progress_path, encoded(progress))
                        print(json.dumps({"migrated_facts": counts[section]}), flush=True)
                for path in spool.glob("*.jsonl"):
                    index: dict[str, str] = {}
                    with path.open("rb") as index_entries:
                        for line in index_entries:
                            key, ref = json.loads(line)
                            if key in index and index[key] != ref:
                                raise ValueError("Conflicting resumed fact pack")
                            index[key] = ref
                    atomic_bytes(
                        archive._index_path("facts", key), zstandard.ZstdCompressor(level=6).compress(encoded(index))
                    )
                sections[section] = archive.put({"count": counts[section], "packs": pack_refs})
            else:
                entries = {}
                while (token := next(events))[1] != "end_map":
                    key = token[2]
                    preview = _Preview()
                    ref = _verified_store(
                        archive, _value_stream(events, next(events), preview if section != "histories" else None)
                    )
                    entries[key] = {"detail": ref}
                    if section in {"records", "evaluations"}:
                        entries[key]["preview"] = archive.put(preview.value)
                    counts[section] += 1
                sections[section] = archive.put(entries)
            atomic_bytes(progress_path, encoded(progress))
            print(json.dumps({"completed_section": section, "count": counts[section]}), flush=True)
    if archive.get(sections["schema_version"]) != 2:
        raise ValueError("Unsupported migration source schema")
    for section, reference in sections.items():
        if section not in {"facts", "histories", "evaluations", "records", "schema_version"}:
            state[section] = archive.get(reference)
    reference_spool = archive.root / "references"
    reference_spool.mkdir(exist_ok=True)
    reference_handles = {f"{i:02x}": (reference_spool / f"{i:02x}").open("w") for i in range(256)}
    for key, entry in archive.get(sections["histories"]).items():
        # Validate every member reference without loading any wallet's raw history.
        conditions = []
        header = {}
        for prefix, event, value in archive.events(entry["detail"]):
            if event == "string":
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("INVALID_HISTORY_MEMBER")
                reference_handles[value[:2]].write(value + "\n")
                if prefix in {"wallet", "as_of"}:
                    header[prefix] = archive.fact(value)
            if prefix == "markets" and event == "map_key":
                conditions.append(value)
        header["conditions_ref"] = archive.put(conditions)
        archive.bind("histories", key, {"detail": entry["detail"], "header": archive.put(header)})
    for handle in reference_handles.values():
        handle.close()
    for path in reference_spool.iterdir():
        index = archive.index("facts", path.name + "0" * 62)
        with path.open() as refs:
            for member_line in refs:
                if member_line.strip() not in index:
                    raise ValueError("MISSING_HISTORY_MEMBER:" + member_line.strip())
        path.unlink()
    reference_spool.rmdir()
    for key, entry in archive.get(sections["evaluations"]).items():
        evaluation = archive.get(entry["preview"])
        if evaluation.get("history_ref"):
            archive.locate("histories", evaluation["history_ref"])
        if evaluation.get("policy_ref"):
            archive.locate("facts", evaluation["policy_ref"])
        evaluation["profiles"] = {
            s: compact_profile(p, entry["detail"]) for s, p in evaluation.get("profiles", {}).items()
        }
        evaluation["reconciliation"] = {"complete": (evaluation.get("reconciliation") or {}).get("complete", False)}
        archive.bind("evaluations", key, {"detail": entry["detail"], "summary": archive.put(evaluation)})
    for key, entry in archive.get(sections["records"]).items():
        row = archive.get(entry["preview"])
        row.update(_migration_fields(archive, entry["detail"]))
        row["details_ref"] = entry["detail"]
        row["changes_ref"] = entry["detail"]
        for field in ("evaluation_ref", "last_successful_evaluation_ref", "metrics_ref"):
            if row.get(field):
                archive.locate("evaluations", row[field])
        state["records"][key] = compact_record(archive, row)
    for candidate in state["candidates"]:
        if candidate.get("summary_ref"):
            archive.fact(candidate["summary_ref"])
    for task in state.get("tasks", {}).values():
        if task.get("history_ref"):
            archive.locate("histories", task["history_ref"])
    if (source.stat().st_ino, source.stat().st_size, source.stat().st_mtime_ns) != revision:
        raise ValueError("MIGRATION_SOURCE_CHANGED")
    state["archive"]["migration_ref"] = archive.put({"sections": sections, "source_bytes": revision[1]})
    write_watchlist(destination, state)
    for path in (archive.root / "spool").glob("*.jsonl"):
        path.unlink()
    (archive.root / "spool").rmdir()
    progress_path.unlink(missing_ok=True)
    return {
        "source_bytes": revision[1],
        "hot_bytes": destination.stat().st_size,
        "candidates": len(state["candidates"]),
        "records": len(state["records"]),
        "evaluations": len(archive.get(sections["evaluations"])),
        "histories": len(archive.get(sections["histories"])),
        "facts": archive.get(sections["facts"])["count"],
    }
