import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .http_clients import channel_client
from .models import ChannelAdapterStatus, PublishRequest, PublishResult
from .runtime import check_rate_limit, create_publish_log, record_event, update_publish_log

BLUESKY_SESSION: dict[str, Any] = {}


def adapter_statuses() -> list[ChannelAdapterStatus]:
    return [_status("instagram"), _status("telegram"), _status("bluesky")]


def adapter_diagnostics(channel: str) -> dict[str, Any]:
    status = _status(channel)
    return {
        "channel": channel,
        "protocol": status.protocol,
        "configured": status.configured,
        "mode": status.mode,
        "required_config": status.required_config,
        "missing_config": status.missing_config,
        "rate_limit": status.rate_limit,
        "supports_autopublish": status.supports_autopublish,
        "next_action": status.next_action,
    }


async def publish(payload: PublishRequest) -> PublishResult:
    ok, limit_message = check_rate_limit(payload.channel)
    diagnostics = adapter_diagnostics(payload.channel)
    log = create_publish_log(payload.channel, payload.model_dump(mode="json"), payload.dry_run, diagnostics)

    if not ok:
        next_action = "Wait for the rate-limit window to reset or lower automation frequency."
        update_publish_log(log["id"], "rate_limited", error=limit_message, retryable=True, next_action=next_action)
        return _result(
            payload,
            log["id"],
            "rate_limited",
            limit_message,
            error=limit_message,
            retryable=True,
            next_action=next_action,
            diagnostics=diagnostics,
        )

    try:
        if payload.channel == "instagram":
            if os.getenv("INSTAGRAM_DIRECT_PUBLISH_ENABLED", "").lower() == "true" and not payload.dry_run:
                external_id = await _publish_instagram(payload)
                next_action = "Post is live on Instagram."
                update_publish_log(log["id"], "published", external_id=external_id, next_action=next_action)
                return _result(payload, log["id"], "published", limit_message, external_id=external_id, next_action=next_action, diagnostics=diagnostics)
            external_id = _instagram_export_id(payload)
            next_action = "Upload/schedule this export in Meta Business Suite or connect Instagram Graph API credentials."
            update_publish_log(log["id"], "exported", external_id=external_id, next_action=next_action)
            return _result(payload, log["id"], "exported", limit_message, external_id=external_id, next_action=next_action, diagnostics=diagnostics)

        if payload.dry_run:
            external_id = f"dry-run-{payload.channel}-{log['id']}"
            next_action = f"Set dry_run=false after configuring {payload.channel} credentials and approval policy."
            update_publish_log(log["id"], "dry_run", external_id=external_id, next_action=next_action)
            return _result(payload, log["id"], "dry_run", limit_message, external_id=external_id, next_action=next_action, diagnostics=diagnostics)

        if payload.channel == "telegram":
            external_id = await _publish_telegram(payload)
            next_action = "Verify message in target Telegram chat."
            update_publish_log(log["id"], "published", external_id=external_id, next_action=next_action)
            return _result(payload, log["id"], "published", limit_message, external_id=external_id, next_action=next_action, diagnostics=diagnostics)

        if payload.channel == "bluesky":
            external_id = await _publish_bluesky(payload)
            next_action = "Verify post URI on Bluesky profile."
            update_publish_log(log["id"], "published", external_id=external_id, next_action=next_action)
            return _result(payload, log["id"], "published", limit_message, external_id=external_id, next_action=next_action, diagnostics=diagnostics)
    except Exception as exc:
        next_action = _next_action_for_error(payload.channel, str(exc), diagnostics)
        update_publish_log(log["id"], "failed", error=str(exc), retryable=True, next_action=next_action, response_preview=str(exc))
        return _result(
            payload,
            log["id"],
            "failed",
            limit_message,
            error=str(exc),
            retryable=True,
            next_action=next_action,
            diagnostics=diagnostics,
        )

    next_action = "Use instagram, telegram, or bluesky."
    update_publish_log(log["id"], "failed", error="Unsupported channel", next_action=next_action)
    return _result(payload, log["id"], "failed", limit_message, error="Unsupported channel", next_action=next_action, diagnostics=diagnostics)


async def retry_publish(log: dict[str, Any]) -> PublishResult:
    raw_payload = log.get("payload")
    payload_data = raw_payload if isinstance(raw_payload, dict) else _payload_from_preview(log.get("payload_preview", ""))
    payload = PublishRequest(**payload_data)
    return await publish(payload)


def _payload_from_preview(preview: str) -> dict[str, Any]:
    try:
        value = json.loads(preview)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    return {"channel": "telegram", "text": str(preview)[:280], "dry_run": True}


def _instagram_export_id(payload: PublishRequest) -> str:
    record_event("instagram_schedule_export_created", channel="instagram", campaign_name=payload.campaign_name)
    return f"instagram-export-{payload.campaign_name.lower().replace(' ', '-')[:40]}"


def _status(channel: str) -> ChannelAdapterStatus:
    instagram_direct_enabled = os.getenv("INSTAGRAM_DIRECT_PUBLISH_ENABLED", "").lower() == "true"
    config = {
        "instagram": {
            "protocol": "Instagram Graph API direct publish; Meta Business Suite export fallback",
            "required": ["INSTAGRAM_BUSINESS_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_FACEBOOK_PAGE_ID"],
            "mode": "direct_publish" if instagram_direct_enabled else "scheduling_export",
            "supports_autopublish": instagram_direct_enabled,
            "next_action": "Review caption/image, verify media URL, then use dry_run=false to publish." if instagram_direct_enabled else "Set INSTAGRAM_DIRECT_PUBLISH_ENABLED=true before direct publishing.",
        },
        "telegram": {
            "protocol": "Telegram Bot API sendMessage",
            "required": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
            "mode": "autopublish_or_dry_run",
            "supports_autopublish": True,
            "next_action": "Create a bot, add it to the target chat/channel, set TELEGRAM_CHAT_ID, then set dry_run=false.",
        },
        "bluesky": {
            "protocol": "AT Protocol com.atproto.server.createSession + com.atproto.repo.createRecord",
            "required": ["BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"],
            "mode": "autopublish_or_dry_run",
            "supports_autopublish": True,
            "next_action": "Create an app password for the handle, configure credentials, then set dry_run=false.",
        },
    }[channel]
    missing = [name for name in config["required"] if not os.getenv(name)]
    configured = len(missing) == 0
    return ChannelAdapterStatus(
        channel=channel,  # type: ignore[arg-type]
        configured=configured,
        mode=config["mode"],
        rate_limit=_rate_limit_label(channel),
        supports_autopublish=config["supports_autopublish"],
        protocol=config["protocol"],
        required_config=config["required"],
        missing_config=missing,
        next_action="" if configured and channel != "instagram" else config["next_action"],
    )


async def _publish_instagram(payload: PublishRequest) -> str:
    ig_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    if not ig_id or not token:
        raise RuntimeError("Instagram credentials missing: INSTAGRAM_BUSINESS_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN are required")

    api_version = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v25.0")
    base_url = f"https://graph.facebook.com/{api_version}"

    media_url = await _resolve_instagram_media_url(payload)

    caption = (payload.text or "")[:2200]
    alt_text = (payload.alt_text or "")[:1000] if payload.alt_text else None

    container_params: dict[str, Any] = {
        "image_url": media_url,
        "caption": caption,
        "access_token": token,
    }
    if alt_text:
        container_params["alt_text"] = alt_text

    client = channel_client()
    container_resp = await client.post(f"{base_url}/{ig_id}/media", params=container_params)
    container_resp.raise_for_status()
    container_id = container_resp.json().get("id")
    if not container_id:
        raise RuntimeError(f"No container ID returned from Meta: {container_resp.text}")

    for _ in range(10):
        await _async_sleep(6)
        status_resp = await client.get(
            f"{base_url}/{container_id}",
            params={"fields": "status_code", "access_token": token},
        )
        status_resp.raise_for_status()
        status_code = status_resp.json().get("status_code")
        if status_code == "FINISHED":
            break
        if status_code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container failed with status: {status_code}")
    else:
        raise RuntimeError("Instagram container did not reach FINISHED status after 60s")

    publish_resp = await client.post(
        f"{base_url}/{ig_id}/media_publish",
        params={"creation_id": container_id, "access_token": token},
    )
    publish_resp.raise_for_status()
    media_id = publish_resp.json().get("id")
    if not media_id:
        raise RuntimeError(f"No media ID returned from Meta publish: {publish_resp.text}")

    record_event("instagram_published", channel="instagram", media_id=media_id, campaign_name=payload.campaign_name)
    return str(media_id)


async def _resolve_instagram_media_url(payload: PublishRequest) -> str:
    if payload.media_url:
        return payload.media_url
    if payload.local_image_path:
        from .media import local_path_to_public_jpeg
        public_base = os.getenv("INSTAGRAM_PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base:
            raise RuntimeError("INSTAGRAM_PUBLIC_BASE_URL is not set — cannot serve local image to Meta")
        filename = local_path_to_public_jpeg(payload.local_image_path)
        return f"{public_base}/media/{filename}"
    raise RuntimeError("Either local_image_path or media_url must be provided for Instagram publish")


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


async def _publish_telegram(payload: PublishRequest) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials missing: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    response = await channel_client().post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": payload.text, "disable_web_page_preview": False},
    )
    response.raise_for_status()
    data = response.json()
    return str(data.get("result", {}).get("message_id", "telegram-message"))


async def _publish_bluesky(payload: PublishRequest) -> str:
    handle = os.getenv("BLUESKY_HANDLE")
    password = os.getenv("BLUESKY_APP_PASSWORD")
    if not handle or not password:
        raise RuntimeError("Bluesky credentials missing: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD are required")
    access_jwt, repo = await _bluesky_session(handle, password)
    try:
        record = await _create_bluesky_record(repo, access_jwt, payload.text)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        BLUESKY_SESSION.clear()
        access_jwt, repo = await _bluesky_session(handle, password)
        record = await _create_bluesky_record(repo, access_jwt, payload.text)
    return str(record.get("uri", "bluesky-post"))


async def _bluesky_session(handle: str, password: str) -> tuple[str, str]:
    if _cached_bluesky_session_valid(handle):
        return BLUESKY_SESSION["access_jwt"], BLUESKY_SESSION["did"]

    session_response = await channel_client().post(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
    )
    session_response.raise_for_status()
    session = session_response.json()
    access_jwt = session.get("accessJwt")
    did = session.get("did")
    if not access_jwt or not did:
        raise ValueError("Invalid Bluesky session response: missing accessJwt or did")

    BLUESKY_SESSION.clear()
    BLUESKY_SESSION.update({
        "handle": handle,
        "access_jwt": access_jwt,
        "did": did,
        "expires_at": _jwt_expires_at(access_jwt),
    })
    return access_jwt, did


def _cached_bluesky_session_valid(handle: str) -> bool:
    expires_at = float(BLUESKY_SESSION.get("expires_at", 0))
    return (
        BLUESKY_SESSION.get("handle") == handle
        and bool(BLUESKY_SESSION.get("access_jwt"))
        and bool(BLUESKY_SESSION.get("did"))
        and expires_at > time.time() + 60
    )


def _jwt_expires_at(token: str) -> float:
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:
        pass
    return time.time() + 3000


async def _create_bluesky_record(repo: str, access_jwt: str, text: str) -> dict[str, Any]:
    record_response = await channel_client().post(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={
            "repo": repo,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": text[:300],
                "createdAt": _now_iso(),
            },
        },
    )
    record_response.raise_for_status()
    record = record_response.json()
    if not isinstance(record, dict):
        raise ValueError("Invalid Bluesky record response")
    return record


def _next_action_for_error(channel: str, error: str, diagnostics: dict[str, Any]) -> str:
    if diagnostics.get("missing_config"):
        return f"Set missing config: {', '.join(diagnostics['missing_config'])}."
    if "401" in error or "Unauthorized" in error:
        return "Refresh credentials/app password and confirm account permissions."
    if "403" in error or "Forbidden" in error:
        return "Confirm account role, business permissions, and channel write access."
    if "429" in error:
        return "Back off publishing schedule and inspect platform rate limits."
    if channel == "telegram":
        return "Confirm bot is a member/admin of the target chat and TELEGRAM_CHAT_ID is correct."
    if channel == "bluesky":
        return "Confirm handle/app password pair and AT Protocol service availability."
    if channel == "instagram":
        return "Use scheduling export now; verify business account/page/token before direct publishing."
    return "Inspect adapter diagnostics and retry after fixing configuration."


def _result(
    payload: PublishRequest,
    log_id: str,
    status: str,
    rate_limit: str,
    *,
    external_id: str = "",
    error: str = "",
    retryable: bool = False,
    next_action: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> PublishResult:
    return PublishResult(
        publish_log_id=log_id,
        channel=payload.channel,
        status=status,
        dry_run=payload.dry_run,
        external_id=external_id,
        error=error,
        rate_limit=rate_limit,
        retryable=retryable,
        next_action=next_action,
        diagnostics=diagnostics or adapter_diagnostics(payload.channel),
        payload=_channel_payload(payload),
    )


def _channel_payload(payload: PublishRequest) -> dict[str, Any]:
    if payload.channel == "instagram":
        return {
            "caption": payload.text,
            "image_prompt": payload.image_prompt,
            "link_url": payload.link_url,
            "format": "scheduling_export",
        }
    return {"text": payload.text, "link_url": payload.link_url}


def _rate_limit_label(channel: str) -> str:
    return os.getenv(f"{channel.upper()}_RATE_LIMIT", "10/3600")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
