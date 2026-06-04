"""Fire-and-forget publisher to the IACP Redis event-bus.

artrack publishes domain events (e.g. ``artrack.waypoint_deleted``) to
``swfme.webhook.{event}`` topics on the shared IACP Redis bus. The swfme-api
WebhookWorker psubscribes on ``swfme.webhook.*``, validates the envelope and
async-triggers the matching workflow (e.g. CascadeDeleteWaypoint, which cleans
Knowledge's dangling ``locations[].waypoint_id`` refs).

Design:
  * Decoupled — artrack only knows the bus, never swfme's URL/auth.
  * Fire-and-forget — a publish failure (bus down, no subscriber) is logged
    and swallowed; it must never break the originating request (the waypoint
    is already deleted). Knowledge's periodic crawl is the durability backstop.
  * Timeout-guarded — the publish is capped so a hung Redis connection cannot
    add latency to the API response.

Federation envelope contract (v1, from swfme-api workers/webhook.py):
    {
      "event":           "artrack.waypoint_deleted",   # required, == WebhookTrigger.event
      "source_service":  "artrack",                     # required, becomes caller_agent
      "tenant_id":       "arkturian",                   # required
      "event_id":        "<uuid>",                      # required (default idempotency_key)
      "timestamp":       "<iso8601>",                   # informational
      "payload":         {...}                          # required dict, workflow parameters
    }
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from .config import settings

logger = logging.getLogger("artrack.event_bus")

# Cap on the publish so a hung/unreachable bus never stalls the API response.
_PUBLISH_TIMEOUT_SECONDS = 2.0

# Lazily-created, reused async Redis client (one per worker process).
_client = None


def _get_client():
    global _client
    if _client is None:
        import redis.asyncio as redis_asyncio  # lazy: missing dep can't break import-time

        _client = redis_asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def publish_event(
    event: str,
    payload: dict,
    tenant_id: str = "arkturian",
    idempotency_key: Optional[str] = None,
) -> bool:
    """Publish one event to ``swfme.webhook.{event}``. Returns True on success.

    Never raises — all failures are logged and swallowed (fire-and-forget).
    """
    if not settings.EVENT_BUS_ENABLED:
        logger.debug("event_bus disabled — skipping publish of %s", event)
        return False

    envelope = {
        "event": event,
        "source_service": "artrack",
        "tenant_id": tenant_id,
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    if idempotency_key:
        envelope["idempotency_key"] = idempotency_key

    topic = f"swfme.webhook.{event}"
    try:
        client = _get_client()
        receivers = await asyncio.wait_for(
            client.publish(topic, json.dumps(envelope)),
            timeout=_PUBLISH_TIMEOUT_SECONDS,
        )
        logger.info(
            "event_bus published %s event_id=%s receivers=%s payload=%s",
            topic, envelope["event_id"], receivers, payload,
        )
        return True
    except Exception as e:  # noqa: BLE001 — fire-and-forget, never propagate
        logger.warning(
            "event_bus publish failed for %s: %s (fire-and-forget, ignored)",
            topic, e,
        )
        return False
