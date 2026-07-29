"""FastAPI application factory.

The OpenAPI schema this app emits is the source of the generated frontend and
`deviceagent` clients — clients are never hand-written. That is what makes the
repo splits safe, so operation ids must stay stable and readable.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app import __version__
from app.api.routes import (
    container_types,
    documents,
    enrichment,
    extraction,
    facets,
    intake,
    labels,
    location_tags,
    locations,
    parts,
    projects,
    provisioning,
    requirements,
    resolve,
    scan,
    search,
    stock,
    system,
)
from app.config import get_settings


def _operation_id(route: APIRoute) -> str:
    """Use the handler's function name as the operation id.

    FastAPI's default appends the path and method, producing generated client
    methods like `health_api_system_health_get`. The function name alone gives
    `health`, which is what a hand-written client would have been called.
    """
    return route.name


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="Almagest",
        version=__version__,
        summary="Self-hosted electronic-component inventory.",
        generate_unique_id_function=_operation_id,
    )

    app.include_router(system.router)
    app.include_router(search.router)
    app.include_router(resolve.router)
    app.include_router(scan.router)
    # Next to scan because it is the same workflow: a payload the resolver
    # returned is parked here, and the desk pass reads it back.
    app.include_router(intake.router)
    app.include_router(parts.router)
    # The document store, plus its own `/api/parts/{id}/documents` routes — kept
    # in the documents module because what they return belongs to the store rather
    # than to the part, the same split provisioning makes for locations.
    app.include_router(documents.router)
    app.include_router(documents.parts_router)
    # The extraction work queue and submit door (ADR 0005), plus the per-document
    # text read. The API owns the queue and `datasheet_fts`; the worker that parses
    # PDFs is a separate process and a separate image, and nothing included here
    # imports a PDF library.
    app.include_router(extraction.router)
    app.include_router(extraction.documents_router)
    app.include_router(container_types.router)
    # `/api/container-types/{id}/documents` — a type's own photo — rides a second
    # router with the same prefix, in `documents` for the same reason
    # `documents.parts_router` is: what it returns belongs to the store.
    app.include_router(documents.container_types_router)
    app.include_router(locations.router)
    # `/api/locations/{id}/documents` — one container's own photo, overriding
    # its type's — same split.
    app.include_router(documents.locations_router)
    # The provisioning walk's own `/api/locations/{id}/...` routes ride a second
    # router with the same prefix, kept in the provisioning module because
    # everything they return belongs to the walk rather than to the tree.
    app.include_router(provisioning.locations_router)
    app.include_router(provisioning.router)
    app.include_router(provisioning.verification_router)
    app.include_router(location_tags.router)
    app.include_router(labels.router)
    app.include_router(stock.router)
    app.include_router(facets.router)
    app.include_router(facets.categories_router)
    app.include_router(projects.router)
    app.include_router(projects.builds_router)
    app.include_router(requirements.router)
    # The review queue for everything `parameter_value_candidate` refused to
    # auto-promote — the safety valve for every "never auto-accept" rule above.
    app.include_router(enrichment.router)
    return app


app = create_app()
