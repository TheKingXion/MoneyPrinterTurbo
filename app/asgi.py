"""Application implementation - ASGI."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.models.exception import HttpException
from app.router import root_api_router
from app.services.tiktok_scheduler import tiktok_scheduler
from app.services.youtube_batch_runner import youtube_batch_runner
from app.services import performance
from app.services import task as task_service
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """Start recovery/schedulers and close their resources with the API process."""
    task_service.recover_interrupted_cross_posts()
    try:
        profile = performance.get_runtime_profile()
        logger.info(
            "adaptive performance profile: "
            f"codec={profile.h264_codec}, threads={profile.ffmpeg_threads}, "
            f"render_slots={profile.render_slots}, network_slots={profile.network_slots}"
        )
    except Exception as exc:
        logger.warning(f"performance hardware detection failed: {exc}")
    tiktok_scheduler.start()
    youtube_batch_runner.resume_pending()
    logger.info("startup event")
    try:
        yield
    finally:
        youtube_batch_runner.shutdown()
        tiktok_scheduler.stop()
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()

# Configures the CORS middleware for the FastAPI app
cors_allowed_origins_str = os.getenv("CORS_ALLOWED_ORIGINS", "")
origins = [origin.strip() for origin in cors_allowed_origins_str.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=None if origins else r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_dir = utils.task_dir()
app.mount(
    "/tasks", StaticFiles(directory=task_dir, html=True, follow_symlink=True), name=""
)

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")
