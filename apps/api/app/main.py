"""FastAPI factory."""
from __future__ import annotations

from fastapi import FastAPI

from .errors import RequestIdMiddleware, install_error_handlers
from .routes import audit as audit_routes
from .routes import claims as claims_routes
from .routes import documents as documents_routes
from .routes import health as health_routes
from .routes import projects as projects_routes
from .routes import tasks as tasks_routes


def create_app() -> FastAPI:
    app = FastAPI(
        title="Evidence-First API",
        version="0.3.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    app.include_router(health_routes.router)
    app.include_router(projects_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(documents_routes.router)
    app.include_router(claims_routes.router)

    return app


app = create_app()