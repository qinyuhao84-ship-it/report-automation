from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from report_automation.api import extract, frontend, generate, other_proof


def create_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(other_proof.router)
    app.include_router(generate.router)
    app.include_router(extract.router)
    app.include_router(frontend.router)
    return app


app = create_app()
