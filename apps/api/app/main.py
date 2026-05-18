"""Evidence-First MVP-0 API entrypoint."""
from __future__ import annotations

from fastapi import FastAPI

from evidencefirst_shared.errors import install_normalized_error_handler

from .routes import answers as answers_routes
from .routes import anti_hallucination_report as anti_hallucination_report_routes
from .routes import audit as audit_routes
from .routes import claim_entailment as claim_entailment_routes
from .routes import claims as claims_routes
from .routes import documents as documents_routes
from .routes import health as health_routes
from .routes import lifecycle_events as lifecycle_events_routes
from .routes import projects as projects_routes
from .routes import source_loss as source_loss_routes
from .routes import source_quality as source_quality_routes
from .routes import task_source_loss as task_source_loss_routes
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
    app.include_router(source_loss_routes.router)
    app.include_router(lifecycle_events_routes.router)
    app.include_router(task_source_loss_routes.router)
    app.include_router(source_quality_routes.router)
    app.include_router(claim_entailment_routes.router)
    app.include_router(anti_hallucination_report_routes.router)

    return app


app = create_app()
