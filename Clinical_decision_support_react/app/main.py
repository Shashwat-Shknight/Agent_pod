"""NuuCare server: JSON API (/api/... for the React UI, plus the original routes) and the built React app.

One process, one port: http://127.0.0.1:8000 serves both the UI and the API, so there is no CORS and no
second server to start. During UI development, Vite (port 5173) proxies /api to this server.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import vbc_poc
from app.api.reference_routes import router as reference_router
from app.api.routes import router as legacy_router
from app.reference import scheduler
from app.web import admin_routes, clinical_routes, rcm_routes
from app.web.context import legacy_route_guard
from app.web.errors import install_error_handlers

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# First path segments that belong to APIs: never answered with the React page (a missing API must 404).
API_PREFIXES = {"api", "docs", "redoc", "openapi.json", "vbc", "encounters", "codes", "ehr", "patients", "reference",
                "analyze", "retrieve", "health", "dev"}
CLIENT_HEADER = "x-nuucare-client"


@asynccontextmanager
async def lifespan(_application: FastAPI):
    scheduler.start()  # daily ICD-10-CM / HCPCS sync (Snowflake backend only; REFERENCE_AUTO_SYNC=0 disables)
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    application = FastAPI(
        title="NuuCare Clinical Decision Support API",
        description="Ambient scribe, grounded CDS, coding & HCC and RCM hand-off.",
        version="2.0.0",
        lifespan=lifespan,
    )
    install_error_handlers(application)

    @application.middleware("http")
    async def protect(request: Request, call_next):
        # CSRF: the session cookie is SameSite=Strict, and every state-changing /api call must also carry a
        # custom header that a cross-site HTML form cannot send.
        if request.url.path.startswith("/api/") and request.method not in SAFE_METHODS:
            if request.headers.get(CLIENT_HEADER) != "web":
                return JSONResponse(status_code=403, content={
                    "error": {"code": "csrf_blocked", "message": "Request blocked: missing client header."},
                    "detail": "Request blocked: missing client header."})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # React API (session-authenticated per route)
    application.include_router(admin_routes.router)
    application.include_router(clinical_routes.router)
    application.include_router(rcm_routes.router)

    # Original endpoints: same paths, now require a signed-in user (the guard leaves /health open)
    guard = [Depends(legacy_route_guard)]
    application.include_router(legacy_router, dependencies=guard)
    application.include_router(reference_router, dependencies=guard)
    if vbc_poc.enabled():  # feature flag: VBC_POC_ENABLED=0 removes the POC without touching the encounter flow
        from app.api.vbc_routes import router as vbc_router

        application.include_router(vbc_router, dependencies=guard)

    _mount_web(application)
    return application


def _mount_web(application: FastAPI) -> None:
    """Serve the built React app; unknown non-API paths return index.html so client-side routes deep-link."""
    index = WEB_DIST / "index.html"
    assets = WEB_DIST / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.split("/", 1)[0] in API_PREFIXES:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Not found"}, "detail": "Not found"})
        candidate = (WEB_DIST / full_path).resolve()
        if full_path and candidate.is_file() and WEB_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse(status_code=503, content={
            "error": {"code": "ui_not_built", "message": "The React UI is not built. Run: python -m app.scripts.build_web"},
            "detail": "The React UI is not built."})


app = create_app()
