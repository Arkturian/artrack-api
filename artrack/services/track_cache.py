"""Per-track revision numbers and a cache that is exact, not time-based.

Hot read paths (by-knowledge, waypoints/detail, route geometry) used to load
every waypoint of a track on every request, about 2,500 rows with JSON for
Track 30, to return a few hundred bytes. Caching them with a TTL would serve
stale data to the editor, so the cache key is a revision instead.

The revision lives in the database: statement-level triggers on `waypoints`
and `track_routes` bump `track_revisions.rev` for every track a statement
touched (insert, update, delete). That covers every writer: the ORM, bulk
`query.update()`, raw SQL, scripts and other services, and all gunicorn
workers see the same number. Reading it is one primary-key lookup.

If the table does not exist (SQLite dev setups) `track_rev()` returns None
and callers rebuild every time, which is the old behaviour.
"""
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional

from sqlalchemy import event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Revision numbers come from one global sequence, so a number is never reused:
# after any change the track's revision is a value no cache has seen before.
_DDL = [
    "CREATE SEQUENCE IF NOT EXISTS artrack_track_rev_seq",
    """CREATE TABLE IF NOT EXISTS track_revisions (
           track_id integer PRIMARY KEY,
           rev bigint NOT NULL
       )""",
    """CREATE OR REPLACE FUNCTION artrack_bump_track_rev() RETURNS trigger
       LANGUAGE plpgsql AS $$
       BEGIN
         IF TG_OP = 'INSERT' THEN
           INSERT INTO track_revisions (track_id, rev)
             SELECT t.track_id, nextval('artrack_track_rev_seq')
             FROM (SELECT DISTINCT track_id FROM new_rows WHERE track_id IS NOT NULL ORDER BY 1) t
           ON CONFLICT (track_id) DO UPDATE SET rev = EXCLUDED.rev;
         ELSIF TG_OP = 'UPDATE' THEN
           INSERT INTO track_revisions (track_id, rev)
             SELECT t.track_id, nextval('artrack_track_rev_seq')
             FROM (SELECT track_id FROM new_rows WHERE track_id IS NOT NULL
                   UNION SELECT track_id FROM old_rows WHERE track_id IS NOT NULL ORDER BY 1) t
           ON CONFLICT (track_id) DO UPDATE SET rev = EXCLUDED.rev;
         ELSE
           INSERT INTO track_revisions (track_id, rev)
             SELECT t.track_id, nextval('artrack_track_rev_seq')
             FROM (SELECT DISTINCT track_id FROM old_rows WHERE track_id IS NOT NULL ORDER BY 1) t
           ON CONFLICT (track_id) DO UPDATE SET rev = EXCLUDED.rev;
         END IF;
         RETURN NULL;
       END $$""",
]

_TRIGGER_TABLES = ("waypoints", "track_routes")
_TRIGGER_EVENTS = {
    "ins": "AFTER INSERT ON {t} REFERENCING NEW TABLE AS new_rows",
    "upd": "AFTER UPDATE ON {t} REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows",
    "del": "AFTER DELETE ON {t} REFERENCING OLD TABLE AS old_rows",
}


def ensure_revision_triggers(engine) -> bool:
    """Create the revision table, function and triggers if missing (Postgres only).

    Idempotent and safe with several workers starting at once: an advisory
    lock serialises them, and triggers are only created when absent.
    """
    if engine.dialect.name != "postgresql":
        return False
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7301302)"))
        for stmt in _DDL:
            conn.execute(text(stmt))
        existing = {r[0] for r in conn.execute(text(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'artrack_rev_%' AND NOT tgisinternal"))}
        for table in _TRIGGER_TABLES:
            for suffix, clause in _TRIGGER_EVENTS.items():
                name = f"artrack_rev_{table}_{suffix}"
                if name in existing:
                    continue
                conn.execute(text(
                    f"CREATE TRIGGER {name} {clause.format(t=table)} "
                    f"FOR EACH STATEMENT EXECUTE FUNCTION artrack_bump_track_rev()"))
                logger.info("created trigger %s", name)
    return True


_rev_supported: Optional[bool] = None
_MEMO_KEY = "artrack_track_rev"


def _forget_revs(target, *args) -> None:
    # Bulk events pass a BulkUpdate/BulkDelete context with .session; never
    # let this hook fail a write.
    session = getattr(target, "session", target)
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.pop(_MEMO_KEY, None)


# The revision is remembered per session (= per request), because some paths
# ask for it once per waypoint. Any write in the session drops the memo, so a
# request that writes and then reads sees the new revision.
for _ev in ("after_flush", "after_commit", "after_rollback", "after_soft_rollback",
            "after_bulk_update", "after_bulk_delete"):
    event.listen(Session, _ev, _forget_revs)


def track_rev(db: Session, track_id: int) -> Optional[int]:
    """Current revision of a track, 0 if never changed since the triggers exist.

    None means revisions are unavailable; callers must then not cache.
    """
    global _rev_supported
    if _rev_supported is False:
        return None
    memo = db.info.setdefault(_MEMO_KEY, {})
    if track_id in memo:
        return memo[track_id]
    try:
        row = db.execute(text("SELECT rev FROM track_revisions WHERE track_id = :t"), {"t": track_id}).first()
    except Exception as e:  # table missing (SQLite) or not yet created
        logger.warning("track_rev unavailable, caching off: %s", e)
        db.rollback()
        _rev_supported = False
        return None
    _rev_supported = True
    rev = int(row[0]) if row else 0
    memo[track_id] = rev
    return rev


MISS = object()


class TrackCache:
    """Small LRU of values that are valid for exactly one track revision.

    An entry is used only if its revision equals the current one. `ttl`
    additionally bounds entries that mix in data from outside the database
    (for example HLS state from storage-api).
    """

    def __init__(self, name: str, max_entries: int = 128, ttl: Optional[float] = None):
        self.name = name
        self.max_entries = max_entries
        self.ttl = ttl
        self._data: "OrderedDict[Hashable, tuple]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get_or_build(self, rev: Optional[int], key: Hashable, build: Callable[[], Any]) -> Any:
        value = self.get(rev, key)
        if value is MISS:
            value = build()
            self.put(rev, key, value)
        return value

    def get(self, rev: Optional[int], key: Hashable) -> Any:
        """Cached value for this revision, or MISS."""
        if rev is None:
            return MISS
        now = time.monotonic()
        with self._lock:
            ent = self._data.get(key)
            if ent is not None and ent[0] == rev and (ent[1] is None or ent[1] > now):
                self._data.move_to_end(key)
                self.hits += 1
                return ent[2]
        self.misses += 1
        return MISS

    def put(self, rev: Optional[int], key: Hashable, value: Any) -> None:
        if rev is None:
            return
        now = time.monotonic()
        with self._lock:
            self._data[key] = (rev, (now + self.ttl) if self.ttl else None, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        return {"name": self.name, "entries": len(self._data), "hits": self.hits, "misses": self.misses}
