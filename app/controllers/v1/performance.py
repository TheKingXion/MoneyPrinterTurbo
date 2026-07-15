import dataclasses

from fastapi import Depends, Query, Request

from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services import performance
from app.utils import utils


def _require_local_access(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HttpException(
            "performance-access",
            status_code=403,
            message="Performance API is restricted to localhost",
        )


router = new_router(dependencies=[Depends(_require_local_access)])


@router.get("/performance/hardware", summary="Detected hardware and adaptive profile")
def hardware_profile():
    hardware = performance.detect_hardware()
    ffmpeg = performance.inspect_ffmpeg()
    profile = performance.get_runtime_profile()
    return utils.get_response(
        200,
        {
            "hardware": dataclasses.asdict(hardware),
            "ffmpeg": {
                **dataclasses.asdict(ffmpeg),
                "encoders": sorted(ffmpeg.encoders),
                "hwaccels": sorted(ffmpeg.hwaccels),
            },
            "profile": dataclasses.asdict(profile),
        },
    )


@router.post("/performance/reprobe", summary="Re-detect hardware and benchmark encoders")
def reprobe_hardware():
    profile = performance.get_runtime_profile(force=True)
    return utils.get_response(200, dataclasses.asdict(profile))


@router.get("/performance/metrics", summary="Recent generation performance metrics")
def performance_metrics(limit: int = Query(20, ge=1, le=200)):
    telemetry = performance.get_telemetry()
    return utils.get_response(
        200,
        {
            "tasks": telemetry.recent_tasks(limit),
            "stages": telemetry.aggregate_stage_timings(),
            "resources": telemetry.latest_resource_sample(),
            "summary": telemetry.summary(),
        },
    )
