"""
Content API Client for ARTrack Knowledge Storage.

Handles CRUD operations for artrack_narration posts in the Content API.
Each track's knowledge (narratives) is stored as a single JSON post.
"""

import httpx
import json
import logging
from typing import Optional, Dict, Any

from .config import settings

logger = logging.getLogger(__name__)

CONTENT_API_BASE = settings.CONTENT_API_BASE
DOC_TYPE = "audio_guide"
AUTHOR_ID = "artrack-system"
AUTHOR_NAME = "ARTrack Knowledge System"
PARTNER_ID = "artrack"


def _slug_for_track(track_id: int) -> str:
    """Deterministic slug for a track's narration post."""
    return f"artrack-narration-{track_id}"


def get_narration_post(track_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch the narration post for a track from content-api.

    Returns the full post dict, or None if not found.
    """
    slug = _slug_for_track(track_id)
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            # Try to find by slug via posts list endpoint
            resp = client.get(
                f"{CONTENT_API_BASE}/api/v1/posts/",
                params={"doc_type": DOC_TYPE, "partner_id": PARTNER_ID, "limit": 100}
            )
            if resp.status_code != 200:
                logger.warning(f"Content API list failed: {resp.status_code}")
                return None

            data = resp.json()
            posts = data.get("posts", [])

            # Find the post for this track by slug or metadata
            for post in posts:
                if post.get("slug") == slug:
                    # Fetch full details
                    detail_resp = client.get(f"{CONTENT_API_BASE}/api/v1/posts/{post['id']}/")
                    if detail_resp.status_code == 200:
                        return detail_resp.json()
                    return post

                meta = post.get("metadata_json") or {}
                if meta.get("track_id") == track_id:
                    detail_resp = client.get(f"{CONTENT_API_BASE}/api/v1/posts/{post['id']}/")
                    if detail_resp.status_code == 200:
                        return detail_resp.json()
                    return post

    except httpx.RequestError as e:
        logger.error(f"Content API request failed: {e}")

    return None


def get_narration_knowledge(track_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch and parse the knowledge JSON for a track.

    Returns the parsed knowledge dict, or None if not found.
    """
    post = get_narration_post(track_id)
    if not post:
        return None

    content = post.get("content")
    if not content:
        return None

    try:
        return json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Failed to parse narration content for track {track_id}")
        return None


def save_narration_knowledge(
    track_id: int,
    track_name: str,
    knowledge: Dict[str, Any]
) -> Optional[int]:
    """
    Create or update the narration post for a track.

    Returns the post ID on success, None on failure.
    """
    slug = _slug_for_track(track_id)
    config = knowledge.get("config", {})
    language = config.get("language", "de")

    # Count filled texts for metadata
    routes_count = len(knowledge.get("routes", {}))
    segments_count = len(knowledge.get("segments", {}))
    pois_count = len(knowledge.get("pois", {}))

    post_data = {
        "title": f"Audio Guide: {track_name}",
        "slug": slug,
        "doc_type": DOC_TYPE,
        "content_type": "json",
        "content": json.dumps(knowledge, ensure_ascii=False),
        "status": "published",
        "author_id": AUTHOR_ID,
        "author_name": AUTHOR_NAME,
        "partner_id": PARTNER_ID,
        "metadata_json": {
            "type": DOC_TYPE,
            "track_id": track_id,
            "track_name": track_name,
            "language": language,
            "routes_count": routes_count,
            "segments_count": segments_count,
            "pois_count": pois_count,
        }
    }

    try:
        existing = get_narration_post(track_id)

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            if existing:
                # Update existing post
                post_id = existing["id"]
                resp = client.put(
                    f"{CONTENT_API_BASE}/api/v1/posts/{post_id}/",
                    json=post_data
                )
                if resp.status_code == 200:
                    logger.info(f"Updated narration post {post_id} for track {track_id}")
                    return post_id
                else:
                    logger.error(f"Content API update failed: {resp.status_code} {resp.text}")
                    return None
            else:
                # Create new post
                resp = client.post(
                    f"{CONTENT_API_BASE}/api/v1/posts/",
                    json=post_data
                )
                if resp.status_code in (200, 201):
                    result = resp.json()
                    post_id = result.get("id")
                    logger.info(f"Created narration post {post_id} for track {track_id}")
                    return post_id
                else:
                    logger.error(f"Content API create failed: {resp.status_code} {resp.text}")
                    return None

    except httpx.RequestError as e:
        logger.error(f"Content API request failed: {e}")
        return None


def delete_narration_post(track_id: int) -> bool:
    """
    Delete the narration post for a track.

    Returns True on success, False on failure.
    """
    existing = get_narration_post(track_id)
    if not existing:
        return True  # Nothing to delete

    post_id = existing["id"]
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.delete(f"{CONTENT_API_BASE}/api/v1/posts/{post_id}/")
            if resp.status_code in (200, 204):
                logger.info(f"Deleted narration post {post_id} for track {track_id}")
                return True
            else:
                logger.error(f"Content API delete failed: {resp.status_code}")
                return False
    except httpx.RequestError as e:
        logger.error(f"Content API request failed: {e}")
        return False
