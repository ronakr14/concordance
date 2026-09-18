"""The two-phase upload (PLAN 11.1), and the mappings it reads and writes.

**Inspect** stores the bytes, profiles the columns and proposes a mapping. It
writes a `sanction_files` row in `INSPECTED` and not one `sanction_records` row:
a file whose columns nobody has confirmed is not data yet.

**Commit** re-reads the stored bytes - not anything the client sends back -
applies the confirmed mapping, and ingests. Re-reading is deliberate: the
mapping is the only thing the client contributes at this step, so there is no
way for "what was inspected" and "what was ingested" to be two different files.

**Versioning on commit.** Each accepted row is compared with the current
version of the same record - same authority, same key:

- no current version: inserted as new;
- a current version with identical content: left alone, counted `unchanged`;
- a current version that differs: the new row is inserted and the old one
  marked not current, pointing at its replacement.

Nothing is edited in place. The old row is what earlier runs were decided
against, and replay recomputes their snapshot hash over it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.config import Settings
from concordance.db.enums import SanctionFileStatus
from concordance.db.loader import SANCTION_COLUMNS, as_date, copy_rows, normalized_columns
from concordance.db.models import ColumnMapping, SanctionFile, SanctionRecord
from concordance.db.repositories.sanctions import SanctionRepository
from concordance.domain import SanctionRecord as DomainSanction
from concordance.errors import ConflictError, InvalidError, NotFoundError, TooLargeError
from concordance.logging_setup import get_logger
from concordance.matching.normalization import normalize_sanction
from concordance.protocols import StorageBackend
from concordance.sanctions import mapping as mappings
from concordance.sanctions.canonical import CANONICAL_FIELDS, CONTENT_FIELDS
from concordance.sanctions.ingest import ParsedRow, ParseResult, parse_rows
from concordance.sanctions.workbook import Sheet, WorkbookError, cell_text, read_workbook

log = get_logger("sanctions.service")

#: Keys per `IN (...)` when looking up current versions. Bounded so a 100k-row
#: file is a hundred short statements rather than one enormous one.
LOOKUP_BATCH = 1_000

#: Samples shown per column in the inspection.
SAMPLES = 3


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def storage_for(settings: Settings) -> StorageBackend:
    if settings.STORAGE_BACKEND == "s3":
        from concordance.storage.s3 import S3Storage

        return S3Storage(bucket="concordance")
    from concordance.storage.local import LocalStorage

    return LocalStorage(settings.STORAGE_LOCAL_PATH)


def _storage_key(sha256: str, filename: str) -> str:
    # Content-addressed. Two uploads of the same bytes are the same object,
    # and a filename with a path in it cannot choose where the blob lands.
    suffix = ".xlsx" if filename.lower().endswith(".xlsx") else ".bin"
    return f"sanctions/{sha256[:2]}/{sha256}{suffix}"


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


@dataclass
class ColumnProfile:
    name: str
    non_empty: int
    samples: list[str]
    proposed_field: str | None
    confidence: float


@dataclass
class Inspection:
    file: SanctionFile
    rows: int
    columns: list[ColumnProfile]
    proposal: mappings.Proposal


def inspect_upload(
    session: Session,
    settings: Settings,
    actor: Actor,
    *,
    filename: str,
    data: bytes,
    source_authority: str | None = None,
) -> Inspection:
    """Phase one: store, profile, propose. Ingests nothing."""
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise TooLargeError(
            f"the file is {len(data):,} bytes; the limit is {settings.MAX_UPLOAD_BYTES:,}",
            details={"limit_bytes": settings.MAX_UPLOAD_BYTES},
        )
    sha256 = hashlib.sha256(data).hexdigest()
    repo = SanctionRepository(session)
    existing = repo.file_by_hash(sha256)
    if existing is not None:
        raise _duplicate(existing)

    sheet = _read(data, settings)
    authority = (source_authority or "").strip() or None
    proposal = propose_for(session, sheet.headers, authority)

    uri = storage_for(settings).put(_storage_key(sha256, filename), data)
    file = SanctionFile(
        filename=_safe_filename(filename),
        storage_uri=uri,
        sha256=sha256,
        uploaded_by=actor.user_id,
        row_count=len(sheet.rows),
        status=str(SanctionFileStatus.INSPECTED),
        source_authority=proposal.source_authority or authority,
    )
    session.add(file)
    try:
        session.flush()
    except IntegrityError as exc:
        # Two uploads of the same bytes raced past the lookup above. The
        # unique index decided; report it the same way.
        session.rollback()
        raced = SanctionRepository(session).file_by_hash(sha256)
        if raced is not None:
            raise _duplicate(raced) from exc
        raise

    audit.record(
        session,
        actor,
        "sanction_file.inspected",
        entity_type="sanction_file",
        entity_id=file.id,
        after={
            "filename": file.filename,
            "sha256": sha256,
            "rows": len(sheet.rows),
            "columns": len(sheet.headers),
            "proposal_source": proposal.source,
            "source_authority": file.source_authority,
        },
    )
    log.info("sanctions.inspected", file_id=str(file.id), rows=len(sheet.rows))
    return Inspection(file=file, rows=len(sheet.rows), columns=_profile(sheet, proposal), proposal=proposal)


def propose_for(session: Session, headers: list[str], authority: str | None) -> mappings.Proposal:
    """A stored mapping when one fits this file, the heuristic otherwise."""
    repo = SanctionRepository(session)
    candidates: list[ColumnMapping] = []
    if authority:
        default = repo.default_mapping(authority)
        if default is not None:
            candidates.append(default)
    # Recognise the source from its headers alone: a stored mapping whose every
    # column is present in this file. Defaults first, then the mapping that
    # explains the most columns.
    stored = sorted(
        repo.list_mappings(authority) if authority else repo.list_mappings(),
        key=lambda m: (not m.is_default, -len(m.mapping or {})),
    )
    candidates.extend(stored)
    for candidate in candidates:
        reused = mappings.reuse(dict(candidate.mapping or {}), headers)
        if reused is not None:
            return mappings.Proposal(
                mapping=reused,
                confidence=dict.fromkeys(reused, 1.0),
                source="stored",
                mapping_id=candidate.id,
                source_authority=candidate.source_authority,
                unmapped_required=mappings.missing_required(reused),
            )
    proposal = mappings.propose(headers)
    proposal.source_authority = authority
    return proposal


def _profile(sheet: Sheet, proposal: mappings.Proposal) -> list[ColumnProfile]:
    out: list[ColumnProfile] = []
    for index, header in enumerate(sheet.headers):
        values = [
            cell_text(cells[index])
            for _, cells in sheet.rows
            if index < len(cells) and cells[index] is not None
        ]
        samples = list(dict.fromkeys(v for v in values if v))[:SAMPLES]
        field_name, confidence = proposal.field_for(header)
        out.append(ColumnProfile(header, len(values), samples, field_name, confidence))
    return out


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------


@dataclass
class CommitReport:
    file: SanctionFile
    mapping: ColumnMapping
    parsed: ParseResult
    new: int = 0
    replaced: int = 0
    unchanged: int = 0
    replaced_ids: list[str] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return self.new + self.replaced

    def summary(self) -> dict[str, Any]:
        return {
            **self.parsed.summary(),
            "new": self.new,
            "replaced": self.replaced,
            "unchanged": self.unchanged,
            "inserted": self.inserted,
        }


def commit_upload(
    session: Session,
    settings: Settings,
    actor: Actor,
    file_id: uuid.UUID,
    *,
    mapping: dict[str, str],
    source_authority: str | None = None,
    save_as_default: bool = False,
    mapping_name: str | None = None,
) -> CommitReport:
    """Phase two: apply a confirmed mapping and ingest."""
    # Locked, so two commits of one file cannot both see `INSPECTED`.
    file = session.get(SanctionFile, file_id, with_for_update=True)
    if file is None:
        raise NotFoundError(f"no uploaded file {file_id}")
    if file.status != SanctionFileStatus.INSPECTED:
        raise ConflictError(
            f"file {file_id} is {file.status}; only an inspected file can be committed",
            code="file_not_inspected",
            details={"status": file.status},
        )

    authority = (source_authority or file.source_authority or "").strip()
    if not authority:
        raise InvalidError(
            "name the source authority this file came from",
            code="invalid_mapping",
            details={"source_authority": "required - a record's identity is its authority and key"},
        )

    sheet = _read(storage_for(settings).get(file.storage_uri), settings)
    errors = mappings.validate(mapping, sheet.headers)
    if errors:
        raise InvalidError(
            "the mapping cannot be applied to this file", code="invalid_mapping", details=errors
        )

    parsed = parse_rows(sheet, mapping, source_authority=authority)
    if not parsed.rows:
        raise InvalidError(
            "no row in the file can be ingested",
            code="no_usable_rows",
            details={"rejected_rows": [r.as_dict() for r in parsed.rejected[:50]]},
        )

    mapping_row = _resolve_mapping(
        session, actor, authority, mapping, save_as_default=save_as_default, name=mapping_name
    )
    report = CommitReport(file=file, mapping=mapping_row, parsed=parsed)
    _write_records(session, file, authority, parsed.rows, report)

    before = {"status": file.status}
    file.status = str(SanctionFileStatus.COMMITTED)
    file.mapping_id = mapping_row.id
    file.source_authority = authority
    file.notes = json.dumps(report.summary(), default=str)
    audit.record(
        session,
        actor,
        "sanction_file.committed",
        entity_type="sanction_file",
        entity_id=file.id,
        before=before,
        after={
            "status": file.status,
            "mapping_id": mapping_row.id,
            "source_authority": authority,
            **{k: v for k, v in report.summary().items() if not k.endswith("_rows")},
        },
    )
    log.info("sanctions.committed", file_id=str(file.id), **{
        k: v for k, v in report.summary().items() if isinstance(v, int)
    })
    return report


def _write_records(
    session: Session,
    file: SanctionFile,
    authority: str,
    rows: list[ParsedRow],
    report: CommitReport,
) -> None:
    current = _current_versions(session, authority, [r.record_id for r in rows])
    to_insert: list[tuple[ParsedRow, SanctionRecord | None]] = []
    for row in rows:
        old = current.get(row.record_id)
        if old is not None and _same_content(old, row.values):
            report.unchanged += 1
            continue
        to_insert.append((row, old))
    if not to_insert:
        return

    now = datetime.now(UTC)
    replaced = [old for _, old in to_insert if old is not None]
    if replaced:
        # Retire the old versions first: the partial unique index admits one
        # current version per identity, and the new rows arrive current.
        session.execute(
            update(SanctionRecord)
            .where(SanctionRecord.id.in_([old.id for old in replaced]))
            .values(is_current=False, replaced_at=now)
        )

    highest = session.scalar(select(func.coalesce(func.max(SanctionRecord.ordinal), -1)))
    start = int(highest if highest is not None else -1) + 1
    session.flush()
    records: list[tuple[Any, ...]] = []
    links: list[dict[str, Any]] = []
    for offset, (row, old) in enumerate(to_insert):
        new_id = uuid.uuid4()
        records.append(_copy_row(new_id, file.id, start + offset, row))
        if old is None:
            report.new += 1
        else:
            report.replaced += 1
            links.append({"id": old.id, "replaced_by": new_id})
            report.replaced_ids.append(str(old.id))

    # COPY on the session's own connection, so the insert is inside the same
    # transaction as the file's status change and its audit row.
    cursor = session.connection().connection.cursor()
    try:
        copy_rows(cursor, "sanction_records", SANCTION_COLUMNS, records)
    finally:
        cursor.close()
    if links:
        session.execute(update(SanctionRecord), links)


def _current_versions(
    session: Session, authority: str, keys: list[str]
) -> dict[str, SanctionRecord]:
    found: dict[str, SanctionRecord] = {}
    for batch in _batches(keys, LOOKUP_BATCH):
        stmt = select(SanctionRecord).where(
            SanctionRecord.is_current.is_(True),
            func.coalesce(SanctionRecord.source_authority, "") == authority,
            SanctionRecord.record_id.in_(batch),
        )
        for row in session.scalars(stmt):
            found[row.record_id] = row
    return found


def _same_content(old: SanctionRecord, values: dict[str, Any]) -> bool:
    for name in CONTENT_FIELDS:
        then = old.dob_raw if name == "dob" else getattr(old, name)
        if _comparable(then) != _comparable(values.get(name)):
            return False
    return True


def _comparable(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (bool, date)):
        return value
    return " ".join(str(value).split())


def _copy_row(row_id: uuid.UUID, file_id: uuid.UUID, ordinal: int, row: ParsedRow) -> tuple[Any, ...]:
    v = row.values
    domain = DomainSanction(
        record_id=row.record_id,
        **{k: v.get(k) for k in CANONICAL_FIELDS if k != "record_id"},
        is_organization=bool(v.get("is_organization")),
        source_authority=v.get("source_authority"),
        raw=row.raw,
    )
    name_norm, sorted_norm, phonetic, addr, zip5, trigram = normalized_columns(
        normalize_sanction(domain)
    )
    by_column: dict[str, Any] = {
        "id": row_id,
        "record_id": row.record_id,
        "file_id": file_id,
        "source_authority": domain.source_authority,
        "npi": domain.npi,
        "first_name": domain.first_name,
        "middle_name": domain.middle_name,
        "last_name": domain.last_name,
        "suffix": domain.suffix,
        "dob": as_date(domain.dob),
        "address_line1": domain.address_line1,
        "address_line2": domain.address_line2,
        "city": domain.city,
        "state": domain.state,
        "zip": domain.zip,
        "license_number": domain.license_number,
        "license_state": domain.license_state,
        "specialty": domain.specialty,
        "organization_name": domain.organization_name,
        "dba_name": domain.dba_name,
        "ein": domain.ein,
        "is_organization": domain.is_organization,
        "dob_raw": domain.dob,
        "sanction_type": domain.sanction_type,
        "exclusion_date": domain.exclusion_date,
        "reinstatement_date": domain.reinstatement_date,
        "raw": json.dumps(row.raw, default=str),
        "name_norm": name_norm,
        "name_sorted_norm": sorted_norm,
        "name_phonetic": phonetic,
        "addr_norm": addr,
        "zip5": zip5,
        "trigram_key": trigram,
        "ordinal": ordinal,
    }
    return tuple(by_column[c] for c in SANCTION_COLUMNS)


# --------------------------------------------------------------------------
# mappings
# --------------------------------------------------------------------------


def _resolve_mapping(
    session: Session,
    actor: Actor,
    authority: str,
    mapping: dict[str, str],
    *,
    save_as_default: bool,
    name: str | None,
) -> ColumnMapping:
    """The stored mapping this commit used - an existing one, or a new one.

    Every committed file points at a mapping row, so the file's interpretation
    is replayable. An identical mapping already stored for the authority is
    reused rather than duplicated; anything else is saved. The first mapping
    saved for an authority becomes its default, because the point of storing
    it is that the next upload from that source needs no mapping step.
    """
    repo = SanctionRepository(session)
    wanted = dict(sorted(mapping.items()))
    for existing in repo.list_mappings(authority):
        if dict(sorted((existing.mapping or {}).items())) == wanted:
            if save_as_default and not existing.is_default:
                set_default(session, actor, existing)
            return existing

    has_default = repo.default_mapping(authority) is not None
    row = create_mapping(
        session,
        actor,
        source_authority=authority,
        name=name or _auto_name(session, authority),
        mapping=wanted,
        is_default=save_as_default or not has_default,
    )
    return row


def create_mapping(
    session: Session,
    actor: Actor,
    *,
    source_authority: str,
    name: str,
    mapping: dict[str, str],
    is_default: bool = False,
) -> ColumnMapping:
    _check_mapping_fields(mapping)
    if _name_taken(session, source_authority, name):
        raise ConflictError(
            f"a mapping named {name!r} already exists for {source_authority}",
            code="mapping_name_taken",
            details={"name": "choose another name"},
        )
    row = ColumnMapping(
        source_authority=source_authority,
        name=name,
        mapping=mapping,
        is_default=False,
        created_by=actor.user_id,
    )
    session.add(row)
    session.flush()
    if is_default:
        set_default(session, actor, row, audit_it=False)
    audit.record(
        session,
        actor,
        "column_mapping.created",
        entity_type="column_mapping",
        entity_id=row.id,
        after={"source_authority": source_authority, "name": name, "mapping": mapping,
               "is_default": row.is_default},
    )
    return row


def update_mapping(
    session: Session,
    actor: Actor,
    mapping_id: uuid.UUID,
    *,
    name: str,
    source_authority: str,
    mapping: dict[str, str],
    is_default: bool,
) -> ColumnMapping:
    """Edit a mapping no committed file has used yet.

    Once a file has been ingested through a mapping, that mapping is part of
    the file's provenance: editing it would change, after the fact, what the
    record says about how the file was read. A changed layout gets a new
    mapping instead.
    """
    row = session.get(ColumnMapping, mapping_id, with_for_update=True)
    if row is None:
        raise NotFoundError(f"no column mapping {mapping_id}")
    _check_mapping_fields(mapping)
    used = session.scalar(
        select(func.count())
        .select_from(SanctionFile)
        .where(
            SanctionFile.mapping_id == mapping_id,
            SanctionFile.status == SanctionFileStatus.COMMITTED,
        )
    )
    content_changes = (
        dict(row.mapping or {}) != mapping
        or row.source_authority != source_authority
        or row.name != name
    )
    if used and content_changes:
        raise ConflictError(
            f"mapping {row.name!r} was used to ingest {used} file(s) and cannot be edited; "
            "create a new mapping for the new layout",
            code="mapping_in_use",
            details={"files": int(used)},
        )
    if (row.source_authority, row.name) != (source_authority, name) and _name_taken(
        session, source_authority, name
    ):
        raise ConflictError(
            f"a mapping named {name!r} already exists for {source_authority}",
            code="mapping_name_taken",
        )

    before = {"name": row.name, "source_authority": row.source_authority,
              "mapping": row.mapping, "is_default": row.is_default}
    row.name = name
    row.source_authority = source_authority
    row.mapping = mapping
    if is_default and not row.is_default:
        set_default(session, actor, row, audit_it=False)
    elif not is_default:
        row.is_default = False
    audit.record(
        session,
        actor,
        "column_mapping.updated",
        entity_type="column_mapping",
        entity_id=row.id,
        before=before,
        after={"name": name, "source_authority": source_authority, "mapping": mapping,
               "is_default": row.is_default},
    )
    return row


def set_default(
    session: Session, actor: Actor, row: ColumnMapping, *, audit_it: bool = True
) -> None:
    """Make `row` the default for its authority, and nothing else the default."""
    session.execute(
        update(ColumnMapping)
        .where(
            ColumnMapping.source_authority == row.source_authority,
            ColumnMapping.id != row.id,
            ColumnMapping.is_default.is_(True),
        )
        .values(is_default=False)
    )
    row.is_default = True
    if audit_it:
        audit.record(
            session,
            actor,
            "column_mapping.default_set",
            entity_type="column_mapping",
            entity_id=row.id,
            after={"source_authority": row.source_authority, "name": row.name},
        )


def _check_mapping_fields(mapping: dict[str, str]) -> None:
    errors = {
        f"mapping.{name}": "not a canonical field"
        for name in mapping
        if name not in CANONICAL_FIELDS
    }
    errors.update(
        {
            f"mapping.{name}": "map it to a column header"
            for name, header in mapping.items()
            if not isinstance(header, str) or not header.strip()
        }
    )
    for name in mappings.missing_required(mapping):
        errors[f"mapping.{name}"] = "map last_name or organization_name - a record needs a name"
    if errors:
        raise InvalidError("the mapping is not valid", code="invalid_mapping", details=errors)


def _name_taken(session: Session, authority: str, name: str) -> bool:
    return (
        session.scalar(
            select(ColumnMapping.id).where(
                ColumnMapping.source_authority == authority, ColumnMapping.name == name
            )
        )
        is not None
    )


def _auto_name(session: Session, authority: str) -> str:
    base = f"{authority} {datetime.now(UTC).date().isoformat()}"
    name, n = base, 2
    while _name_taken(session, authority, name):
        name, n = f"{base} ({n})", n + 1
    return name[:100]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read(data: bytes, settings: Settings) -> Sheet:
    try:
        sheet = read_workbook(data, max_rows=settings.MAX_UPLOAD_ROWS)
    except WorkbookError as exc:
        code = "too_many_rows" if "more than" in str(exc) else "invalid_workbook"
        raise InvalidError(str(exc), code=code) from exc
    if not sheet.rows:
        raise InvalidError("the workbook has a header row but no data rows", code="empty_workbook")
    return sheet


def _duplicate(existing: SanctionFile) -> ConflictError:
    return ConflictError(
        "this exact file has already been uploaded",
        code="duplicate_file",
        details={"file_id": str(existing.id), "status": existing.status,
                 "uploaded_at": existing.uploaded_at.isoformat()},
    )


def _safe_filename(filename: str) -> str:
    """The name as displayed: no directory part, bounded length."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip() or "upload.xlsx"
    return base[:255]


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


__all__ = [
    "ColumnProfile",
    "CommitReport",
    "Inspection",
    "commit_upload",
    "create_mapping",
    "inspect_upload",
    "propose_for",
    "set_default",
    "storage_for",
    "update_mapping",
]
