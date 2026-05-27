import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ai_providers import generate_text, provider_status
from .ai_news_pipeline import run_ai_news_pipeline
from .artifact_store import ALLOWED_ARTIFACT_NAMES, default_store as artifact_store
from .channel_adapters import adapter_diagnostics, adapter_statuses, publish, retry_publish
from .http_clients import close_http_clients
from .logging_config import configure_logging
from .media import media_dir
from .models import AiNewsRunRequest, AiNewsRunResult, CampaignPack, CampaignRequest, ChannelAdapterStatus, PublishRequest, PublishResult, Story
from .runtime import APP_NAME, allowed_origins, debug_snapshot, finish_run, get_publish_log, get_run, list_publish_logs, list_runs, record_event, start_run, uptime_seconds


configure_logging(APP_NAME)
logger = logging.getLogger(APP_NAME)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        yield
    finally:
        await close_http_clients()


app = FastAPI(title="Social PR Autopilot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/media", StaticFiles(directory=str(media_dir())), name="media")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        record_event("request_failed", level="error", request_id=request_id, method=request.method, path=request.url.path, duration_ms=duration_ms, error=str(exc))
        logger.exception("request_failed", extra={"app": APP_NAME, "event": "request_failed", "request_id": request_id, "method": request.method, "path": request.url.path, "duration_ms": duration_ms, "error": str(exc)})
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["x-request-id"] = request_id
    logger.info("request_completed", extra={"app": APP_NAME, "event": "request_completed", "request_id": request_id, "method": request.method, "path": request.url.path, "status_code": response.status_code, "duration_ms": duration_ms})
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "social-pr-autopilot"}


@app.get("/ready")
async def ready() -> dict:
    return {
        "status": "ready",
        "app": "social-pr-autopilot",
        "uptime_seconds": uptime_seconds(),
        "providers": provider_status(),
    }


@app.get("/api/runs")
async def runs() -> dict:
    return {"runs": list_runs()}


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs/{run_id}/artifact/{name}")
async def run_artifact(run_id: str, name: str):
    if name not in ALLOWED_ARTIFACT_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown artifact: {name}")
    run_dir = artifact_store.run_dir(run_id)
    if not run_dir:
        raise HTTPException(status_code=404, detail="Run artifact directory not found")
    path = run_dir / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Artifact '{name}' not found for run {run_id}")
    return FileResponse(str(path), media_type="application/json")


@app.get("/debug")
async def debug() -> dict:
    return debug_snapshot()


@app.get("/api/channels", response_model=list[ChannelAdapterStatus])
async def channels() -> list[ChannelAdapterStatus]:
    return adapter_statuses()


@app.get("/api/channels/{channel}/diagnostics")
async def channel_diagnostics(channel: str) -> dict:
    if channel not in {"instagram", "telegram", "bluesky"}:
        raise HTTPException(status_code=404, detail="Channel not supported")
    return adapter_diagnostics(channel)


@app.get("/api/publish-logs")
async def publish_logs() -> dict:
    return {"publish_logs": list_publish_logs()}


@app.post("/api/publish", response_model=PublishResult)
async def publish_endpoint(payload: PublishRequest) -> PublishResult:
    return await publish(payload)


@app.post("/api/publish-logs/{publish_log_id}/retry", response_model=PublishResult)
async def retry_publish_endpoint(publish_log_id: str) -> PublishResult:
    log = get_publish_log(publish_log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Publish log not found")
    return await retry_publish(log)


@app.post("/api/ai-news/run", response_model=AiNewsRunResult)
async def ai_news_run(payload: AiNewsRunRequest) -> AiNewsRunResult:
    manual_story = None
    if payload.story_url:
        manual_story = Story(
            url=payload.story_url,
            title=payload.story_title or payload.story_url,
            summary=payload.story_summary or "",
            source=payload.source_name or "manual",
        )

    return await run_ai_news_pipeline(
        dry_run=payload.dry_run,
        publish=payload.publish,
        source_override=payload.sources,
        manual_story=manual_story,
    )


@app.post("/api/campaign", response_model=CampaignPack)
async def campaign(payload: CampaignRequest) -> CampaignPack:
    run = start_run("campaign", payload.model_dump(mode="json"))
    prompt = f"""
You are an autonomous B2B social and PR launch agent.
Product: {payload.product}
Event: {payload.event}
Audience: {payload.audience}
Launch date: {payload.launch_date}
Channels: {", ".join(payload.channels)}

Create a concise campaign kit with posts, PR pitch, image directions, and a seven-day calendar.
"""
    try:
        text = await generate_text(prompt, purpose="social PR campaign")
        posts = {channel: f"{channel.upper()}: {text[:240]}" for channel in payload.channels}
        pack = CampaignPack(
            run_id=run["id"],
            campaign_name=f"{payload.product} Launch Campaign",
            angle=text[:240],
            posts=posts,
            image_prompts=[
                f"Instagram square visual for {payload.product}: {payload.event}",
                f"Press hero image showing B2B automation workflow for {payload.product}",
            ],
            press_pitch=f"Pitch: {text[:500]}",
            calendar=[
                "Day 1: teaser post and Telegram announcement",
                "Day 2: founder POV thread",
                "Day 3: Instagram carousel",
                "Day 4: PR pitch follow-up",
                "Day 5: customer use-case post",
                "Day 6: behind-the-scenes build note",
                "Day 7: analytics digest and next test",
            ],
            publish_policy={
                "telegram": "autopublish",
                "bluesky": "autopublish after QA",
                "x": "draft first",
                "instagram": "schedule export",
                "press": "human approval",
            },
        )
        finish_run(run["id"], "completed", pack.angle)
        return pack
    except Exception as exc:
        finish_run(run["id"], "failed", "Campaign generation failed", str(exc))
        raise
