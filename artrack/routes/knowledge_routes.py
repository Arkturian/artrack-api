"""
Track Knowledge API v3

Handles generation and storage of pre-generated narrative texts for audio guides.

Storage:
- Route texts (intro/outro) → TrackRoute.metadata_json["knowledge"]
- POI texts (approaching/at_poi) → Waypoint.metadata_json["knowledge"]
- Segment texts (entry/exit) → Segment marker Waypoint.metadata_json["knowledge"]

Endpoints (Track-Level):
- GET  /tracks/{track_id}/knowledge - Get all knowledge for a track
- GET  /tracks/{track_id}/knowledge/version - Get lightweight version info (for cache validation)
- POST /tracks/{track_id}/knowledge/generate - Generate all narratives
- POST /tracks/{track_id}/knowledge/audio - Generate TTS audio for single item
- PUT  /tracks/{track_id}/knowledge - Save all knowledge
- DELETE /tracks/{track_id}/knowledge - Delete all knowledge
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel
import httpx
import json
import logging
import uuid
import asyncio

from ..database import get_db, SessionLocal
from ..models import Track, Waypoint, TrackRoute
from ..auth import get_current_user, User
from ..content_client import (
    get_narration_knowledge,
    save_narration_knowledge,
    delete_narration_post,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# File-based job storage (shared between workers)
import os
import tempfile

JOBS_FILE = "/tmp/artrack_generation_jobs.json"

def _load_jobs() -> Dict[str, Dict]:
    """Load jobs from file."""
    try:
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load jobs: {e}")
    return {}

def _save_jobs(jobs: Dict[str, Dict]):
    """Save jobs to file."""
    try:
        with open(JOBS_FILE, 'w') as f:
            json.dump(jobs, f)
    except Exception as e:
        logger.warning(f"Failed to save jobs: {e}")

def _get_job(job_id: str) -> Optional[Dict]:
    """Get a specific job."""
    jobs = _load_jobs()
    return jobs.get(job_id)

def _set_job(job_id: str, job_data: Dict):
    """Set a job."""
    jobs = _load_jobs()
    jobs[job_id] = job_data
    _save_jobs(jobs)

# AI API endpoint
INTERNAL_API_KEY = "Inetpass1"
AI_API_BASE = "https://api-ai.arkturian.com"


# ============ Pydantic Models ============

class NarrativeText(BaseModel):
    text: str = ""
    text_original: Optional[str] = None
    edited: bool = False
    audio_storage_id: Optional[int] = None


class KnowledgeConfig(BaseModel):
    persona: str = ""
    target_audience: str = ""
    language: str = "de"
    tone: str = "friendly"
    background_knowledge: str = ""


class GenerateRequest(BaseModel):
    persona: str = ""
    target_audience: str = ""
    language: str = "de"
    tone: str = "friendly"
    background_knowledge: str = ""
    generate_routes: bool = True
    generate_segments: bool = True
    generate_pois: bool = True
    only_missing: bool = False  # Only generate texts that are empty


class AudioGenerateRequest(BaseModel):
    """Request to generate TTS audio for a single knowledge item."""
    item_type: str  # "route", "segment", "poi"
    item_id: Optional[str] = None  # route_id, segment name, or waypoint_id
    text_type: str  # "intro", "outro", "entry", "exit", "approaching", "at_poi"
    voice: str = "nova"
    add_music: bool = False
    language: str = "de"


class CueItem(BaseModel):
    """A single cue within a knowledge item."""
    index: int
    text: str
    audio_storage_id: Optional[int] = None
    duration_seconds: Optional[float] = None


# ============ Helper Functions ============

def _load_track_data(db: Session, track_id: int) -> Dict:
    """Load all routes, segments, and POIs for a track."""

    # Load all routes
    routes = db.query(TrackRoute).filter(TrackRoute.track_id == track_id).all()

    # Load all waypoints (except GPS tracks)
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    # Separate segment markers and POIs
    # Screen points (photo/video uploads) are NOT considered POIs for knowledge generation
    # Only "manual" waypoints are proper POIs with meaningful content
    segment_waypoints = []
    pois = []

    for wp in all_waypoints:
        if wp.metadata_json and wp.metadata_json.get("segment"):
            segment_waypoints.append(wp)
        elif wp.waypoint_type == "manual":
            # Only manual waypoints are proper POIs for audio guide content
            pois.append(wp)
        # Skip photo/video/audio waypoints - these are screen points (client uploads)

    # Group segments by name
    segments = {}
    for wp in segment_waypoints:
        segment_meta = wp.metadata_json.get("segment", {})
        seg_name = segment_meta.get("name")
        role = segment_meta.get("role")

        if not seg_name or not role:
            continue

        if seg_name not in segments:
            segments[seg_name] = {
                "name": seg_name,
                "start_wp": None,
                "end_wp": None,
                "description": wp.user_description or ""
            }

        if role == "start":
            segments[seg_name]["start_wp"] = wp
            if wp.user_description:
                segments[seg_name]["description"] = wp.user_description
        elif role == "end":
            segments[seg_name]["end_wp"] = wp

    return {
        "routes": routes,
        "segments": segments,
        "pois": pois
    }


def _get_waypoint_knowledge(wp: Waypoint) -> Optional[Dict]:
    """Get knowledge from a waypoint's metadata."""
    if not wp or not wp.metadata_json:
        return None
    return wp.metadata_json.get("knowledge")


def _save_waypoint_knowledge(wp: Waypoint, knowledge: Dict, db: Session):
    """Save knowledge to a waypoint's metadata."""
    if not wp:
        return

    metadata = wp.metadata_json or {}
    metadata["knowledge"] = knowledge
    wp.metadata_json = metadata
    flag_modified(wp, "metadata_json")


async def _generate_narrative_text(
    narrative_type: str,
    context: Dict[str, Any],
    config: KnowledgeConfig
) -> str:
    """Generate narrative text using AI."""

    background = ""
    if config.background_knowledge:
        background = f"""
HINTERGRUNDWISSEN ZUM ORT:
{config.background_knowledge}

Nutze dieses Wissen um die Texte informativer und interessanter zu gestalten.
"""

    prompts = {
        "route_intro": f"""
Schreibe eine Willkommensnachricht für den Start einer Wanderroute.

Route: {context.get('route_name', 'Wanderroute')}
Länge: {context.get('route_length_km', 0):.1f} km
Beschreibung: {context.get('route_description', '')}
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe einen kurzen, einladenden Text (2-4 Sätze) der die Wanderer willkommen heißt und neugierig auf die Route macht.
Antworte NUR mit dem Text, keine Erklärungen.
""",
        "route_outro": f"""
Schreibe eine Abschlussnachricht für das Ende einer Wanderroute.

Route: {context.get('route_name', 'Wanderroute')}
Länge: {context.get('route_length_km', 0):.1f} km
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe einen kurzen Abschlusstext (2-3 Sätze) der die Wanderer verabschiedet und ihnen für die Wanderung dankt.
Antworte NUR mit dem Text, keine Erklärungen.
""",
        "segment_entry": f"""
Schreibe einen kurzen Begrüßungstext für einen Streckenabschnitt.

Abschnitt: {context.get('segment_name', 'Abschnitt')}
Beschreibung: {context.get('segment_description', '')}
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe 1-2 Sätze die den Abschnitt kurz beschreiben. Fokussiere auf das Wesentliche.
Antworte NUR mit dem Text, keine Erklärungen.
""",
        "segment_exit": f"""
Schreibe einen kurzen Übergangstext für das Verlassen eines Streckenabschnitts.

Abschnitt: {context.get('segment_name', 'Abschnitt')}
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe 1 Satz der den Übergang zum nächsten Abschnitt einleitet.
Antworte NUR mit dem Text, keine Erklärungen.
""",
        "poi_approaching": f"""
Schreibe eine kurze Ankündigung für einen Point of Interest.

POI: {context.get('poi_name', 'Sehenswürdigkeit')}
Beschreibung: {context.get('poi_description', '')}
Entfernung: ca. 50m
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe 1 Satz der den POI ankündigt und neugierig macht.
Antworte NUR mit dem Text, keine Erklärungen.
""",
        "poi_at": f"""
Schreibe eine Beschreibung für einen Point of Interest.

POI: {context.get('poi_name', 'Sehenswürdigkeit')}
Beschreibung vom Autor: {context.get('poi_description', '')}
{background}
Persona: {config.persona or 'Du bist ein freundlicher Audio-Guide.'}
Zielgruppe: {config.target_audience or 'Wanderer'}
Ton: {config.tone}
Sprache: {config.language}

Schreibe 2-4 Sätze die den POI beschreiben. Nutze die Beschreibung als Grundlage, erweitere sie aber mit interessanten Details.
Antworte NUR mit dem Text, keine Erklärungen.
"""
    }

    prompt = prompts.get(narrative_type, "")
    if not prompt:
        return ""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AI_API_BASE}/ai/claude",
                json={
                    "prompt": prompt,
                    "max_tokens": 300,
                    "temperature": 0.7
                },
                headers={
                    "X-API-KEY": INTERNAL_API_KEY,
                    "Content-Type": "application/json"
                },
                timeout=60.0
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("message", "").strip()
            else:
                logger.error(f"AI API returned {response.status_code}: {response.text}")
                return ""

    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        return ""


async def _split_text_into_cues(text: str, language: str = "de") -> List[str]:
    """
    Split narrative text into 3-5 cues using AI.

    Each cue should be a natural break point that can be played/skipped independently.
    This enables the audio manager to dynamically control playback.
    """
    if not text or len(text) < 50:
        # Very short texts → single cue
        return [text] if text else []

    prompt = f"""
Teile den folgenden Text in 3-5 kurze Abschnitte (Cues) auf.

Regeln:
- Jeder Cue sollte 1-3 Sätze lang sein
- Jeder Cue muss eigenständig verständlich sein
- Natürliche Sprechpausen als Trennpunkte nutzen
- Inhaltlich zusammengehörige Sätze zusammen lassen
- Mindestens 2 Cues, maximal 5 Cues

Text:
{text}

Antworte NUR mit den Cues, getrennt durch "---" auf einer eigenen Zeile.
Beispiel-Format:
Erster Cue mit einem oder zwei Sätzen.
---
Zweiter Cue mit weiterem Inhalt.
---
Dritter Cue zum Abschluss.
"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AI_API_BASE}/ai/claude",
                json={
                    "prompt": prompt,
                    "max_tokens": 500,
                    "temperature": 0.3  # Low temp for consistent splitting
                },
                headers={
                    "X-API-KEY": INTERNAL_API_KEY,
                    "Content-Type": "application/json"
                },
                timeout=60.0
            )

            if response.status_code == 200:
                result = response.json()
                ai_response = result.get("message", "").strip()

                # Parse cues from response
                cues = [cue.strip() for cue in ai_response.split("---") if cue.strip()]

                # Validate cues
                if len(cues) >= 2 and len(cues) <= 5:
                    return cues
                elif len(cues) == 1:
                    return cues
                else:
                    # Fallback: split on sentence boundaries
                    logger.warning(f"AI returned {len(cues)} cues, using fallback")
                    return _fallback_split(text)
            else:
                logger.error(f"AI API returned {response.status_code}")
                return _fallback_split(text)

    except Exception as e:
        logger.error(f"Cue splitting failed: {e}")
        return _fallback_split(text)


def _fallback_split(text: str) -> List[str]:
    """Fallback: split text into ~3 parts on sentence boundaries."""
    import re

    # Split on sentence endings
    sentences = re.split(r'(?<=[.!?])\s+', text)

    if len(sentences) <= 2:
        return [text]

    # Group into 3 parts
    total = len(sentences)
    part_size = max(1, total // 3)

    cues = []
    for i in range(0, total, part_size):
        chunk = " ".join(sentences[i:i + part_size])
        if chunk.strip():
            cues.append(chunk.strip())

    # Merge last two if we have 4+ cues
    while len(cues) > 3:
        cues[-2] = cues[-2] + " " + cues[-1]
        cues.pop()

    return cues if cues else [text]


# ============ API Endpoints (Track-Level) ============

@router.get("/{track_id}/knowledge")
def get_track_knowledge(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all knowledge for a track.

    Returns:
    - routes: List of routes with their intro/outro
    - segments: All segments with entry/exit texts
    - pois: All POIs with approaching/at_poi texts
    - config: Generation config stored at track level
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.visibility == "private" and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Load structural data (route/segment/poi names, IDs)
    data = _load_track_data(db, track_id)

    # Load narrative texts from content-api
    content_knowledge = get_narration_knowledge(track_id)

    if content_knowledge:
        knowledge = content_knowledge
        knowledge.setdefault("version", 3)
        knowledge.setdefault("config", {})
    else:
        knowledge = {
            "version": 3,
            "config": {},
            "routes": {},
            "segments": {},
            "pois": {}
        }

    # Merge structural data — ensure all current routes/segments/pois are represented
    for route in data["routes"]:
        rid = str(route.id)
        if rid not in knowledge.get("routes", {}):
            knowledge.setdefault("routes", {})[rid] = {
                "id": route.id,
                "name": route.name,
                "description": route.description or "",
                "intro": {"text": "", "edited": False},
                "outro": {"text": "", "edited": False}
            }
        else:
            knowledge["routes"][rid]["id"] = route.id
            knowledge["routes"][rid]["name"] = route.name
            knowledge["routes"][rid]["description"] = route.description or ""

    for seg_name, seg_data in data["segments"].items():
        if seg_name not in knowledge.get("segments", {}):
            knowledge.setdefault("segments", {})[seg_name] = {
                "name": seg_name,
                "description": seg_data.get("description", ""),
                "start_waypoint_id": seg_data["start_wp"].id if seg_data.get("start_wp") else None,
                "end_waypoint_id": seg_data["end_wp"].id if seg_data.get("end_wp") else None,
                "entry": {"text": "", "edited": False},
                "exit": {"text": "", "edited": False}
            }
        else:
            knowledge["segments"][seg_name]["start_waypoint_id"] = seg_data["start_wp"].id if seg_data.get("start_wp") else None
            knowledge["segments"][seg_name]["end_waypoint_id"] = seg_data["end_wp"].id if seg_data.get("end_wp") else None

    for poi in data["pois"]:
        pid = str(poi.id)
        poi_name = (poi.metadata_json or {}).get("title", f"POI #{poi.id}")
        if pid not in knowledge.get("pois", {}):
            knowledge.setdefault("pois", {})[pid] = {
                "waypoint_id": poi.id,
                "name": poi_name,
                "description": poi.user_description or "",
                "approaching": {"text": "", "edited": False},
                "at_poi": {"text": "", "edited": False}
            }
        else:
            knowledge["pois"][pid]["name"] = poi_name
            knowledge["pois"][pid]["description"] = poi.user_description or ""

    logger.info(f"Loaded knowledge for track {track_id} from content-api (exists={content_knowledge is not None})")

    # Check if any knowledge exists (v4: texts under narrations.<character>.<type>)
    def _has_narration_texts(items, text_keys):
        for item in items.values():
            # v4 format: narrations.character_id.text_type.text
            narrations = item.get("narrations", {})
            for char_narr in narrations.values():
                for key in text_keys:
                    if char_narr.get(key, {}).get("text"):
                        return True
            # v3 compat: direct text_type.text
            for key in text_keys:
                if item.get(key, {}).get("text"):
                    return True
        return False

    has_route_texts = _has_narration_texts(knowledge.get("routes", {}), ["intro", "outro"])
    has_segment_texts = _has_narration_texts(knowledge.get("segments", {}), ["entry", "exit"])
    has_poi_texts = _has_narration_texts(knowledge.get("pois", {}), ["approaching", "at_poi"])

    # ── v4 → v3 flattening ──────────────────────────────────────
    # The v4 format stores texts under narrations.<character_id>.<type>
    # but all frontends expect v3 format: item.<type>.text directly.
    # Flatten the selected character's narrations for backward compatibility.
    from fastapi import Query as FastQuery
    character_id = None  # Will be set from query param in future
    if knowledge.get("version", 3) >= 4 and knowledge.get("characters"):
        characters = knowledge.get("characters", [])
        # Find default character or first one
        default_char = next((c for c in characters if c.get("is_default")), characters[0] if characters else None)
        char_id = character_id or (default_char["id"] if default_char else None)
        
        if char_id:
            # Flatten routes
            for rid, rdata in knowledge.get("routes", {}).items():
                narrations = rdata.pop("narrations", {})
                char_narr = narrations.get(char_id, {})
                for key in ["intro", "outro"]:
                    if key not in rdata or not rdata[key].get("text"):
                        rdata[key] = char_narr.get(key, {"text": "", "edited": False})
            
            # Flatten segments
            for sname, sdata in knowledge.get("segments", {}).items():
                narrations = sdata.pop("narrations", {})
                char_narr = narrations.get(char_id, {})
                for key in ["entry", "exit"]:
                    if key not in sdata or not sdata[key].get("text"):
                        sdata[key] = char_narr.get(key, {"text": "", "edited": False})
            
            # Flatten POIs
            for pid, pdata in knowledge.get("pois", {}).items():
                narrations = pdata.pop("narrations", {})
                char_narr = narrations.get(char_id, {})
                for key in ["approaching", "at_poi"]:
                    if key not in pdata or not pdata[key].get("text"):
                        pdata[key] = char_narr.get(key, {"text": "", "edited": False})
            
            # Add character info to config for frontends
            knowledge.setdefault("config", {})["active_character"] = char_id
            knowledge["config"]["available_characters"] = [
                {"id": c["id"], "name": c["name"]} for c in characters
            ]
    
    return {
        "exists": has_route_texts or has_segment_texts or has_poi_texts,
        "knowledge": knowledge,
        "track_id": track_id,
        "track_name": track.name
    }


# ============ Knowledge Version Endpoint (for Cache Validation) ============

def _compute_knowledge_hash(db: Session, track_id: int, data: Dict) -> str:
    """
    Compute SHA256 hash of all knowledge content for cache validation.

    Includes all text content in deterministic order:
    - Track config
    - Route intro/outro texts
    - Segment entry/exit texts
    - POI approaching/at_poi texts
    - All audio storage IDs
    """
    import hashlib

    hash_parts = []

    # 1. Track config
    track = db.query(Track).filter(Track.id == track_id).first()
    if track:
        config = (track.metadata_json or {}).get("knowledge_config", {})
        if config:
            hash_parts.append(f"CONFIG:{json.dumps(config, sort_keys=True)}")

    # 2. Routes (sorted by ID)
    for route in sorted(data["routes"], key=lambda r: r.id):
        route_metadata = route.metadata_json or {}
        route_knowledge = route_metadata.get("knowledge", {})

        intro = route_knowledge.get("intro", {})
        outro = route_knowledge.get("outro", {})

        intro_text = intro.get("text", "")
        outro_text = outro.get("text", "")
        intro_cues = intro.get("cues", [])
        outro_cues = outro.get("cues", [])

        if intro_text:
            hash_parts.append(f"R{route.id}_intro:{intro_text}")
        if outro_text:
            hash_parts.append(f"R{route.id}_outro:{outro_text}")

        # Include audio storage IDs
        for cue in intro_cues:
            if cue.get("audio_storage_id"):
                hash_parts.append(f"R{route.id}_intro_audio:{cue['audio_storage_id']}")
        for cue in outro_cues:
            if cue.get("audio_storage_id"):
                hash_parts.append(f"R{route.id}_outro_audio:{cue['audio_storage_id']}")

    # 3. Segments (sorted by name)
    for seg_name in sorted(data["segments"].keys()):
        seg_data = data["segments"][seg_name]
        start_wp = seg_data.get("start_wp")
        end_wp = seg_data.get("end_wp")

        if start_wp:
            start_knowledge = (start_wp.metadata_json or {}).get("knowledge", {})
            entry = start_knowledge.get("entry", {})
            entry_text = entry.get("text", "")
            entry_cues = entry.get("cues", [])

            if entry_text:
                hash_parts.append(f"S{seg_name}_entry:{entry_text}")
            for cue in entry_cues:
                if cue.get("audio_storage_id"):
                    hash_parts.append(f"S{seg_name}_entry_audio:{cue['audio_storage_id']}")

        if end_wp:
            end_knowledge = (end_wp.metadata_json or {}).get("knowledge", {})
            exit_data = end_knowledge.get("exit", {})
            exit_text = exit_data.get("text", "")
            exit_cues = exit_data.get("cues", [])

            if exit_text:
                hash_parts.append(f"S{seg_name}_exit:{exit_text}")
            for cue in exit_cues:
                if cue.get("audio_storage_id"):
                    hash_parts.append(f"S{seg_name}_exit_audio:{cue['audio_storage_id']}")

    # 4. POIs (sorted by ID)
    for poi in sorted(data["pois"], key=lambda p: p.id):
        poi_knowledge = (poi.metadata_json or {}).get("knowledge", {})

        approaching = poi_knowledge.get("approaching", {})
        at_poi = poi_knowledge.get("at_poi", {})

        approaching_text = approaching.get("text", "")
        at_poi_text = at_poi.get("text", "")
        approaching_cues = approaching.get("cues", [])
        at_poi_cues = at_poi.get("cues", [])

        if approaching_text:
            hash_parts.append(f"P{poi.id}_approaching:{approaching_text}")
        if at_poi_text:
            hash_parts.append(f"P{poi.id}_at_poi:{at_poi_text}")

        for cue in approaching_cues:
            if cue.get("audio_storage_id"):
                hash_parts.append(f"P{poi.id}_approaching_audio:{cue['audio_storage_id']}")
        for cue in at_poi_cues:
            if cue.get("audio_storage_id"):
                hash_parts.append(f"P{poi.id}_at_poi_audio:{cue['audio_storage_id']}")

    # Combine and hash
    combined = "\n".join(hash_parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def _count_audio_cues(data: Dict) -> tuple:
    """
    Count total audio cues and collect all storage IDs.

    Returns: (audio_count, storage_ids_list)
    """
    audio_count = 0
    storage_ids = []

    # Routes
    for route in data["routes"]:
        route_metadata = route.metadata_json or {}
        route_knowledge = route_metadata.get("knowledge", {})

        for text_type in ["intro", "outro"]:
            text_data = route_knowledge.get(text_type, {})
            cues = text_data.get("cues", [])
            for cue in cues:
                if cue.get("audio_storage_id"):
                    audio_count += 1
                    storage_ids.append(cue["audio_storage_id"])

    # Segments
    for seg_name, seg_data in data["segments"].items():
        for wp in [seg_data.get("start_wp"), seg_data.get("end_wp")]:
            if wp:
                wp_knowledge = (wp.metadata_json or {}).get("knowledge", {})
                for text_type in ["entry", "exit"]:
                    text_data = wp_knowledge.get(text_type, {})
                    cues = text_data.get("cues", [])
                    for cue in cues:
                        if cue.get("audio_storage_id"):
                            audio_count += 1
                            storage_ids.append(cue["audio_storage_id"])

    # POIs
    for poi in data["pois"]:
        poi_knowledge = (poi.metadata_json or {}).get("knowledge", {})
        for text_type in ["approaching", "at_poi"]:
            text_data = poi_knowledge.get(text_type, {})
            cues = text_data.get("cues", [])
            for cue in cues:
                if cue.get("audio_storage_id"):
                    audio_count += 1
                    storage_ids.append(cue["audio_storage_id"])

    return audio_count, storage_ids


@router.get("/{track_id}/knowledge/version")
def get_knowledge_version(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get lightweight version info for track knowledge (for cache validation).

    Returns content hash and counts without full knowledge data.
    Use this to check if cached knowledge is still valid.

    Response:
    - content_hash: SHA256 hash of all knowledge content
    - last_modified: Track's last update timestamp
    - poi_count: Number of POIs with knowledge
    - audio_count: Number of generated audio cues
    - has_knowledge: Whether any knowledge exists
    - storage_ids: List of all audio storage IDs (for cache diffing)
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.visibility == "private" and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Load track data
    data = _load_track_data(db, track_id)

    # Compute hash
    content_hash = _compute_knowledge_hash(db, track_id, data)

    # Count audio
    audio_count, storage_ids = _count_audio_cues(data)

    # Check if knowledge exists (same logic as get_track_knowledge)
    has_route_texts = False
    has_segment_texts = False
    has_poi_texts = False

    for route in data["routes"]:
        route_metadata = route.metadata_json or {}
        route_knowledge = route_metadata.get("knowledge", {})
        if route_knowledge.get("intro", {}).get("text") or route_knowledge.get("outro", {}).get("text"):
            has_route_texts = True
            break

    for seg_name, seg_data in data["segments"].items():
        start_wp = seg_data.get("start_wp")
        end_wp = seg_data.get("end_wp")
        start_knowledge = _get_waypoint_knowledge(start_wp) or {}
        end_knowledge = _get_waypoint_knowledge(end_wp) or {}
        if start_knowledge.get("entry", {}).get("text") or end_knowledge.get("exit", {}).get("text"):
            has_segment_texts = True
            break

    for poi in data["pois"]:
        poi_knowledge = _get_waypoint_knowledge(poi) or {}
        if poi_knowledge.get("approaching", {}).get("text") or poi_knowledge.get("at_poi", {}).get("text"):
            has_poi_texts = True
            break

    return {
        "track_id": track_id,
        "content_hash": content_hash,
        "last_modified": track.updated_at.isoformat() + "Z" if track.updated_at else None,
        "poi_count": len(data["pois"]),
        "route_count": len(data["routes"]),
        "segment_count": len(data["segments"]),
        "audio_count": audio_count,
        "storage_ids": storage_ids,
        "has_knowledge": has_route_texts or has_segment_texts or has_poi_texts
    }


async def _run_generation_job(job_id: str, track_id: int, body_dict: dict, user_id: int):
    """Background task that runs the actual generation."""
    job = _get_job(job_id)
    if not job:
        logger.error(f"Job {job_id} not found at start of background task")
        return

    try:
        db = SessionLocal()
        track = db.query(Track).filter(Track.id == track_id).first()
        data = _load_track_data(db, track_id)

        config = KnowledgeConfig(
            persona=body_dict.get("persona", ""),
            target_audience=body_dict.get("target_audience", ""),
            language=body_dict.get("language", "de"),
            tone=body_dict.get("tone", "friendly"),
            background_knowledge=body_dict.get("background_knowledge", "")
        )

        only_missing = body_dict.get("only_missing", False)

        # Load existing knowledge from content-api if only_missing mode
        existing_knowledge = {"routes": {}, "segments": {}, "pois": {}}
        if only_missing:
            content_knowledge = get_narration_knowledge(track_id)
            if content_knowledge:
                for rid, rdata in content_knowledge.get("routes", {}).items():
                    existing_knowledge["routes"][rid] = {
                        "intro": rdata.get("intro", {}).get("text", "") if isinstance(rdata.get("intro"), dict) else "",
                        "outro": rdata.get("outro", {}).get("text", "") if isinstance(rdata.get("outro"), dict) else ""
                    }
                for sname, sdata in content_knowledge.get("segments", {}).items():
                    existing_knowledge["segments"][sname] = {
                        "entry": sdata.get("entry", {}).get("text", "") if isinstance(sdata.get("entry"), dict) else "",
                        "exit": sdata.get("exit", {}).get("text", "") if isinstance(sdata.get("exit"), dict) else ""
                    }
                for pid, pdata in content_knowledge.get("pois", {}).items():
                    existing_knowledge["pois"][pid] = {
                        "approaching": pdata.get("approaching", {}).get("text", "") if isinstance(pdata.get("approaching"), dict) else "",
                        "at_poi": pdata.get("at_poi", {}).get("text", "") if isinstance(pdata.get("at_poi"), dict) else ""
                    }
            logger.info(f"Generation job {job_id}: loaded existing knowledge from content-api")

        knowledge = {
            "version": 3,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "config": config.model_dump(),
            "routes": {},
            "segments": {},
            "pois": {}
        }

        # If only_missing, pre-populate from content-api
        if only_missing and content_knowledge:
            for route in data["routes"]:
                rid = str(route.id)
                ck_route = content_knowledge.get("routes", {}).get(rid, {})
                knowledge["routes"][rid] = {
                    "id": route.id,
                    "name": route.name,
                    "description": route.description or "",
                    "intro": ck_route.get("intro", {"text": "", "text_original": "", "edited": False}),
                    "outro": ck_route.get("outro", {"text": "", "text_original": "", "edited": False})
                }

            for seg_name, seg_data in data["segments"].items():
                start_wp = seg_data.get("start_wp")
                end_wp = seg_data.get("end_wp")
                ck_seg = content_knowledge.get("segments", {}).get(seg_name, {})
                knowledge["segments"][seg_name] = {
                    "name": seg_name,
                    "description": seg_data.get("description", ""),
                    "start_waypoint_id": start_wp.id if start_wp else None,
                    "end_waypoint_id": end_wp.id if end_wp else None,
                    "entry": ck_seg.get("entry", {"text": "", "text_original": "", "edited": False}),
                    "exit": ck_seg.get("exit", {"text": "", "text_original": "", "edited": False})
                }

            for poi in data["pois"]:
                pid = str(poi.id)
                poi_name = (poi.metadata_json or {}).get("title", f"POI #{poi.id}")
                ck_poi = content_knowledge.get("pois", {}).get(pid, {})
                knowledge["pois"][pid] = {
                    "waypoint_id": poi.id,
                    "name": poi_name,
                    "description": poi.user_description or "",
                    "approaching": ck_poi.get("approaching", {"text": "", "text_original": "", "edited": False}),
                    "at_poi": ck_poi.get("at_poi", {"text": "", "text_original": "", "edited": False})
                }

        # Build task list
        task_list = []
        skipped_count = 0

        logger.info(f"Generation job {job_id}: only_missing={only_missing}")

        if body_dict.get("generate_routes", True):
            for route in data["routes"]:
                gps_count = db.query(Waypoint).filter(
                    Waypoint.track_id == track_id,
                    Waypoint.waypoint_type == "gps_track",
                    Waypoint.route_id == route.id
                ).count()
                route_length_km = (gps_count * 10) / 1000

                # Get text values (handle both string and dict formats)
                existing_intro_raw = existing_knowledge["routes"].get(str(route.id), {}).get("intro", "")
                existing_outro_raw = existing_knowledge["routes"].get(str(route.id), {}).get("outro", "")
                existing_intro = existing_intro_raw.get("text", "") if isinstance(existing_intro_raw, dict) else existing_intro_raw
                existing_outro = existing_outro_raw.get("text", "") if isinstance(existing_outro_raw, dict) else existing_outro_raw

                # Check if intro already exists
                if not only_missing or not existing_intro:
                    task_list.append(("route", str(route.id), "intro", {
                        "route_name": route.name,
                        "route_description": route.description or "",
                        "route_length_km": route_length_km
                    }, route.name, route.id))
                else:
                    skipped_count += 1

                # Check if outro already exists
                if not only_missing or not existing_outro:
                    task_list.append(("route", str(route.id), "outro", {
                        "route_name": route.name,
                        "route_length_km": route_length_km
                    }, route.name, route.id))
                else:
                    skipped_count += 1

        if body_dict.get("generate_segments", True):
            for seg_name, seg_data in data["segments"].items():
                # Get text values (handle both string and dict formats)
                existing_entry_raw = existing_knowledge["segments"].get(seg_name, {}).get("entry", "")
                existing_exit_raw = existing_knowledge["segments"].get(seg_name, {}).get("exit", "")
                existing_entry = existing_entry_raw.get("text", "") if isinstance(existing_entry_raw, dict) else existing_entry_raw
                existing_exit = existing_exit_raw.get("text", "") if isinstance(existing_exit_raw, dict) else existing_exit_raw

                # Check if entry already exists
                if not only_missing or not existing_entry:
                    task_list.append(("segment", seg_name, "entry", {
                        "segment_name": seg_name,
                        "segment_description": seg_data.get("description", "")
                    }, seg_data, None))
                else:
                    skipped_count += 1

                # Check if exit already exists
                if not only_missing or not existing_exit:
                    task_list.append(("segment", seg_name, "exit", {
                        "segment_name": seg_name
                    }, seg_data, None))
                else:
                    skipped_count += 1

        if body_dict.get("generate_pois", True):
            for poi in data["pois"]:
                poi_name = (poi.metadata_json or {}).get("title", f"POI #{poi.id}")
                poi_description = poi.user_description or ""

                # Get text values (handle both string and dict formats)
                existing_approaching_raw = existing_knowledge["pois"].get(str(poi.id), {}).get("approaching", "")
                existing_at_poi_raw = existing_knowledge["pois"].get(str(poi.id), {}).get("at_poi", "")

                # Extract text if it's a dict
                existing_approaching = existing_approaching_raw.get("text", "") if isinstance(existing_approaching_raw, dict) else existing_approaching_raw
                existing_at_poi = existing_at_poi_raw.get("text", "") if isinstance(existing_at_poi_raw, dict) else existing_at_poi_raw

                logger.info(f"POI {poi.id} ({poi_name}): approaching='{existing_approaching[:30] if existing_approaching else ''}', at_poi='{existing_at_poi[:30] if existing_at_poi else ''}'")

                # Check if approaching already exists
                if not only_missing or not existing_approaching:
                    task_list.append(("poi", str(poi.id), "approaching", {
                        "poi_name": poi_name,
                        "poi_description": poi_description
                    }, poi_name, poi.id))
                else:
                    skipped_count += 1

                # Check if at_poi already exists
                if not only_missing or not existing_at_poi:
                    task_list.append(("poi", str(poi.id), "at_poi", {
                        "poi_name": poi_name,
                        "poi_description": poi_description
                    }, poi_name, poi.id))
                else:
                    skipped_count += 1

        db.close()

        logger.info(f"Generation job {job_id}: {len(task_list)} tasks to generate, {skipped_count} skipped (only_missing={only_missing})")

        job["total"] = len(task_list)
        job["status"] = "running"
        _set_job(job_id, job)

        # Process sequentially
        error_count = 0
        for i, task_info in enumerate(task_list):
            item_type, item_id, text_type, prompt_data, extra1, extra2 = task_info

            try:
                # Build correct prompt type key
                # Route: route_intro, route_outro
                # Segment: segment_entry, segment_exit
                # POI: poi_approaching, poi_at (NOT poi_at_poi!)
                if item_type == "route":
                    prompt_type = f"route_{text_type}"
                elif item_type == "poi" and text_type == "at_poi":
                    prompt_type = "poi_at"  # Fix: prompts dict uses "poi_at" not "poi_at_poi"
                else:
                    prompt_type = f"{item_type}_{text_type}"

                logger.info(f"Generating {item_type} {item_id} {text_type} with prompt_type={prompt_type}")
                text = await _generate_narrative_text(prompt_type, prompt_data, config)

                if not text:
                    logger.warning(f"Empty text generated for {item_type} {item_id} {text_type}")
            except Exception as e:
                logger.warning(f"Generation failed for {item_type} {item_id} {text_type}: {e}")
                text = ""
                error_count += 1

            # Store result
            if item_type == "route":
                if item_id not in knowledge["routes"]:
                    knowledge["routes"][item_id] = {
                        "id": extra2,
                        "name": extra1,
                        "intro": {"text": "", "text_original": "", "edited": False},
                        "outro": {"text": "", "text_original": "", "edited": False}
                    }
                knowledge["routes"][item_id][text_type] = {
                    "text": text, "text_original": text, "edited": False
                }

            elif item_type == "segment":
                seg_data = extra1
                if item_id not in knowledge["segments"]:
                    knowledge["segments"][item_id] = {
                        "name": item_id,
                        "start_waypoint_id": seg_data["start_wp"].id if seg_data.get("start_wp") else None,
                        "end_waypoint_id": seg_data["end_wp"].id if seg_data.get("end_wp") else None,
                        "entry": {"text": "", "text_original": "", "edited": False},
                        "exit": {"text": "", "text_original": "", "edited": False}
                    }
                knowledge["segments"][item_id][text_type] = {
                    "text": text, "text_original": text, "edited": False
                }

            elif item_type == "poi":
                if item_id not in knowledge["pois"]:
                    knowledge["pois"][item_id] = {
                        "waypoint_id": extra2,
                        "name": extra1,
                        "approaching": {"text": "", "text_original": "", "edited": False},
                        "at_poi": {"text": "", "text_original": "", "edited": False}
                    }
                knowledge["pois"][item_id][text_type] = {
                    "text": text, "text_original": text, "edited": False
                }

            # Update progress every 5 items (reduce file I/O)
            if (i + 1) % 5 == 0 or i == len(task_list) - 1:
                job["completed"] = i + 1
                job["current_item"] = f"{item_type}: {extra1 if item_type != 'segment' else item_id}"
                _set_job(job_id, job)

        job["status"] = "completed"
        job["knowledge"] = knowledge
        job["stats"] = {
            "routes_count": len(knowledge["routes"]),
            "segments_count": len(knowledge["segments"]),
            "pois_count": len(knowledge["pois"]),
            "total_texts": len(task_list),
            "successful_texts": len(task_list) - error_count,
            "failed_texts": error_count
        }
        _set_job(job_id, job)

    except Exception as e:
        logger.error(f"Generation job {job_id} failed: {e}")
        job["status"] = "failed"
        job["error"] = str(e)
        _set_job(job_id, job)


@router.post("/{track_id}/knowledge/generate")
async def start_generate_track_knowledge(
    track_id: int,
    body: GenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Start background generation of narrative texts.

    Returns a job_id immediately. Poll /generate/status?job_id=xxx for progress.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can generate knowledge")

    # Create job
    job_id = str(uuid.uuid4())
    job_data = {
        "status": "starting",
        "track_id": track_id,
        "completed": 0,
        "total": 0,
        "current_item": "",
        "knowledge": None,
        "stats": None,
        "error": None
    }
    _set_job(job_id, job_data)

    # Start background task
    background_tasks.add_task(
        _run_generation_job,
        job_id,
        track_id,
        body.model_dump(),
        current_user.id
    )

    return {
        "success": True,
        "job_id": job_id,
        "message": "Generation started. Poll /generate/status for progress."
    }


@router.get("/{track_id}/knowledge/generate-status")
async def get_generation_status(
    track_id: int,
    job_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of a background generation job.

    Returns progress, and when complete, the generated knowledge.
    """
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["track_id"] != track_id:
        raise HTTPException(status_code=403, detail="Job does not belong to this track")

    response = {
        "status": job["status"],
        "completed": job["completed"],
        "total": job["total"],
        "current_item": job["current_item"],
        "progress_percent": round(job["completed"] / max(job["total"], 1) * 100, 1)
    }

    if job["status"] == "completed":
        response["knowledge"] = job["knowledge"]
        response["stats"] = job["stats"]
        # Clean up old job after retrieval
        # del _generation_jobs[job_id]

    if job["status"] == "failed":
        response["error"] = job["error"]

    return response


# Keep old synchronous endpoint for small generations (backwards compatibility)
@router.post("/{track_id}/knowledge/generate-sync")
async def generate_track_knowledge_sync(
    track_id: int,
    body: GenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Synchronous generation (for small tracks only, may timeout on large tracks).
    Use /generate for large tracks with background processing.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can generate knowledge")

    data = _load_track_data(db, track_id)

    # Count items
    total_items = 0
    if body.generate_routes:
        total_items += len(data["routes"]) * 2
    if body.generate_segments:
        total_items += len(data["segments"]) * 2
    if body.generate_pois:
        total_items += len(data["pois"]) * 2

    if total_items > 20:
        raise HTTPException(
            status_code=400,
            detail=f"Too many items ({total_items}). Use /generate for background processing."
        )

    # Run synchronously for small tracks
    config = KnowledgeConfig(
        persona=body.persona,
        target_audience=body.target_audience,
        language=body.language,
        tone=body.tone,
        background_knowledge=body.background_knowledge
    )

    knowledge = {
        "version": 3,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "config": config.model_dump(),
        "routes": {},
        "segments": {},
        "pois": {}
    }

    # Generate routes
    if body.generate_routes:
        for route in data["routes"]:
            intro = await _generate_narrative_text("route_intro", {"route_name": route.name}, config)
            outro = await _generate_narrative_text("route_outro", {"route_name": route.name}, config)
            knowledge["routes"][str(route.id)] = {
                "id": route.id,
                "name": route.name,
                "intro": {"text": intro, "text_original": intro, "edited": False},
                "outro": {"text": outro, "text_original": outro, "edited": False}
            }

    return {
        "success": True,
        "knowledge": knowledge,
        "stats": {
            "routes_count": len(knowledge["routes"]),
            "segments_count": len(knowledge["segments"]),
            "pois_count": len(knowledge["pois"]),
            "total_texts": total_items
        }
    }


@router.put("/{track_id}/knowledge")
def save_track_knowledge(
    track_id: int,
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save all knowledge for a track.

    All narrative texts are stored exclusively in content-api.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can save knowledge")

    knowledge = body.get("knowledge")
    if not knowledge:
        raise HTTPException(status_code=400, detail="Knowledge object required")

    # ── v3 → v4 wrapping ──────────────────────────────────────
    # If the frontend sends v3 format (flat texts), wrap into v4 narrations
    if knowledge.get("version", 3) < 4:
        # Detect active character from config or default
        char_id = (knowledge.get("config") or {}).get("active_character", "dr_tschauko")
        
        for rid, rdata in knowledge.get("routes", {}).items():
            if "narrations" not in rdata:
                rdata["narrations"] = {char_id: {}}
            char_narr = rdata["narrations"].setdefault(char_id, {})
            for key in ["intro", "outro"]:
                if key in rdata and isinstance(rdata[key], dict) and rdata[key].get("text"):
                    char_narr[key] = rdata[key]  # Keep flat key for content-frontend compat
        
        for sname, sdata in knowledge.get("segments", {}).items():
            if "narrations" not in sdata:
                sdata["narrations"] = {char_id: {}}
            char_narr = sdata["narrations"].setdefault(char_id, {})
            for key in ["entry", "exit"]:
                if key in sdata and isinstance(sdata[key], dict) and sdata[key].get("text"):
                    char_narr[key] = sdata[key]  # Keep flat key for content-frontend compat
        
        for pid, pdata in knowledge.get("pois", {}).items():
            if "narrations" not in pdata:
                pdata["narrations"] = {char_id: {}}
            char_narr = pdata["narrations"].setdefault(char_id, {})
            for key in ["approaching", "at_poi"]:
                if key in pdata and isinstance(pdata[key], dict) and pdata[key].get("text"):
                    char_narr[key] = pdata[key]  # Keep flat key for content-frontend compat
        
        # Upgrade version
        knowledge["version"] = 4
        # Add characters if missing
        if "characters" not in knowledge:
            persona = (knowledge.get("config") or {}).get("persona", "")
            knowledge["characters"] = [{
                "id": char_id,
                "name": "Dr. Peter Tschauko",
                "persona": persona,
                "is_default": True
            }]
        
        logger.info(f"Upgraded v3 knowledge to v4 for track {track_id} (character: {char_id})")

    # Save to content-api (single source of truth)
    content_post_id = save_narration_knowledge(track_id, track.name, knowledge)
    if not content_post_id:
        raise HTTPException(status_code=502, detail="Failed to save knowledge to content-api")

    logger.info(f"Saved knowledge to content-api post {content_post_id} for track {track_id}")

    return {
        "success": True,
        "track_id": track_id,
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "saved_routes": len(knowledge.get("routes", {})),
        "saved_segments": len(knowledge.get("segments", {})),
        "saved_pois": len(knowledge.get("pois", {})),
        "content_post_id": content_post_id
    }


@router.post("/{track_id}/knowledge/audio")
async def generate_knowledge_audio(
    track_id: int,
    body: AudioGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Generate TTS audio for a single knowledge item with cue splitting.

    Workflow:
    1. Split text into 3-5 cues using AI
    2. Generate TTS audio for each cue
    3. Store cues with their audio_storage_ids
    4. Return array of cues

    item_type: "route", "segment", "poi"
    item_id: route_id (for route), segment_name (for segment), waypoint_id (for poi)
    text_type: "intro", "outro", "entry", "exit", "approaching", "at_poi"
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can generate audio")

    # Validate combinations
    valid_combinations = {
        "route": ["intro", "outro"],
        "segment": ["entry", "exit"],
        "poi": ["approaching", "at_poi"]
    }

    if body.item_type not in valid_combinations:
        raise HTTPException(status_code=400, detail=f"Invalid item_type: {body.item_type}")

    if body.text_type not in valid_combinations[body.item_type]:
        raise HTTPException(status_code=400, detail=f"Invalid text_type for {body.item_type}")

    if not body.item_id:
        raise HTTPException(status_code=400, detail="item_id required")

    # Get text from content-api
    content_knowledge = get_narration_knowledge(track_id)
    if not content_knowledge:
        raise HTTPException(status_code=404, detail="No knowledge found for this track")

    text = None
    source_id = None
    section_map = {"route": "routes", "segment": "segments", "poi": "pois"}
    section = section_map.get(body.item_type)

    if section and body.item_id in content_knowledge.get(section, {}):
        item_data = content_knowledge[section][body.item_id]
        text_data = item_data.get(body.text_type, {})
        text = text_data.get("text", "") if isinstance(text_data, dict) else ""
        source_id = f"{body.item_type}_{body.item_id}_{body.text_type}"

    if not text:
        raise HTTPException(status_code=400, detail="No text found to generate audio from")

    # Step 1: Split text into cues
    logger.info(f"Splitting text into cues for {source_id}")
    cue_texts = await _split_text_into_cues(text, body.language)
    logger.info(f"Split into {len(cue_texts)} cues")

    # Step 2: Generate TTS for each cue sequentially
    cues_result = []
    total_duration = 0.0

    async with httpx.AsyncClient() as client:
        for idx, cue_text in enumerate(cue_texts):
            job_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat() + "Z"

            dialog_payload = {
                "id": job_id,
                "type": "speech_request",
                "timestamp": timestamp,
                "content": {
                    "text": cue_text,
                    "language": body.language,
                    "speed": 1.0,
                    "voice": body.voice
                },
                "config": {
                    "provider": "openai",
                    "output_format": "mp3",
                    "dialog_mode": True,
                    "add_music": body.add_music if idx == 0 else False,  # Music only on first cue
                    "add_sfx": False,
                    "analyze_only": False,
                    "voice_mapping": {"Narrator": body.voice}
                },
                "save_options": {
                    "is_public": True,
                    "link_id": f"knowledge;{track_id};{source_id};cue{idx}",
                    "collection_id": f"artrack-knowledge:{track_id}"
                }
            }

            try:
                response = await client.post(
                    f"{AI_API_BASE}/ai/dialog/start",
                    json=dialog_payload,
                    headers={"X-API-KEY": INTERNAL_API_KEY, "Content-Type": "application/json"},
                    timeout=30.0
                )

                if response.status_code != 200:
                    logger.error(f"Dialog API failed for cue {idx}: {response.status_code}")
                    cues_result.append({
                        "index": idx,
                        "text": cue_text,
                        "audio_storage_id": None,
                        "duration_seconds": None,
                        "error": "Failed to start audio generation"
                    })
                    continue

                # Poll for completion
                max_wait = 60  # 60 seconds per cue
                elapsed = 0
                cue_done = False

                while elapsed < max_wait and not cue_done:
                    await asyncio.sleep(2)
                    elapsed += 2

                    status_resp = await client.get(
                        f"{AI_API_BASE}/ai/dialog/status",
                        params={"id": job_id},
                        headers={"X-API-KEY": INTERNAL_API_KEY},
                        timeout=30.0
                    )

                    if status_resp.status_code != 200:
                        continue

                    status_data = status_resp.json()
                    phase = status_data.get("phase", "")

                    if phase == "done":
                        result = status_data.get("result", {})
                        storage_id = result.get("id")
                        audio_url = result.get("url") or result.get("file_url")
                        duration = result.get("duration_seconds", 0)

                        cues_result.append({
                            "index": idx,
                            "text": cue_text,
                            "audio_storage_id": storage_id,
                            "audio_url": audio_url,
                            "duration_seconds": duration
                        })
                        total_duration += duration or 0
                        cue_done = True
                        logger.info(f"Cue {idx} done: storage_id={storage_id}, duration={duration}s")

                    elif phase == "error":
                        cues_result.append({
                            "index": idx,
                            "text": cue_text,
                            "audio_storage_id": None,
                            "duration_seconds": None,
                            "error": "Audio generation failed"
                        })
                        cue_done = True

                if not cue_done:
                    cues_result.append({
                        "index": idx,
                        "text": cue_text,
                        "audio_storage_id": None,
                        "duration_seconds": None,
                        "error": "Audio generation timed out"
                    })

            except httpx.RequestError as e:
                logger.error(f"Dialog API request failed for cue {idx}: {e}")
                cues_result.append({
                    "index": idx,
                    "text": cue_text,
                    "audio_storage_id": None,
                    "duration_seconds": None,
                    "error": str(e)
                })

    # Step 3: Update content-api with cue data
    cue_data = [
        {
            "index": c["index"],
            "text": c["text"],
            "audio_storage_id": c.get("audio_storage_id"),
            "duration_seconds": c.get("duration_seconds")
        }
        for c in cues_result
    ]

    if section and body.item_id in content_knowledge.get(section, {}):
        item_data = content_knowledge[section][body.item_id]
        if body.text_type in item_data:
            item_data[body.text_type]["cues"] = cue_data
            item_data[body.text_type]["total_duration"] = total_duration
        save_narration_knowledge(track_id, track.name, content_knowledge)
        logger.info(f"Saved audio cues to content-api for {body.item_type} {body.item_id}")

    # Count successful cues
    successful_cues = sum(1 for c in cues_result if c.get("audio_storage_id"))

    return {
        "success": successful_cues > 0,
        "item_type": body.item_type,
        "item_id": body.item_id,
        "text_type": body.text_type,
        "cues_count": len(cues_result),
        "successful_cues": successful_cues,
        "total_duration_seconds": total_duration,
        "cues": cues_result
    }


@router.delete("/{track_id}/knowledge")
def delete_track_knowledge(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete all knowledge for a track."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can delete knowledge")

    # Delete from content-api (single source of truth)
    deleted = delete_narration_post(track_id)
    if not deleted:
        raise HTTPException(status_code=502, detail="Failed to delete knowledge from content-api")

    logger.info(f"Deleted narration from content-api for track {track_id}")
    return {"success": True, "deleted": True}
