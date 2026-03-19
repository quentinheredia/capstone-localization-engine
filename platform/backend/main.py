"""
IPS Management Platform — Backend Entry Point

Launches the FastAPI application with:
  - Full CRUD for the Campus → Building → Floor → Room hierarchy
  - Anchor and Tag device management
  - Boundary crossing and alert log viewer
  - Config YAML generation from database state
  - Docker engine lifecycle management

Usage:
  python main.py                                # dev mode
  uvicorn main:app --host 0.0.0.0 --port 8080  # production

The localization engine (Hybrid) runs as a separate container on port 8001.
This platform API runs on port 8080.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from models import Base, engine as db_engine
from api.hierarchy import router as hierarchy_router
from api.devices import router as devices_router
from api.logs import router as logs_router
from api.config import router as config_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("platform")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────
    log.info("Creating database tables (if not exist)...")
    Base.metadata.create_all(bind=db_engine)
    log.info("Platform API ready.")
    yield
    # ── shutdown ─────────────────────────────────────────────────────────
    log.info("Shutting down.")


app = FastAPI(
    title="IPS Management Platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register route modules
app.include_router(hierarchy_router)
app.include_router(devices_router)
app.include_router(logs_router)
app.include_router(config_router)


@app.get("/health")
def health():
    return {"ok": True, "service": "ips-platform"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
