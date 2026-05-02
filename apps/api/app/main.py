"""Evidence-First MVP-0 API entrypoint."""
from __future__ import annotations

from fastapi import FastAPI

from evidencefirst_shared.errors import install_normalized_error_handler

from .routes import answers as answers_routes
from .routes import audit as audit_routes
from .routes import claims as claims_routes
from .routes import documents as documents_routes
from .routes import health as health_routes
from .routes import projects as projects_routes
from .routes import tasks as tasks_routes


def create_app() -> FastAPI:
    app = FastAPI(
        title="Evidence-First MVP-0 API",
        version="0.4.0",
    )

    install_normalized_error_handler(app)

    app.include_router(health_routes.router)
    app.include_router(projects_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(documents_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(claims_routes.router)
    app.include_router(answers_routes.router)

    return app


app = create_app()
