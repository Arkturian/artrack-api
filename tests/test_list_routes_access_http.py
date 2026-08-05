"""list_routes ueber HTTP — mit echten Anfragen statt nur der Helfer-Logik.

Ergaenzt test_track_read_access.py. Jener prueft `can_read_track` als Funktion;
dieser prueft, dass der ENDPUNKT das Ergebnis auch wirklich anwendet — also
Waechter, Routing und Antwortmodell zusammen.

Anlass (Issue #673): Der gemeldete Fall war ein Fremder, der einen
oeffentlichen Track liest. Ueber echtes HTTP war er lange nicht pruefbar, weil
dafuer ein Schluessel noetig waere, der NICHT auf den Track-Eigentuemer
aufloest — und ein Konto auf einem Produktivdienst legt man dafuer nicht an.

Der Ausweg: die Identitaet nicht erschaffen, sondern per
`dependency_overrides` vorgeben. Die Datenbank wird ebenso ersetzt, sodass der
Lauf ohne Postgres auskommt und nichts Produktives beruehrt.

WAS DIESER TEST NICHT ABDECKT: den Weg durch nginx und die Aufloesung eines
echten X-API-KEY auf einen Nutzer. Diese Strecke bleibt der Live-Abnahme
vorbehalten. Hier endet die Aussage beim Handler.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main
from artrack.auth import get_current_user
from artrack.database import get_db


EIGNER = 1
FREMD = 999


class _Nutzer:
    def __init__(self, id, trust_level="user"):
        self.id = id
        self.trust_level = trust_level
        self._readonly_key = False


class _Track:
    def __init__(self, created_by, visibility):
        self.id = 30
        self.created_by = created_by
        self.visibility = visibility
        self.collaborators = []


class _Route:
    def __init__(self, i):
        self.id = i
        self.track_id = 30
        self.name = f"Route {i}"
        self.color = None
        self.description = None
        self.storage_object_ids = None
        self.storage_collection = None
        # Echtes Datum noetig: Das response_model validiert die Antwort, und
        # None laesst den Handler mit 500 enden — ein Fehler der Attrappe, der
        # sich leicht als Fehler des Waechters missdeuten laesst.
        self.created_at = datetime(2026, 1, 1, 12, 0, 0)


class _Query:
    def __init__(self, modell, track):
        self._modell = modell
        self._track = track

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._track if self._modell.__name__ == "Track" else None

    def all(self):
        return [] if self._modell.__name__ == "Track" else [_Route(56), _Route(60)]


class _DB:
    def __init__(self, track):
        self._track = track

    def query(self, modell):
        return _Query(modell, self._track)


def _hole_routen(track, nutzer):
    main.app.dependency_overrides[get_db] = lambda: _DB(track)
    main.app.dependency_overrides[get_current_user] = lambda: nutzer
    try:
        with TestClient(main.app, raise_server_exceptions=False) as c:
            return c.get("/tracks/30/routes")
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("bezeichnung,track,nutzer,erwartet", [
    ("Eigentuemer sieht seinen privaten Track",
     _Track(EIGNER, "private"), _Nutzer(EIGNER), 200),
    ("Fremder wird am privaten Track abgewiesen",
     _Track(EIGNER, "private"), _Nutzer(FREMD), 403),
    ("Fremder darf den OEFFENTLICHEN Track lesen",     # <- der Fall aus #673
     _Track(EIGNER, "public"), _Nutzer(FREMD), 200),
    ("Admin sieht auch private Tracks",
     _Track(EIGNER, "private"), _Nutzer(FREMD, "admin"), 200),
    ("Moderator sieht auch private Tracks",
     _Track(EIGNER, "private"), _Nutzer(FREMD, "moderator"), 200),
])
def test_list_routes_zugriff(bezeichnung, track, nutzer, erwartet):
    r = _hole_routen(track, nutzer)
    assert r.status_code == erwartet, f"{bezeichnung}: {r.status_code} statt {erwartet}"


def test_oeffentlicher_leser_bekommt_die_routen_auch_wirklich():
    """Ein 200 allein genuegt nicht — es koennte auch eine leere Liste sein.

    Genau diese Verwechslung hat mich am 2026-07-31 an anderer Stelle erwischt:
    Ein gruener Bau mit leerem Ergebnis sieht aus wie Erfolg.
    """
    r = _hole_routen(_Track(EIGNER, "public"), _Nutzer(FREMD))
    assert r.status_code == 200
    daten = r.json()
    assert isinstance(daten, list) and len(daten) == 2, daten
    assert {x["id"] for x in daten} == {56, 60}
