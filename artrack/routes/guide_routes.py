from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, Literal, Dict, Any, List
from math import radians, cos, sin, asin, sqrt
import logging

from ..database import get_db
from ..models import Track, Waypoint, TrackRoute, StorageObject
from ..auth import get_current_user, User

from ..services.route_status import guide_routes
router = APIRouter()
logger = logging.getLogger(__name__)


def _load_storage_objects(db: Session, storage_object_ids: List[int], storage_collection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Load storage objects from IDs or collection names.
    Only returns: images (jpg, png, webp), PDFs, MD, JSON.

    Returns: [{ "id": int, "url": str, "type": str, "filename": str, "mime_type": str }]
    """
    storage_items = []

    # Load from storage_object_ids
    if storage_object_ids:
        objects = db.query(StorageObject).filter(StorageObject.id.in_(storage_object_ids)).all()
        for obj in objects:
            # Filter by mime type: images, pdf, markdown, json
            mime = (obj.mime_type or "").lower()
            if any(t in mime for t in ["image/", "pdf", "markdown", "json", "text/plain"]):
                # Determine type from mime
                if "image/" in mime:
                    obj_type = "image"
                elif "pdf" in mime:
                    obj_type = "pdf"
                elif "markdown" in mime or obj.original_filename.endswith(".md"):
                    obj_type = "markdown"
                elif "json" in mime:
                    obj_type = "json"
                else:
                    obj_type = "document"

                storage_items.append({
                    "id": obj.id,
                    "url": obj.file_url,
                    "type": obj_type,
                    "filename": obj.original_filename,
                    "mime_type": obj.mime_type
                })

    # Load from storage_collection (collection_id + optional owner_email)
    if storage_collection:
        collection_id = storage_collection.get("name") or storage_collection.get("id")
        owner_email = storage_collection.get("owner_email")

        if collection_id:
            query = db.query(StorageObject).filter(StorageObject.collection_id == collection_id)
            if owner_email:
                # Join with User to filter by owner email (optional)
                from ..models import User
                query = query.join(User, StorageObject.owner_user_id == User.id).filter(User.email == owner_email)

            objects = query.all()
            for obj in objects:
                # Filter by mime type
                mime = (obj.mime_type or "").lower()
                if any(t in mime for t in ["image/", "pdf", "markdown", "json", "text/plain"]):
                    if "image/" in mime:
                        obj_type = "image"
                    elif "pdf" in mime:
                        obj_type = "pdf"
                    elif "markdown" in mime or obj.original_filename.endswith(".md"):
                        obj_type = "markdown"
                    elif "json" in mime:
                        obj_type = "json"
                    else:
                        obj_type = "document"

                    # Avoid duplicates (already loaded from IDs)
                    if not any(s["id"] == obj.id for s in storage_items):
                        storage_items.append({
                            "id": obj.id,
                            "url": obj.file_url,
                            "type": obj_type,
                            "filename": obj.original_filename,
                            "mime_type": obj.mime_type
                        })

    return storage_items


def _infer_category_from_title(title: Optional[str]) -> str:
    t = (title or "").lower()
    if t.startswith("aussichtspunkt") or "aussicht" in t:
        return "aussichtspunkt"
    if "sehens" in t:
        return "sehenswuerdigkeit"
    if "treppe" in t or "stairs" in t:
        return "treppe"
    if "bank" in t or "bench" in t:
        return "bank"
    return "misc"


def _map_category_to_domain(cat: str) -> str:
    c = (cat or "").lower()
    if "aussicht" in c:
        return "scenic"
    if "sehens" in c:
        return "historical"
    if "treppe" in c or "stairs" in c:
        return "workstation"
    if "bank" in c or "bench" in c:
        return "social"
    return "social"


def _map_category_to_style(cat: str) -> str:
    if cat == "treppe":
        return "technical"
    if cat == "sehenswuerdigkeit":
        return "detailed"
    return "storytelling"


@router.post("/{track_id}/guide")
def build_guide_from_track(
    track_id: int,
    body: Dict[str, Any] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Build iOS Guide JSON from a track's waypoints.

    Body params (optional):
      - siteId, siteName, description
      - voice: { provider, style, speed, pitch, language }
      - audioSettings: { crossfadeDuration, maxNarrationLength, volumeNormalization,
                        continuousNarration, continuousInterval, ambientGuide, ambientInterval,
                        poiAwareness, poiAwarenessDistance, poiAwarenessInterval }
      - defaultRadius (meters)
      - enterMultiplier, exitMultiplier, cooldownSeconds
      - persona, backgroundMusic
    """
    body = body or {}

    track: Optional[Track] = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.visibility == "private" and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Pull waypoints for the track
    waypoints = db.query(Waypoint).filter(Waypoint.track_id == track_id).all()

    language = body.get("language") or (body.get("voice", {}) or {}).get("language") or "de-AT"
    voice = {
        "provider": (body.get("voice", {}) or {}).get("provider", "openai"),
        "style": (body.get("voice", {}) or {}).get("style", "warm_guide"),
        "speed": (body.get("voice", {}) or {}).get("speed", 0.9),
        "pitch": (body.get("voice", {}) or {}).get("pitch", 1.0),
        "language": (body.get("voice", {}) or {}).get("language", language),
    }
    audio_settings_default = {
        "crossfadeDuration": 2.0,
        "maxNarrationLength": 45.0,
        "volumeNormalization": True,
        "continuousNarration": False,
        "continuousInterval": 30,
        "ambientGuide": False,
        "ambientInterval": 60,
        "poiAwareness": True,
        "poiAwarenessDistance": 200,
        "poiAwarenessInterval": 50,
        "promptVerbosity": "comprehensive",  # "minimal" or "comprehensive"
    }
    audio_settings = { **audio_settings_default, **(body.get("audioSettings") or {}) }

    default_radius: float = float(body.get("defaultRadius") or 15)
    enter_mul: float = float(body.get("enterMultiplier") or 0.7)
    exit_mul: float = float(body.get("exitMultiplier") or 1.5)
    cooldown: int = int(body.get("cooldownSeconds") or 60)

    persona: Optional[str] = body.get("persona")
    background_music: Optional[str] = body.get("backgroundMusic")

    pois = []
    for idx, wp in enumerate(waypoints):
        meta = wp.metadata_json or {}
        title = meta.get("title") or f"POI {idx+1}"
        cat_internal = _infer_category_from_title(title)
        domain = _map_category_to_domain(cat_internal)
        style = _map_category_to_style(cat_internal)
        # Try both field names for backward compatibility
        radius_override = meta.get("radiusMeters") or meta.get("radiusOverrideMeters")
        base_radius = float(radius_override) if isinstance(radius_override, (int, float)) else float(default_radius)
        static_audio_id = meta.get("staticAudioStorageId")
        music_id = meta.get("musicStorageId")

        poi = {
            "id": str(wp.id),
            "title": title,
            "category": domain,
            "coordinates": { "latitude": wp.latitude, "longitude": wp.longitude },
            "radiusMeters": base_radius,
            "priority": 1.0,
            "triggers": {
                "enterRadius": round(base_radius * enter_mul, 2),
                "exitRadius": round(base_radius * exit_mul, 2),
                "cooldownSeconds": cooldown,
            },
            "content": {
                "description": wp.user_description or None,
                "style": style,
                "staticAudioFile": (f"storage:{static_audio_id}" if static_audio_id else None),
                "dynamicPrompt": f"Beschreibe \"{title}\" ({cat_internal}). {('Hinweis: ' + (wp.user_description or '')) if wp.user_description else ''}".strip(),
                "meta": { "sourceCategory": cat_internal, "sourceStyle": style },
            },
        }
        if music_id:
            poi["content"]["music"] = [{
                "storage_id": music_id,
                "loop": True,
                "volume": 0.7,
                "intro_pause_ms": 0,
            }]
        pois.append(poi)

    guide = {
        "id": body.get("siteId") or f"track_{track_id}",
        "name": body.get("siteName") or (track.name or f"Track {track_id}"),
        "description": body.get("description") or (track.description or f"Audio Guide for track {track_id}"),
        "language": language,
        "trackId": track_id,
        "voice": voice,
        "audioSettings": audio_settings,
        **({"persona": persona} if persona else {}),
        **({"backgroundMusic": background_music} if background_music else {}),
        "pois": pois,
    }

    return guide


@router.put("/{track_id}/guide-config")
def update_guide_config(
    track_id: int,
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update guide configuration for a track.

    Stores guide settings in Track.metadata_json["guide"]:
    {
        "language": "de-AT",
        "persona": "Du bist ein erfahrener Wanderführer...",
        "defaultRadius": 15,
        "voice": {
            "provider": "openai",
            "style": "warm_guide",
            "speed": 0.9,
            "pitch": 1.0,
            "language": "de-AT"
        },
        "audioSettings": {
            "crossfadeDuration": 2.0,
            "maxNarrationLength": 45.0,
            "volumeNormalization": true,
            "continuousNarration": false,
            "continuousInterval": 30,
            "ambientGuide": false,
            "ambientInterval": 60,
            "poiAwareness": true,
            "poiAwarenessDistance": 200,
            "poiAwarenessInterval": 50
        },
        "backgroundMusic": "537;598;642"
    }
    """
    # Load track
    track: Optional[Track] = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track creator can update guide config")

    # Get existing metadata or create new
    metadata = track.metadata_json or {}

    # Update guide config
    metadata["guide"] = {
        "language": body.get("language", "de-AT"),
        "metaIdentity": body.get("metaIdentity"),  # System-level AI identity
        "persona": body.get("persona"),
        "userType": body.get("userType", "wanderer"),
        "userAge": body.get("userAge"),  # Optional user age
        "userInterests": body.get("userInterests"),  # Optional user interests array
        "userAdditionalInfo": body.get("userAdditionalInfo"),  # Optional additional user context
        "narrativeTone": body.get("narrativeTone", "motivating"),
        "chatHistory": body.get("chatHistory"),  # Array of {role, content} messages
        "defaultRadius": body.get("defaultRadius", 15),
        "voice": body.get("voice", {}),
        "audioSettings": body.get("audioSettings", {}),
        "backgroundMusic": body.get("backgroundMusic")
    }

    # Save to database
    from sqlalchemy.orm.attributes import flag_modified
    track.metadata_json = metadata
    flag_modified(track, "metadata_json")  # Mark JSON field as modified
    db.commit()
    db.refresh(track)

    return {
        "success": True,
        "track_id": track_id,
        "guide_config": metadata["guide"]
    }


# ========== NEW: Enhanced Export with Routes & Segments ==========

def _calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS coordinates in meters"""
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return 6371000 * c  # Earth radius in meters


def _calculate_polyline_distance(polyline: List[Dict[str, float]]) -> float:
    """Calculate total distance of GPS polyline"""
    total = 0.0
    for i in range(len(polyline) - 1):
        p1, p2 = polyline[i], polyline[i + 1]
        total += _calculate_haversine_distance(
            p1["latitude"], p1["longitude"],
            p2["latitude"], p2["longitude"]
        )
    return total


@router.post("/{track_id}/ios-guide-export")
def export_ios_guide_with_routes(
    track_id: int,
    body: Dict[str, Any] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Enhanced iOS Guide Export with Routes & Segments support

    Exports:
    - Multiple routes (alternative paths through the track)
    - Route segments with GPS polylines
    - Segment narratives (distance-triggered audio)
    - POIs with snap-to-route info
    - Full compatibility with Swift SpatialAudioGuide
    """
    body = body or {}

    # Load Track
    track: Optional[Track] = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.visibility == "private" and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Load track storage objects (images, PDFs, MD, JSON)
    track_storage = _load_storage_objects(
        db,
        track.storage_object_ids or [],
        track.storage_collection or {}
    )

    # Base guide config: Use Track.metadata_json["guide"] with fallbacks
    track_guide_config = (track.metadata_json or {}).get("guide", {})

    # Language: body > track.metadata_json > default
    language = body.get("language") or track_guide_config.get("language") or "de-AT"

    # Voice config: body > track.metadata_json > defaults
    voice_defaults = {
        "provider": "openai",
        "style": "warm_guide",
        "speed": 0.9,
        "pitch": 1.0,
        "language": language
    }
    voice_from_track = track_guide_config.get("voice", {})
    voice_from_body = body.get("voice", {})
    voice = {**voice_defaults, **voice_from_track, **voice_from_body}

    # Audio settings: body > track.metadata_json > defaults
    audio_defaults = {
        "crossfadeDuration": 2.0,
        "maxNarrationLength": 45.0,
        "volumeNormalization": True,
        "continuousNarration": False,
        "continuousInterval": 30,
        "ambientGuide": False,
        "ambientInterval": 60,
        "poiAwareness": True,
        "poiAwarenessDistance": 200,
        "poiAwarenessInterval": 50,
        "routeStatusNarration": True,
        "routeStatusTimeInterval": 300,
        "routeStatusDistanceMarkers": 500,
        "routeStatusMinSpeed": 0.5
    }
    audio_from_track = track_guide_config.get("audioSettings", {})
    audio_from_body = body.get("audioSettings", {})
    audio_settings = {**audio_defaults, **audio_from_track, **audio_from_body}

    # Meta Identity: body > track.metadata_json > None
    meta_identity = body.get("metaIdentity") or track_guide_config.get("metaIdentity")

    # Persona: body > track.metadata_json > default
    persona = body.get("persona") or track_guide_config.get("persona") or f"Du bist ein erfahrener Wanderführer für die Route \"{track.name}\"."

    # User Type: body > track.metadata_json > default
    user_type = body.get("userType") or track_guide_config.get("userType") or "wanderer"

    # User Age: body > track.metadata_json > None
    user_age = body.get("userAge") or track_guide_config.get("userAge")

    # User Interests: body > track.metadata_json > None
    user_interests = body.get("userInterests") or track_guide_config.get("userInterests")

    # User Additional Info: body > track.metadata_json > None
    user_additional_info = body.get("userAdditionalInfo") or track_guide_config.get("userAdditionalInfo")

    # Narrative Tone: body > track.metadata_json > default
    narrative_tone = body.get("narrativeTone") or track_guide_config.get("narrativeTone") or "motivating"

    # Background music: body > track.metadata_json > None
    background_music = body.get("backgroundMusic") or track_guide_config.get("backgroundMusic")

    # Default POI radius: body > track.metadata_json > 15m
    default_radius = body.get("defaultRadius") or track_guide_config.get("defaultRadius") or 15

    # Load Routes (filter out segment markers with < 3 GPS points)
    routes_db = guide_routes(db.query(TrackRoute).filter(TrackRoute.track_id == track_id).order_by(TrackRoute.id).all())
    routes_export = []

    for route_db in routes_db:
        # Get GPS track points for this route
        gps_points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type == "gps_track",
            Waypoint.route_id == route_db.id
        ).order_by(Waypoint.timestamp).all()

        polyline = [
            {
                "latitude": wp.latitude,
                "longitude": wp.longitude,
                "altitude": wp.altitude if wp.altitude else 0.0
            }
            for wp in gps_points
        ]

        # Skip segment markers (routes with < 3 GPS points)
        if len(polyline) < 3:
            print(f"⏭️  Skipping route '{route_db.name}' (id={route_db.id}): only {len(polyline)} GPS points (segment marker)")
            continue

        # Calculate route distance
        route_distance = _calculate_polyline_distance(polyline) if polyline else 0.0

        # NEW SEGMENT SYSTEM: Load segments from waypoint pairs (metadata_json.segment)
        # Segments are defined by two waypoints (start/end) with metadata_json.segment = {name, role, routeId}

        segments_export = []

        # Load all waypoints, filter segment markers in Python (SQLite doesn't support ->>)
        all_waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type != "gps_track"
        ).all()

        # Filter segment markers (waypoints with metadata_json.segment)
        segment_waypoints = [
            wp for wp in all_waypoints
            if wp.metadata_json and wp.metadata_json.get("segment")
        ]

        # Filter segment waypoints for this route
        route_segment_wps = [
            wp for wp in segment_waypoints
            if wp.metadata_json.get("snap", {}).get("route_id") == route_db.id
        ]

        # Group by segment name
        segments_from_waypoints = {}
        for wp in route_segment_wps:
            segment_meta = wp.metadata_json.get("segment", {})
            seg_name = segment_meta.get("name")
            role = segment_meta.get("role")

            if not seg_name or not role:
                continue

            if seg_name not in segments_from_waypoints:
                segments_from_waypoints[seg_name] = {}

            segments_from_waypoints[seg_name][role] = wp

        # Convert to RouteSegment objects
        for seg_name, wps in segments_from_waypoints.items():
            if "start" in wps and "end" in wps:
                start_wp = wps["start"]
                end_wp = wps["end"]

                start_snap = start_wp.metadata_json.get("snap", {})
                end_snap = end_wp.metadata_json.get("snap", {})

                start_distance = start_snap.get("along_meters")
                end_distance = end_snap.get("along_meters")

                # If along_meters is missing, calculate it on-the-fly (derived property)
                if start_distance is None:
                    logger.info(f"🔄 Calculating along_meters for segment '{seg_name}' start waypoint...")
                    start_distance = _calculate_along_meters_for_waypoint(start_wp, polyline)
                    logger.info(f"   → Calculated: {start_distance:.1f}m")

                if end_distance is None:
                    logger.info(f"🔄 Calculating along_meters for segment '{seg_name}' end waypoint...")
                    end_distance = _calculate_along_meters_for_waypoint(end_wp, polyline)
                    logger.info(f"   → Calculated: {end_distance:.1f}m")

                # Generate narratives from user_descriptions
                narratives = []
                segment_length = end_distance - start_distance

                # Start narrative (at segment entry)
                if start_wp.user_description:
                    narratives.append({
                        "triggerDistance": 0,  # Trigger at segment start
                        "type": "context",
                        "text": None,
                        "audioUrl": None,
                        "productionCuesPrompt": {
                            "context_type": "segment_entry",
                            "segment_name": seg_name,
                            "user_description": start_wp.user_description,
                            "distance_meters": segment_length
                        }
                    })

                # End narrative (at segment exit)
                if end_wp.user_description:
                    narratives.append({
                        "triggerDistance": segment_length,  # Trigger at segment end
                        "type": "context",
                        "text": None,
                        "audioUrl": None,
                        "productionCuesPrompt": {
                            "context_type": "segment_exit",
                            "segment_name": seg_name,
                            "user_description": end_wp.user_description,
                            "distance_meters": segment_length
                        }
                    })

                # Mid-segment narrative (if segment is long enough)
                if segment_length > 50 and (start_wp.user_description or end_wp.user_description):
                    mid_desc = start_wp.user_description or end_wp.user_description
                    narratives.append({
                        "triggerDistance": segment_length / 2,  # Trigger at segment midpoint
                        "type": "landmark",
                        "text": None,
                        "audioUrl": None,
                        "productionCuesPrompt": {
                            "context_type": "segment_midpoint",
                            "segment_name": seg_name,
                            "user_description": mid_desc,
                            "distance_meters": segment_length
                        }
                    })

                # Load storage objects from segment waypoints (start or end waypoint metadata)
                segment_wp_storage = []
                for wp in [start_wp, end_wp]:
                    if wp.metadata_json:
                        segment_wp_storage.extend(_load_storage_objects(
                            db,
                            wp.metadata_json.get("storage_object_ids", []),
                            wp.metadata_json.get("storage_collection", {})
                        ))

                # Remove duplicates based on ID
                unique_storage = []
                seen_ids = set()
                for s in segment_wp_storage:
                    if s["id"] not in seen_ids:
                        unique_storage.append(s)
                        seen_ids.add(s["id"])

                segments_export.append({
                    "id": f"seg_{route_db.id}_{seg_name.replace(' ', '_')}",
                    "name": seg_name,
                    "startDistance": start_distance,
                    "endDistance": end_distance,
                    "narratives": narratives,
                    "storageObjects": unique_storage if unique_storage else None
                })

        # Sort segments by startDistance
        segments_export.sort(key=lambda s: s["startDistance"])

        # Load route storage objects
        route_storage = _load_storage_objects(
            db,
            route_db.storage_object_ids or [],
            route_db.storage_collection or {}
        )

        routes_export.append({
            "id": f"route_{route_db.id}",
            "name": route_db.name,
            "description": route_db.description or "",
            "color": route_db.color or "#4CAF50",
            "distanceMeters": route_distance,
            "polyline": polyline,
            "segments": segments_export,
            "storageObjects": route_storage if route_storage else None
        })

    # Build map of existing route IDs for validation
    existing_route_ids = set(f"route_{route.id}" for route in routes_db)
    print(f"📍 Existing route IDs: {existing_route_ids}")

    # Load all non-GPS waypoints once
    if 'all_waypoints' not in locals():
        all_waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type != "gps_track"
        ).all()

    # Filter out segment markers (waypoints with segment metadata) → these are POIs
    poi_waypoints = [
        wp for wp in all_waypoints
        if not wp.metadata_json or not wp.metadata_json.get("segment")
    ]

    pois = []
    for idx, wp in enumerate(poi_waypoints):
        meta = wp.metadata_json or {}
        title = meta.get("title") or f"POI {idx+1}"
        cat_internal = _infer_category_from_title(title)
        domain = _map_category_to_domain(cat_internal)
        style = _map_category_to_style(cat_internal)

        # Extract snap info
        snap_info = meta.get("snap")
        snapped_to = None
        fixed_routes = None

        if snap_info:
            old_route_id = snap_info.get("route_id")
            route_id_str = f"route_{old_route_id}" if old_route_id else None

            # Check for fixed_routes (POI is pinned to specific routes)
            if "fixed_routes" in snap_info:
                # Explicit fixed_routes array - validate they exist
                valid_fixed = [f"route_{rid}" for rid in snap_info["fixed_routes"] if f"route_{rid}" in existing_route_ids]
                if valid_fixed:
                    fixed_routes = valid_fixed
                else:
                    # All fixed routes are invalid - try to re-snap to nearest existing route
                    print(f"⚠️ POI '{title}' has invalid fixedRoutes, attempting re-snap to nearest route")
                    fixed_routes = None  # Will be re-snapped below
            elif route_id_str and route_id_str in existing_route_ids:
                # Route exists - auto-fix to that route
                fixed_routes = [route_id_str]
            else:
                # Route no longer exists - try to find nearest existing route
                if route_id_str:
                    print(f"⚠️ POI '{title}' snapped to non-existent route {route_id_str}")

                # Intelligent multi-route snap: Find ALL routes this POI is close to
                # If multiple routes are nearly equidistant (e.g. shared path section),
                # the POI should be visible on ALL those routes

                poi_coord = (wp.latitude, wp.longitude)
                route_distances = []  # [(route_id, min_distance), ...]

                for route_db in routes_db:
                    # Get route polyline
                    route_waypoints = db.query(Waypoint).filter(
                        Waypoint.track_id == track_id,
                        Waypoint.route_id == route_db.id
                    ).order_by(Waypoint.recorded_at).all()

                    if not route_waypoints:
                        continue

                    # Find closest point on this route
                    min_dist_to_route = float('inf')
                    for rwp in route_waypoints:
                        dx = rwp.latitude - poi_coord[0]
                        dy = rwp.longitude - poi_coord[1]
                        # Convert to approximate meters (rough approximation)
                        dist_meters = ((dx * 111000)**2 + (dy * 111000 * 0.7)**2) ** 0.5

                        if dist_meters < min_dist_to_route:
                            min_dist_to_route = dist_meters

                    route_distances.append((f"route_{route_db.id}", min_dist_to_route))

                # Sort by distance
                route_distances.sort(key=lambda x: x[1])

                if route_distances:
                    # Threshold: POI is "on" a route if within 50m
                    snap_threshold = 50.0
                    # Similarity threshold: Routes are "same position" if diff < 10m
                    similarity_threshold = 10.0

                    closest_route, closest_dist = route_distances[0]

                    if closest_dist <= snap_threshold:
                        # POI is close enough to at least one route
                        # Check if other routes are also very close (shared path)
                        candidate_routes = [closest_route]

                        for route_id, dist in route_distances[1:]:
                            if dist <= snap_threshold:
                                # This route is also close
                                diff = abs(dist - closest_dist)
                                if diff <= similarity_threshold:
                                    # Nearly same distance → shared path section
                                    candidate_routes.append(route_id)

                        if len(candidate_routes) > 1:
                            print(f"   → POI on shared path: {candidate_routes} (distances: {[f'{d:.1f}m' for _, d in route_distances[:len(candidate_routes)]]})")
                        else:
                            print(f"   → Re-snapped to nearest route: {closest_route} ({closest_dist:.1f}m)")

                        fixed_routes = candidate_routes
                        route_id_str = closest_route  # Use closest for snappedTo
                    else:
                        # POI too far from all routes - visible on all
                        print(f"   → POI too far from routes (closest: {closest_dist:.1f}m), visible on all")
                        fixed_routes = None
                        route_id_str = None
                else:
                    # No routes found - visible on all
                    fixed_routes = None
                    route_id_str = None

            # Build snappedTo info
            snapped_to = {
                "routeId": route_id_str,
                "segmentId": f"segment_{wp.segment_id}" if wp.segment_id else None,
                "distanceAlongSegment": snap_info.get("along_meters", 0),
                "lateralOffsetMeters": snap_info.get("lateral_offset_meters", 0)
            } if route_id_str else None

        # Try both field names for backward compatibility
        # Admin Portal uses "radiusMeters", older code used "radiusOverrideMeters"
        radius = float(meta.get("radiusMeters") or meta.get("radiusOverrideMeters") or default_radius)

        # Load POI storage objects (images, PDFs, MD, JSON)
        poi_storage = _load_storage_objects(
            db,
            meta.get("storage_object_ids", []),
            meta.get("storage_collection", {})
        )

        poi = {
            "id": f"wp_{wp.id}",
            "title": title,
            "category": domain,
            "coordinates": {
                "latitude": wp.latitude,
                "longitude": wp.longitude,
                "altitude": wp.altitude if wp.altitude else 0.0
            },
            "radiusMeters": radius,
            "snappedTo": snapped_to,
            "fixedRoutes": fixed_routes,  # NEW: List of route IDs this POI is fixed to
            "triggers": {
                "enterRadius": round(radius * 0.7, 2),
                "exitRadius": round(radius * 1.5, 2),
                "cooldownSeconds": 60
            },
            "content": {
                "description": wp.user_description or "",
                "style": style,
                "staticAudioFile": f"storage:{meta['staticAudioStorageId']}" if meta.get("staticAudioStorageId") else None,
                "dynamicPrompt": f"Beschreibe \"{title}\". Hinweis: {wp.user_description or ''}",
                "storageObjects": poi_storage if poi_storage else None
            }
        }

        pois.append(poi)

    # Chat history: body > track.metadata_json > None
    chat_history = body.get("chatHistory") or track_guide_config.get("chatHistory")

    # Build complete guide
    guide = {
        "id": body.get("siteId") or f"track_{track_id}",
        "name": body.get("siteName") or track.name or f"Track {track_id}",
        "description": body.get("description") or track.description or "",
        "language": language,
        "trackId": track_id,
        "persona": persona,
        "userType": user_type,
        "narrativeTone": narrative_tone,
        "voice": voice,
        "audioSettings": audio_settings,
        "backgroundMusic": background_music or [],
        "routes": routes_export,
        "pois": pois,
        "trackStatistics": {
            "totalWaypoints": track.total_waypoints or 0,
            "distanceMeters": track.distance_meters or 0.0,
            "durationSeconds": track.duration_seconds or 0,
            "elevationGainMeters": track.elevation_gain_meters or 0.0
        },
        "storageObjects": track_storage if track_storage else None
    }

    # Add optional fields only if present
    if meta_identity:
        guide["metaIdentity"] = meta_identity
    if user_age:
        guide["userAge"] = user_age
    if user_interests:
        guide["userInterests"] = user_interests
    if user_additional_info:
        guide["userAdditionalInfo"] = user_additional_info
    if chat_history:
        guide["chatHistory"] = chat_history

    return guide


def _calculate_along_meters_for_waypoint(waypoint, polyline: List[Dict[str, float]]) -> float:
    """
    Calculate along_meters (distance along route) for a waypoint on-the-fly.
    This is a derived property computed from waypoint position + route polyline.
    """
    if not polyline or len(polyline) < 2:
        return 0.0

    _, _, distance_along, _ = _snap_point_to_polyline(waypoint.latitude, waypoint.longitude, polyline)
    return distance_along


def _snap_point_to_polyline(point_lat: float, point_lon: float, polyline: List[Dict[str, float]]) -> tuple:
    """
    Snap a point to the closest position on a polyline.
    Returns: (snapped_lat, snapped_lon, distance_along_route, snap_distance)
    """
    min_distance = float('inf')
    best_snap_lat = point_lat
    best_snap_lon = point_lon
    distance_along_route = 0.0
    best_distance_along = 0.0

    current_route_distance = 0.0

    for i in range(len(polyline) - 1):
        p1 = polyline[i]
        p2 = polyline[i + 1]

        # Simple projection: find closest point on segment p1-p2
        # For simplicity, just check distance to p1 and p2
        dist_to_p1 = _calculate_haversine_distance(point_lat, point_lon, p1["latitude"], p1["longitude"])
        dist_to_p2 = _calculate_haversine_distance(point_lat, point_lon, p2["latitude"], p2["longitude"])

        if dist_to_p1 < min_distance:
            min_distance = dist_to_p1
            best_snap_lat = p1["latitude"]
            best_snap_lon = p1["longitude"]
            best_distance_along = current_route_distance

        if dist_to_p2 < min_distance:
            min_distance = dist_to_p2
            best_snap_lat = p2["latitude"]
            best_snap_lon = p2["longitude"]
            best_distance_along = current_route_distance + _calculate_haversine_distance(
                p1["latitude"], p1["longitude"], p2["latitude"], p2["longitude"]
            )

        # Update distance along route
        current_route_distance += _calculate_haversine_distance(
            p1["latitude"], p1["longitude"], p2["latitude"], p2["longitude"]
        )

    return (best_snap_lat, best_snap_lon, best_distance_along, min_distance)


@router.post("/{track_id}/segment/from-pois")
def create_segment_from_pois(
    track_id: int,
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a segment GPS polyline between two POIs by snapping to route.

    Body:
    - startPoiId: int (waypoint ID)
    - endPoiId: int (waypoint ID)
    - routeId: int (optional, auto-select best route if not provided)
    - maxSnapDistance: float (optional, meters, fallback to POI coords if exceeded)
    """
    start_poi_id = body.get("startPoiId")
    end_poi_id = body.get("endPoiId")
    route_id = body.get("routeId")
    max_snap_distance = body.get("maxSnapDistance", 100.0)  # default 100m

    if not start_poi_id or not end_poi_id:
        raise HTTPException(status_code=400, detail="startPoiId and endPoiId required")

    # Load POIs
    start_poi = db.query(Waypoint).filter(Waypoint.id == start_poi_id).first()
    end_poi = db.query(Waypoint).filter(Waypoint.id == end_poi_id).first()

    if not start_poi or not end_poi:
        raise HTTPException(status_code=404, detail="POI not found")

    # Load route polyline
    if route_id:
        route = db.query(TrackRoute).filter(TrackRoute.id == route_id, TrackRoute.track_id == track_id).first()
        if not route:
            raise HTTPException(status_code=404, detail="Route not found")
    else:
        # Auto-select first route
        route = next(iter(guide_routes(db.query(TrackRoute).filter(TrackRoute.track_id == track_id).order_by(TrackRoute.id).all())), None)
        if not route:
            raise HTTPException(status_code=404, detail="No routes found for this track")

    # Get route GPS points
    gps_points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.route_id == route.id
    ).order_by(Waypoint.timestamp).all()

    if len(gps_points) < 2:
        raise HTTPException(status_code=400, detail="Route has insufficient GPS points")

    polyline = [
        {"latitude": wp.latitude, "longitude": wp.longitude, "altitude": wp.altitude or 0.0}
        for wp in gps_points
    ]

    # Snap POIs to route
    start_snap_lat, start_snap_lon, start_distance, start_snap_dist = _snap_point_to_polyline(
        start_poi.latitude, start_poi.longitude, polyline
    )
    end_snap_lat, end_snap_lon, end_distance, end_snap_dist = _snap_point_to_polyline(
        end_poi.latitude, end_poi.longitude, polyline
    )

    # Check if snap distance exceeded
    if start_snap_dist > max_snap_distance:
        print(f"⚠️ Start POI snap distance {start_snap_dist:.1f}m exceeds max {max_snap_distance}m, using original coords")
        start_snap_lat = start_poi.latitude
        start_snap_lon = start_poi.longitude

    if end_snap_dist > max_snap_distance:
        print(f"⚠️ End POI snap distance {end_snap_dist:.1f}m exceeds max {max_snap_distance}m, using original coords")
        end_snap_lat = end_poi.latitude
        end_snap_lon = end_poi.longitude

    # Ensure start is before end
    if start_distance > end_distance:
        start_distance, end_distance = end_distance, start_distance
        start_snap_lat, end_snap_lat = end_snap_lat, start_snap_lat
        start_snap_lon, end_snap_lon = end_snap_lon, start_snap_lon

    # Extract segment polyline
    segment_polyline = []
    current_distance = 0.0

    # Add start point
    segment_polyline.append({
        "latitude": start_snap_lat,
        "longitude": start_snap_lon,
        "altitude": 0.0
    })

    # Add intermediate points
    for i in range(len(polyline)):
        point = polyline[i]

        if i > 0:
            prev_point = polyline[i - 1]
            current_distance += _calculate_haversine_distance(
                prev_point["latitude"], prev_point["longitude"],
                point["latitude"], point["longitude"]
            )

        if start_distance < current_distance < end_distance:
            segment_polyline.append(point)

    # Add end point
    segment_polyline.append({
        "latitude": end_snap_lat,
        "longitude": end_snap_lon,
        "altitude": 0.0
    })

    return {
        "success": True,
        "segment": {
            "startDistance": start_distance,
            "endDistance": end_distance,
            "polyline": segment_polyline,
            "startSnapDistance": start_snap_dist,
            "endSnapDistance": end_snap_dist,
            "routeId": route.id,
            "routeName": route.name
        }
    }

