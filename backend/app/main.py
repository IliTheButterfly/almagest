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
from app.api.routes import search, system
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
    return app


app = create_app()
