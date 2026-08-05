"""
Collaborative Track Models - Erweiterung für Multi-User Track Sharing
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import uuid
import secrets

from .models import Base

# === Database Models ===

class TrackCollaborator(Base):
    """Users who can contribute to a collaborative track"""
    __tablename__ = "track_collaborators"
    
    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    
    # Permissions
    role = Column(String, default="contributor")  # owner, admin, contributor, viewer
    can_add_waypoints = Column(Boolean, default=True)
    can_edit_waypoints = Column(Boolean, default=False)  # Only own waypoints
    can_delete_waypoints = Column(Boolean, default=False)  # Only own waypoints
    can_invite_others = Column(Boolean, default=False)
    can_edit_track = Column(Boolean, default=False)
    
    # Metadata
    joined_at = Column(DateTime, default=datetime.utcnow)
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    last_activity = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    # Relationships
    track = relationship("Track", back_populates="collaborators")
    user = relationship("User", foreign_keys=[user_id])
    inviter = relationship("User", foreign_keys=[invited_by])

class TrackInvitation(Base):
    """Pending invitations to join a collaborative track"""
    __tablename__ = "track_invitations"
    
    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"))
    invited_by = Column(Integer, ForeignKey("users.id"))
    
    # Invitation details
    email = Column(String, nullable=True)  # For email invitations
    username = Column(String, nullable=True)  # For username invitations
    invite_token = Column(String, unique=True, default=lambda: secrets.token_urlsafe(32))
    invite_code = Column(String, unique=True, default=lambda: ''.join(secrets.choice('ABCDEFGHIJKLMNPQRSTUVWXYZ123456789') for _ in range(8)))  # 8-char code
    
    # Permissions for invited user
    role = Column(String, default="contributor")
    can_add_waypoints = Column(Boolean, default=True)
    can_edit_waypoints = Column(Boolean, default=False)
    can_delete_waypoints = Column(Boolean, default=False)
    can_invite_others = Column(Boolean, default=False)
    
    # Status and timing
    status = Column(String, default="pending")  # pending, accepted, declined, expired, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=7))
    accepted_at = Column(DateTime, nullable=True)
    
    # Message
    personal_message = Column(Text, nullable=True)
    
    # Relationships
    track = relationship("Track", back_populates="invitations")
    inviter = relationship("User")

class TrackActivity(Base):
    """Activity log for collaborative tracks"""
    __tablename__ = "track_activities"
    
    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    
    # Activity details
    activity_type = Column(String)  # waypoint_added, waypoint_edited, waypoint_deleted, user_joined, user_left, track_edited
    target_type = Column(String, nullable=True)  # waypoint, track, user
    target_id = Column(Integer, nullable=True)
    
    # Activity data
    description = Column(String)
    # NOTE: SQLAlchemy Declarative reserves attribute name "metadata" for table metadata.
    # Use a different attribute name for custom data storage.
    metadata_json = Column(Text, nullable=True)  # JSON data for activity details
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    track = relationship("Track")
    user = relationship("User")

# === Pydantic Schemas ===

class TrackCollaboratorCreate(BaseModel):
    user_id: int
    role: str = "contributor"
    can_add_waypoints: bool = True
    can_edit_waypoints: bool = False
    can_delete_waypoints: bool = False
    can_invite_others: bool = False

class TrackCollaboratorResponse(BaseModel):
    id: int
    user_id: int
    username: Optional[str] = None  # From User join
    role: str
    can_add_waypoints: bool
    can_edit_waypoints: bool
    can_delete_waypoints: bool
    can_invite_others: bool
    joined_at: datetime
    last_activity: datetime
    is_active: bool
    
    class Config:
        from_attributes = True

class TrackInvitationCreate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    role: str = "contributor"
    can_add_waypoints: bool = True
    can_edit_waypoints: bool = False
    can_delete_waypoints: bool = False
    can_invite_others: bool = False
    expires_in_hours: int = 168  # 7 days default
    personal_message: Optional[str] = None

class TrackInvitationResponse(BaseModel):
    id: int
    track_id: int
    track_name: Optional[str] = None  # From Track join
    email: Optional[str]
    username: Optional[str]
    invite_token: str
    invite_code: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime
    personal_message: Optional[str]
    inviter_name: Optional[str] = None  # From User join
    
    class Config:
        from_attributes = True

class TrackActivityResponse(BaseModel):
    id: int
    track_id: int
    user_id: int
    username: Optional[str] = None
    activity_type: str
    target_type: Optional[str]
    target_id: Optional[int]
    description: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class CollaborativeTrackCreate(BaseModel):
    name: str
    description: Optional[str] = None
    visibility: str = "private"
    track_type: str = "hiking"
    tags: List[str] = []
    is_collaborative: bool = True
    collaboration_mode: str = "invite_only"  # invite_only, open, link_share
    max_collaborators: int = 10

class CollaborativeTrackUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_collaborative: Optional[bool] = None
    collaboration_mode: Optional[str] = None
    max_collaborators: Optional[int] = None

class InviteAcceptRequest(BaseModel):
    invite_token: str

class InviteByCodeRequest(BaseModel):
    invite_code: str

class TrackPermissions(BaseModel):
    """Current user's permissions for a track"""
    can_view: bool = True
    can_add_waypoints: bool = False
    can_edit_waypoints: bool = False  # Own waypoints only
    can_delete_waypoints: bool = False  # Own waypoints only
    can_invite_others: bool = False
    can_edit_track: bool = False
    can_manage_collaborators: bool = False  # Admin/Owner only
    is_owner: bool = False
    role: str = "viewer"

class CollaborativeTrackStats(BaseModel):
    """Statistics for collaborative tracks"""
    total_collaborators: int
    active_collaborators: int  # Active in last 7 days
    total_waypoints: int
    waypoints_by_user: Dict[str, int]  # username -> waypoint count
    most_active_contributor: Optional[str]
    last_activity: Optional[datetime]
    activity_last_7_days: int

# === Helper Functions ===

def generate_invite_link(track_id: int, invite_token: str, base_url: str = "https://api.arkturian.com") -> str:
    """Generate shareable invite link"""
    return f"{base_url}/artrack/invite/{invite_token}"

def generate_qr_code_data(track_id: int, invite_code: str) -> str:
    """Generate QR code data for easy sharing"""
    return f"artrack://join/{invite_code}"

def check_collaboration_limits(track, new_collaborators_count: int = 1) -> bool:
    """Check if track can accept more collaborators"""
    current_count = len([c for c in track.collaborators if c.is_active])
    return current_count + new_collaborators_count <= track.max_collaborators

def get_user_permissions(track, user_id: int) -> TrackPermissions:
    """Get user's permissions for a track"""
    # Track owner has all permissions
    if track.created_by == user_id:
        return TrackPermissions(
            can_view=True,
            can_add_waypoints=True,
            can_edit_waypoints=True,
            can_delete_waypoints=True,
            can_invite_others=True,
            can_edit_track=True,
            can_manage_collaborators=True,
            is_owner=True,
            role="owner"
        )
    
    # Find collaborator record
    collaborator = next((c for c in track.collaborators if c.user_id == user_id and c.is_active), None)
    
    if not collaborator:
        # Not a collaborator - check if track is public
        if track.visibility == "public":
            return TrackPermissions(can_view=True, role="viewer")
        else:
            return TrackPermissions(can_view=False, role="none")
    
    # Return collaborator permissions
    return TrackPermissions(
        can_view=True,
        can_add_waypoints=collaborator.can_add_waypoints,
        can_edit_waypoints=collaborator.can_edit_waypoints,
        can_delete_waypoints=collaborator.can_delete_waypoints,
        can_invite_others=collaborator.can_invite_others,
        can_edit_track=collaborator.can_edit_track,
        can_manage_collaborators=collaborator.role in ["admin"],
        is_owner=False,
        role=collaborator.role
    )


# Trust-Stufen, die ueber die Track-Sichtbarkeit hinweg lesen duerfen.
MODERATION_TRUST_LEVELS = ("admin", "moderator")


def can_read_track(track, user) -> bool:
    """Darf dieser Nutzer diesen Track LESEN?

    Ein einziger Ort fuer den Lese-Zugriff. Vorher stand die Pruefung in FUENF
    verschiedenen Schreibweisen an 24 GET-Endpunkten -- gezaehlt am 2026-08-01:

        13x  track.created_by != current_user.id
              -> streng eigentuemer-only. Ignoriert Sichtbarkeit UND
                 Kollaborateure. Das war der gemeldete Fehler (Issue #673):
                 ein oeffentlicher Track blieb fuer alle ausser dem Eigentuemer
                 gesperrt.
         7x  ... and visibility == "private" and trust_level not in (admin, moderator)
         2x  visibility == "private" and created_by != ...
         1x  ... and visibility == "private"
         1x  not track or (... visibility ... trust_level ...)

    Die letzten vier Formen liessen oeffentliche Tracks bereits durch -- 11 der
    24 Endpunkte waren also nie kaputt. Das ist der Grund, warum der Fehler so
    schwer zu fassen war: Ein Client bekam bei manchen Aufrufen Daten und bei
    anderen 403, ohne erkennbare Regel.

    KEINE der beiden vorhandenen Bauarten war fuer sich vollstaendig:

      - `get_user_permissions(...).can_view` kennt Eigentuemer, aktive
        Kollaborateure und oeffentliche Tracks -- aber KEINE Trust-Stufen.
        Haette man alles darauf umgestellt, haetten Admins und Moderatoren
        ihren Zugriff auf private Tracks VERLOREN.
      - Die Sichtbarkeits-Variante kennt Trust-Stufen -- aber keine
        Kollaborateure. Ein eingetragener Mitarbeiter an einem privaten Track
        wurde davon abgewiesen.

    Diese Funktion ist die Vereinigung beider: Eigentuemer ODER aktiver
    Kollaborateur ODER oeffentlicher Track ODER Admin/Moderator.

    NUR FUER LESEZUGRIFFE. Schreibpfade duerfen das hier nicht verwenden: Bei
    einem oeffentlichen Track ist das Ergebnis fuer JEDEN wahr. Die 40
    Schreib-Stellen tragen dieselbe `created_by`-Zeile im Wortlaut und bleiben
    bewusst unangetastet -- wer sie mechanisch mitersetzt, macht den Track fuer
    jeden authentifizierten Nutzer beschreibbar.
    """
    if track is None or user is None:
        return False
    if getattr(user, "trust_level", None) in MODERATION_TRUST_LEVELS:
        return True
    return get_user_permissions(track, user.id).can_view