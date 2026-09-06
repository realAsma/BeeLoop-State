"""The store: filing, time, validation, and the two files a work item lives in."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema

UTC = dt.timezone.utc
STAMP = "%Y-%m-%dT%H:%M:%SZ"

ROOT_BUCKET = "_root"
MAX_BUCKET_BYTES = 200  # one path component is capped at 255 everywhere
DIGEST_BYTES = 8  # of that budget, reserved for the cwd digest

# What state_update may write, and which of the two files it lands in. The
# agent never writes either directly and does not need to know which is which.
WORK_FILE_FIELDS = ("description", "current_status", "prior_actions",
                    "next_steps", "blockers", "artifacts", "final_learnings")
INDEX_ROW_FIELDS = ("completion", "short_description")
# Written field order; schema.json cannot enforce JSON object order.
INDEX_ROW_ORDER = ("cwd", "work_name", "short_description", "updated",
                   "completion", "work_state_path")
# Identity and location are set at initialize; changing them is an admin
# operation on the index, not something a work item does to itself mid-flight.
IMMUTABLE_FIELDS = ("work_name", "work_state_path", "cwd", "updated")

# Filing, not content, so this never leaves the store.
INTERNAL = ("work_state_path",)


class StoreError(RuntimeError):
    """Anything the caller did that the store refuses."""


class NotFound(StoreError):
    pass


class AlreadyExists(StoreError):
    pass


class Invalid(StoreError):
    """A record that does not satisfy schema.json."""


class UnsafePath(StoreError):
    """A work_state_path that would escape states/."""


# --------------------------------------------------------------------- filing


def slug(cwd: str) -> str:
    """Render a normalized cwd readably, then append its full-path digest.

        /home/ak/Bots/BeeBotBS  ->  home-ak-Bots-BeeBotBS-6f3a1c04

    The readable prefix is flattened and truncated, so the unconditional digest
    preserves collision resistance. It covers the full normalized cwd, not the
    prefix. A split UTF-8 sequence is dropped while decoding the byte slice.
    """
    readable = cwd.strip("/").replace("/", "-").lstrip("_-")
    readable = readable.encode()[:MAX_BUCKET_BYTES - DIGEST_BYTES - 1].decode("utf-8", "ignore")
    return f"{readable or ROOT_BUCKET}-{sha256(cwd.encode()).hexdigest()[:DIGEST_BYTES]}"


def work_state_path(work_name: str, cwd: str) -> str:
    return f"{slug(cwd)}/{work_name}.json"


def normalize_cwd(cwd: str) -> str:
    """Normalize the required cwd key half so equivalent spellings share identity."""
    if not cwd or not cwd.strip():
        raise StoreError("cwd is required: with work_name it is the key")
    return os.path.normpath(os.path.abspath(os.path.expanduser(cwd.strip()))).rstrip("/") or "/"


def resolve_in_store(states_dir: Path, relative: str) -> Path:
    """Keep even a hand-edited relative path contained in the store."""
    pure = PurePosixPath(relative)
    resolved = (states_dir / pure).resolve()
    # Structural (no empty path, no absolute, no "..") and positional (lands
    # under states/). Both, because the string can be innocent and still resolve
    # out through a symlinked bucket.
    if (not relative or pure.is_absolute() or ".." in pure.parts
            or states_dir.resolve() not in resolved.parents):
        raise UnsafePath(f"work_state_path must stay inside states/: {relative!r}")
    return resolved


# ----------------------------------------------------------------------- time

_RELATIVE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def now() -> str:
    return dt.datetime.now(UTC).strftime(STAMP)


def resolve_bound(value: str | None) -> str | None:
    """Resolve a duration like "7d" (s/m/h/d/w) or timestamp to stored UTC."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if match := _RELATIVE.match(value):
        span = int(match[1]) * _SECONDS[match[2].lower()]
        return (dt.datetime.now(UTC) - dt.timedelta(seconds=span)).strftime(STAMP)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StoreError(
            f"{value!r} is not a duration like '7d' or a timestamp like "
            f"'2026-08-21T12:00:00Z'"
        ) from None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).strftime(STAMP)


def _next_after(previous: str | None) -> str:
    """Return a strictly increasing update time so stale-token checks stay sound."""
    current = now()
    if previous and current <= previous:
        return (dt.datetime.strptime(previous, STAMP) + dt.timedelta(seconds=1)).strftime(STAMP)
    return current


# ------------------------------------------------------------------ the store


@dataclass(frozen=True)
class Filters:
    since: str | None = None
    until: str | None = None
    completion: str | None = None
    cwd: str | None = None
    limit: int = 20


class Store:
    """Validate every write without coercion; an unusable schema prevents loading."""

    def __init__(self, states_dir: Path | str):
        self.dir = Path(states_dir).resolve()
        self.index_path = self.dir / "index.jsonl"
        try:
            self.schema = json.loads((self.dir / "schema.json").read_text("utf-8"))
            jsonschema.Draft202012Validator.check_schema(self.schema)
            definitions = self.schema["$defs"]
            for name in ("index_row", "work_file"):
                definitions[name]
        except (OSError, ValueError, KeyError, jsonschema.SchemaError) as exc:
            raise StoreError(f"schema unusable, so every write is refused: {exc}") from exc
        self._checkers = {
            name: jsonschema.Draft202012Validator(
                {"$ref": f"#/$defs/{name}", "$defs": definitions})
            for name in ("index_row", "work_file")
        }

    def check(self, record: str, value: Any) -> None:
        errors = sorted(self._checkers[record].iter_errors(value), key=lambda e: list(e.path))
        if errors:
            raise Invalid("; ".join(_describe(e) for e in errors))

    # -------------------------------------------------------------- reading

    def read_index(self) -> list[dict[str, Any]]:
        """Read every index row newest first, breaking ties by work_name."""
        rows = []
        for number, line in enumerate(self.index_path.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise StoreError(f"index.jsonl:{number} is not valid JSON: {exc}") from exc
        rows.sort(key=_by_recency, reverse=True)
        return rows

    def get(self, work_name: str, cwd: str) -> dict[str, Any]:
        """Read a consistent, merged snapshot without conveying ownership."""
        cwd = normalize_cwd(cwd)
        with self._locked(shared=True):
            row = _row_in(self.read_index(), work_name, cwd)
            path = self._file(row)
            if not path.exists():
                raise NotFound(
                    f"{work_name!r} in {cwd!r} has an index row but no file; "
                    f"run `serve --validate`")
            return {**json.loads(path.read_text("utf-8")), **_public(row)}

    def search(self, filters: Filters) -> list[dict[str, Any]]:
        """Filter newest-first index rows mechanically by time, state, and place."""
        since, until = resolve_bound(filters.since), resolve_bound(filters.until)
        # The one place cwd stays optional: here it is a filter, not a key.
        cwd = normalize_cwd(filters.cwd) if filters.cwd and filters.cwd.strip() else None
        if filters.completion not in (None, "open", "done"):
            raise StoreError(f"completion must be 'open' or 'done', not {filters.completion!r}")
        if filters.limit < 0:
            raise StoreError("limit must be 0 (no cap) or a positive number of rows")

        rows = []
        for row in self.read_index():
            updated = row.get("updated", "")
            if since and updated < since:
                continue
            if until and updated >= until:
                continue
            if filters.completion and row.get("completion") != filters.completion:
                continue
            # Exact, not prefix: a prefix would silently match a nested checkout.
            if cwd and normalize_cwd(row.get("cwd")) != cwd:
                continue
            rows.append(_public(row))
        return rows[: filters.limit] if filters.limit else rows

    # -------------------------------------------------------------- writing

    def initialize(self, work_name: str, short_description: str,
                   cwd: str) -> dict[str, Any]:
        """Create the bucket, empty work file, and index row only."""
        cwd = normalize_cwd(cwd)
        row = {"cwd": cwd,
               "work_name": work_name,
               "short_description": short_description,
               "updated": now(),
               "completion": "open",
               "work_state_path": work_state_path(work_name, cwd)}
        self.check("index_row", row)

        with self._locked():
            rows = self.read_index()
            if any(_is(r, work_name, cwd) for r in rows):
                raise AlreadyExists(
                    f"work_name {work_name!r} is already in use in {cwd}; a name has to be "
                    f"unique within its directory, not across the store")
            path = self._file(row)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, "{}\n")
            self._write_index(rows + [row])
        return row

    def update(self, work_name: str, cwd: str, fields: dict[str, Any],
               expected: str | None = None) -> str:
        """Update content across both files and return the next freshness token.

        The recoverable write order is work file then index. `expected`, when
        given, is compared with the last `updated` inside the lock.
        """
        if unknown := [k for k in fields
                       if k not in WORK_FILE_FIELDS and k not in INDEX_ROW_FIELDS]:
            if immutable := [k for k in unknown if k in IMMUTABLE_FIELDS]:
                raise StoreError(f"{', '.join(immutable)}: set at initialize and not writable")
            raise StoreError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if not fields:
            raise StoreError("state_update needs at least one field to write")
        cwd = normalize_cwd(cwd)

        with self._locked():
            rows = self.read_index()
            current = _row_in(rows, work_name, cwd)
            index = rows.index(current)
            row = dict(current)
            if expected is not None and row.get("updated") != expected:
                raise StoreError(
                    f"{work_name!r} in {cwd!r} changed since you read it (you have {expected}, "
                    f"the store has {row.get('updated')}). Re-read with state_get and retry -- "
                    f"your write would have erased somebody else's.")
            path = self._file(row)

            content = json.loads(path.read_text("utf-8"))
            content.update({k: v for k, v in fields.items() if k in WORK_FILE_FIELDS})
            self.check("work_file", content)

            row.update({k: v for k, v in fields.items() if k in INDEX_ROW_FIELDS})
            row["updated"] = _next_after(row.get("updated"))
            self.check("index_row", row)

            _write(path, json.dumps(content, indent=2, sort_keys=True) + "\n")
            rows[index] = row
            self._write_index(rows)
        return row["updated"]

    # ------------------------------------------------------------ validating

    def validate(self) -> list[str]:
        """Check existing data and cross-record invariants."""
        problems, seen, filed, known = [], set(), set(), set()
        for row in self.read_index():
            name = row.get("work_name", "<unnamed row>")
            try:
                self.check("index_row", row)
            except Invalid as exc:
                problems.append(f"index row {name}: {exc}")
                continue
            # A duplicate key silently makes one record unreachable.
            where = (normalize_cwd(row["cwd"]), name)
            if where in seen:
                problems.append(f"index row {name}: (cwd, work_name) is not unique")
            seen.add(where)
            # A duplicate path lets one record overwrite another.
            if row["work_state_path"] in filed:
                problems.append(f"index row {name}: work_state_path is not unique")
            filed.add(row["work_state_path"])
            try:
                path = self._file(row)
                known.add(path)
                self.check("work_file", json.loads(path.read_text("utf-8")))
            except (StoreError, OSError, ValueError) as exc:
                problems.append(f"work file {name}: {exc}")

        problems += [f"orphan work file with no index row: {p}"
                     for p in sorted(self.dir.glob("*/*.json")) if p.resolve() not in known]
        return problems

    # ---------------------------------------------------------------- private

    def _file(self, row: dict[str, Any]) -> Path:
        """Resolve a possibly hand-edited row's path safely inside the store."""
        return resolve_in_store(self.dir, row["work_state_path"])

    @contextmanager
    def _locked(self, shared: bool = False):
        """Coordinate readers and writers around a consistent store snapshot.

        Use "a+": NFS LOCK_SH requires a readable descriptor, and this mode
        does not truncate the lock file.
        """
        with open(self.dir / ".lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_index(self, rows: list[dict[str, Any]]) -> None:
        """Write oldest first, reversing the same total order used for reads."""
        _write(self.index_path, "".join(
            json.dumps(_ordered(r), ensure_ascii=False) + "\n"
            for r in sorted(rows, key=_by_recency)))


def _by_recency(row: dict[str, Any]) -> tuple[str, str]:
    """Order deterministically, including rows updated in the same second."""
    return row.get("updated", ""), row.get("work_name", "")


def _ordered(row: dict[str, Any]) -> dict[str, Any]:
    """Put known fields first without dropping unknown fields."""
    ordered = {key: row[key] for key in INDEX_ROW_ORDER if key in row}
    return ordered | {k: v for k, v in row.items() if k not in ordered}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in INTERNAL}


def _is(row: dict[str, Any], work_name: str, cwd: str) -> bool:
    """Match the sole identity; cwd must be normalized and missing cwd never matches."""
    return (row.get("work_name") == work_name and bool(row.get("cwd"))
            and normalize_cwd(row["cwd"]) == cwd)


def _row_in(rows: list[dict[str, Any]], work_name: str, cwd: str) -> dict[str, Any]:
    """Find both key halves, naming both on a miss."""
    for row in rows:
        if _is(row, work_name, cwd):
            return row
    raise NotFound(f"no work named {work_name!r} in {cwd!r}")


def _write(path: Path, body: str) -> None:
    """Atomically replace one file using a same-directory, fsynced temp file."""
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False)
    try:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise


_SIZED = {
    "maxLength": ("is too long", ">", "characters"),
    "minLength": ("is too short", "<", "characters"),
    "maxItems": ("has too many entries", ">", "entries"),
    "minItems": ("has too few entries", "<", "entries"),
}


def _describe(error: jsonschema.ValidationError) -> str:
    """Name the field, size rule, actual count, and allowed count."""
    where = ".".join(str(part) for part in error.absolute_path)
    if sized := _SIZED.get(error.validator):
        phrase, comparison, unit = sized
        actual, limit = len(error.instance), error.validator_value
        message = f"{phrase} ({actual:,} {comparison} {limit:,} {unit})"
        return f"{where} {message}" if where else message
    message = error.message
    return f"{where}: {message}" if where else message
