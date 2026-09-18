"""`/sanctions` and `/column-mappings` - the two-phase upload and what it reads (Q1).

`POST /sanctions/upload` inspects and stores; `POST /sanctions/upload/{id}/commit`
ingests. Nothing between them is held in memory or trusted from the client: the
commit re-reads the stored bytes, so the analyst confirms a mapping against the
exact file that will be ingested.

The size limit is enforced twice. A declared `Content-Length` over the limit is
refused before the body is parsed at all; Starlette otherwise spools the whole
multipart body to a temporary file before the handler runs. The spooled file is
then read in bounded chunks, which catches a client that lied about its length,
without ever holding more than the limit in memory.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, status

from concordance.api import presenters, schemas
from concordance.api.deps import ActorDep, CurrentUser, SessionDep, SettingsDep
from concordance.api.errors import NotFoundError
from concordance.api.routers.common import Limit, Offset
from concordance.db.models import ColumnMapping
from concordance.db.repositories.sanctions import SanctionRepository
from concordance.errors import TooLargeError
from concordance.sanctions import service
from concordance.sanctions.canonical import CANONICAL_FIELDS

router = APIRouter(tags=["sanctions"])

#: Bytes read per chunk from the upload stream.
READ_CHUNK = 1024 * 1024


@router.post(
    "/sanctions/upload",
    response_model=schemas.InspectionOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "This exact file was uploaded before."},
               413: {"description": "The file is over the size limit."},
               422: {"description": "Not a readable workbook, or no data rows."}},
)
def upload(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
    file: Annotated[UploadFile, File(description="An .xlsx sanction or exclusion list.")],
    source_authority: Annotated[
        str | None, Form(max_length=100, description="Who published the file, if known.")
    ] = None,
) -> schemas.InspectionOut:
    """Phase one: store the file, profile its columns, propose a mapping. Ingests nothing."""
    _refuse_declared_oversize(request, settings.MAX_UPLOAD_BYTES)
    data = _read_bounded(file, settings.MAX_UPLOAD_BYTES)
    inspection = service.inspect_upload(
        session,
        settings,
        actor,
        filename=file.filename or "upload.xlsx",
        data=data,
        source_authority=source_authority,
    )
    session.commit()
    proposal = inspection.proposal
    return schemas.InspectionOut(
        file_id=inspection.file.id,
        filename=inspection.file.filename,
        sha256=inspection.file.sha256,
        rows=inspection.rows,
        status=inspection.file.status,
        columns=[schemas.ColumnProfileOut(**vars(c)) for c in inspection.columns],
        proposed_mapping=proposal.mapping,
        proposal_source=proposal.source,
        mapping_id=proposal.mapping_id,
        source_authority=inspection.file.source_authority,
        unmapped_required=proposal.unmapped_required,
        canonical_fields=list(CANONICAL_FIELDS),
    )


@router.post(
    "/sanctions/upload/{file_id}/commit",
    response_model=schemas.CommitOut,
    responses={409: {"description": "The file is not in the INSPECTED state."},
               422: {"description": "The mapping is incomplete or names missing columns."}},
)
def commit(
    file_id: uuid.UUID,
    body: schemas.CommitIn,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> schemas.CommitOut:
    """Phase two: apply the confirmed mapping and ingest the file's rows."""
    report = service.commit_upload(
        session,
        settings,
        actor,
        file_id,
        mapping=body.mapping,
        source_authority=body.source_authority,
        save_as_default=body.save_as_default,
        mapping_name=body.mapping_name,
    )
    session.commit()
    summary = report.summary()
    return schemas.CommitOut(
        file_id=report.file.id,
        status=report.file.status,
        mapping_id=report.mapping.id,
        source_authority=report.file.source_authority or "",
        accepted=summary["accepted"],
        new=summary["new"],
        replaced=summary["replaced"],
        unchanged=summary["unchanged"],
        inserted=summary["inserted"],
        rejected=summary["rejected"],
        warnings=summary["warnings"],
        rejected_rows=[schemas.RowIssueOut(**r) for r in summary["rejected_rows"]],
        warning_rows=[schemas.RowIssueOut(**w) for w in summary["warning_rows"]],
    )


@router.get("/sanctions/files", response_model=schemas.Page[schemas.SanctionFileOut])
def list_files(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    status_: Annotated[str | None, Query(alias="status")] = None,
) -> schemas.Page[schemas.SanctionFileOut]:
    """Upload history, newest first, with the mapping each file was read through."""
    page = SanctionRepository(session).list_files(status_, limit=limit, offset=offset)
    return schemas.Page[schemas.SanctionFileOut](
        items=[presenters.file_out(session, f) for f in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/sanctions/files/{file_id}", response_model=schemas.SanctionFileOut)
def get_file(file_id: uuid.UUID, session: SessionDep, _user: CurrentUser) -> schemas.SanctionFileOut:
    file = SanctionRepository(session).get_file(file_id)
    if file is None:
        raise NotFoundError(f"no uploaded file {file_id}")
    return presenters.file_out(session, file)


@router.get("/sanctions", response_model=schemas.Page[schemas.SanctionRecordOut])
def list_records(
    session: SessionDep,
    _user: CurrentUser,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Annotated[str | None, Query(max_length=100, description="Name, record key or NPI.")] = None,
    file_id: uuid.UUID | None = None,
    source_authority: Annotated[str | None, Query(max_length=100)] = None,
    state: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    sanction_type: Annotated[str | None, Query(max_length=100)] = None,
    is_organization: bool | None = None,
    include_history: Annotated[
        bool, Query(description="Include versions replaced by a later upload.")
    ] = False,
) -> schemas.Page[schemas.SanctionRecordOut]:
    page = SanctionRepository(session).search_records(
        q=q,
        file_id=file_id,
        source_authority=source_authority,
        state=state,
        sanction_type=sanction_type,
        is_organization=is_organization,
        include_history=include_history,
        limit=limit,
        offset=offset,
    )
    return schemas.Page[schemas.SanctionRecordOut](
        items=[presenters.record_out(r) for r in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/sanctions/facets", response_model=schemas.FacetsOut)
def facets(session: SessionDep, _user: CurrentUser) -> schemas.FacetsOut:
    """Distinct sanction types, source authorities and states, for filter menus.

    Declared before `/sanctions/{record_id}`: a path parameter matches first
    and would answer `facets` with a 422 for not being a UUID.
    """
    return schemas.FacetsOut(**SanctionRepository(session).facets())


@router.get("/sanctions/{record_id}", response_model=schemas.SanctionRecordDetailOut)
def get_record(
    record_id: uuid.UUID, session: SessionDep, _user: CurrentUser
) -> schemas.SanctionRecordDetailOut:
    """One record, with the original row as uploaded."""
    row = SanctionRepository(session).get_record_row(record_id)
    if row is None:
        raise NotFoundError(f"no sanction record {record_id}")
    return presenters.record_detail(row)


# --------------------------------------------------------------------------
# column mappings
# --------------------------------------------------------------------------


@router.get("/column-mappings", response_model=list[schemas.ColumnMappingOut], tags=["column-mappings"])
def list_mappings(
    session: SessionDep,
    _user: CurrentUser,
    source_authority: Annotated[str | None, Query(max_length=100)] = None,
) -> list[schemas.ColumnMappingOut]:
    rows = SanctionRepository(session).list_mappings(source_authority)
    return [schemas.ColumnMappingOut.model_validate(r) for r in rows]


@router.post(
    "/column-mappings",
    response_model=schemas.ColumnMappingOut,
    status_code=status.HTTP_201_CREATED,
    tags=["column-mappings"],
)
def create_mapping(
    body: schemas.ColumnMappingIn, session: SessionDep, actor: ActorDep
) -> schemas.ColumnMappingOut:
    row = service.create_mapping(
        session,
        actor,
        source_authority=body.source_authority.strip(),
        name=body.name.strip(),
        mapping=body.mapping,
        is_default=body.is_default,
    )
    session.commit()
    return schemas.ColumnMappingOut.model_validate(row)


@router.put(
    "/column-mappings/{mapping_id}",
    response_model=schemas.ColumnMappingOut,
    tags=["column-mappings"],
    responses={409: {"description": "The mapping has ingested files and cannot change."}},
)
def update_mapping(
    mapping_id: uuid.UUID, body: schemas.ColumnMappingIn, session: SessionDep, actor: ActorDep
) -> schemas.ColumnMappingOut:
    """Edit a mapping, or make it its authority's default.

    A mapping that has ingested a file may still be made (or unmade) the
    default, but its content is frozen: it is part of that file's provenance.
    """
    row = service.update_mapping(
        session,
        actor,
        mapping_id,
        name=body.name.strip(),
        source_authority=body.source_authority.strip(),
        mapping=body.mapping,
        is_default=body.is_default,
    )
    session.commit()
    return schemas.ColumnMappingOut.model_validate(row)


@router.post(
    "/column-mappings/{mapping_id}/default",
    response_model=schemas.ColumnMappingOut,
    tags=["column-mappings"],
)
def make_default(
    mapping_id: uuid.UUID, session: SessionDep, actor: ActorDep
) -> schemas.ColumnMappingOut:
    """Make this the mapping proposed for its source authority's next upload."""
    row = session.get(ColumnMapping, mapping_id)
    if row is None:
        raise NotFoundError(f"no column mapping {mapping_id}")
    service.set_default(session, actor, row)
    session.commit()
    return schemas.ColumnMappingOut.model_validate(row)


def _refuse_declared_oversize(request: Request, limit: int) -> None:
    declared = request.headers.get("content-length")
    # Multipart framing adds a few hundred bytes around the file itself.
    if declared and declared.isdigit() and int(declared) > limit + 64 * 1024:
        raise TooLargeError(
            f"the upload is over the {limit:,}-byte limit", details={"limit_bytes": limit}
        )


def _read_bounded(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := file.file.read(READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise TooLargeError(
                f"the file is over the {limit:,}-byte limit", details={"limit_bytes": limit}
            )
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["router"]
