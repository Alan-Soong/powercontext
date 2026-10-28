"""WorkPulse application entry point."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("workpulse")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    await db.ensure_schema()
    log.info("seekdb ready at %s:%s/%s", settings.seekdb_host, settings.seekdb_port, settings.seekdb_database)
    log.info("PowerContext target: %s", settings.pc_url)
    log.info("embedding provider: %s", settings.embedding_provider)
    yield
    await db.close_pool()


app = FastAPI(title="WorkPulse", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
