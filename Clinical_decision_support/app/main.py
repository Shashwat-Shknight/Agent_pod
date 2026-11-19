from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import vbc_poc
from app.api.reference_routes import router as reference_router
from app.api.routes import router
from app.db.client import StorageError
from app.reference import scheduler


@asynccontextmanager
async def lifespan(_application: FastAPI):
    scheduler.start()  # daily ICD-10-CM / HCPCS sync (Snowflake backend only; REFERENCE_AUTO_SYNC=0 disables)
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Clinical Decision Support API",
        description="Decision-support suggestions grounded in the local medical knowledge base.",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.include_router(reference_router)
    if vbc_poc.enabled():  # feature flag: VBC_POC_ENABLED=0 removes the POC without touching the encounter flow
        from app.api.vbc_routes import router as vbc_router

        application.include_router(vbc_router)

    @application.exception_handler(StorageError)
    async def storage_unavailable(_request: Request, error: StorageError):
        # Database outages are reported as 503 instead of an opaque 500; nothing falls back to local files.
        return JSONResponse(status_code=503, content={"detail": f"Database unavailable: {error}"})

    return application


app = create_app()
