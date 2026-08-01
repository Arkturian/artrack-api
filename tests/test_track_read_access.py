"""Lese-Zugriff auf Tracks — can_read_track und die Lese/Schreib-Trennung.

Anlass: Issue #673. Die Tscheppa-iOS-Karte bekam auf GET /tracks/30/routes
HTTP 403, obwohl Track 30 von jedem Besuchergeraet gelesen werden soll.

Ursache war nicht ein Endpunkt, sondern FUENF verschiedene Schreibweisen
derselben Pruefung an 24 GET-Endpunkten, von denen 13 die Track-Sichtbarkeit
gar nicht ansahen. Ein Client bekam dadurch bei manchen Aufrufen Daten und bei
anderen 403, ohne erkennbare Regel.

Die Tests hier decken zwei verschiedene Risiken ab:

  1. Entscheidet can_read_track richtig?  (test_*_darf_lesen / _darf_nicht_lesen)
  2. Bleibt die Lese/Schreib-Trennung erhalten?  (test_struktur_*)

Der zweite Teil ist der wichtigere. Die gefaehrliche Verwechslung ist nicht,
dass jemand die Logik falsch schreibt, sondern dass jemand die 40
SCHREIB-Stellen mitersetzt: Sie tragen dieselbe `created_by`-Zeile im
Wortlaut, und can_read_track ist bei einem oeffentlichen Track fuer JEDEN
wahr. Eine Suchen-Ersetzen-Aenderung ueber artrack/routes/ macht den Track
damit fuer jeden authentifizierten Nutzer beschreibbar — Wegpunkte loeschen,
Routen aendern, Erzaehltexte ueberschreiben. Das faellt in keinem
Funktionstest auf, weil alles weiterhin "funktioniert".
"""

import re
import pathlib

import pytest

from artrack.collaboration_models import can_read_track, get_user_permissions


ROUTES_DIR = pathlib.Path(__file__).resolve().parent.parent / "artrack" / "routes"


class _Kollaborateur:
    def __init__(self, user_id, is_active=True, role="editor"):
        self.user_id = user_id
        self.is_active = is_active
        self.role = role
        # get_user_permissions liest diese Flags direkt aus dem Datensatz.
        self.can_add_waypoints = True
        self.can_edit_waypoints = True
        self.can_delete_waypoints = False
        self.can_invite_others = False
        self.can_edit_track = False


class _Track:
    def __init__(self, created_by=1, visibility="private", collaborators=None):
        self.created_by = created_by
        self.visibility = visibility
        self.collaborators = collaborators or []


class _Nutzer:
    def __init__(self, id, trust_level="user"):
        self.id = id
        self.trust_level = trust_level


# ── 1. Entscheidet can_read_track richtig? ──────────────────────────────────

def test_eigentuemer_darf_lesen():
    track = _Track(created_by=1, visibility="private")
    assert can_read_track(track, _Nutzer(1)) is True


def test_aktiver_kollaborateur_darf_privaten_track_lesen():
    """Die Sichtbarkeits-Variante der alten Pruefung konnte das NICHT —
    sie kannte nur Eigentuemer, Sichtbarkeit und Trust-Stufe."""
    track = _Track(created_by=1, visibility="private",
                   collaborators=[_Kollaborateur(user_id=2)])
    assert can_read_track(track, _Nutzer(2)) is True


def test_inaktiver_kollaborateur_darf_nicht_lesen():
    track = _Track(created_by=1, visibility="private",
                   collaborators=[_Kollaborateur(user_id=2, is_active=False)])
    assert can_read_track(track, _Nutzer(2)) is False


def test_fremder_darf_oeffentlichen_track_lesen():
    """Der Fall aus Issue #673 — genau der war an 13 Endpunkten gesperrt."""
    track = _Track(created_by=1, visibility="public")
    assert can_read_track(track, _Nutzer(99)) is True


def test_fremder_darf_privaten_track_nicht_lesen():
    track = _Track(created_by=1, visibility="private")
    assert can_read_track(track, _Nutzer(99)) is False


@pytest.mark.parametrize("stufe", ["admin", "moderator"])
def test_moderation_darf_privaten_track_lesen(stufe):
    """get_user_permissions allein kann das NICHT — es kennt keine
    Trust-Stufen. Waere alles darauf umgestellt worden, haetten Admins und
    Moderatoren ihren Zugriff auf private Tracks verloren."""
    track = _Track(created_by=1, visibility="private")
    assert can_read_track(track, _Nutzer(99, trust_level=stufe)) is True


def test_gewoehnlicher_nutzer_ist_kein_moderator():
    track = _Track(created_by=1, visibility="private")
    assert can_read_track(track, _Nutzer(99, trust_level="user")) is False


@pytest.mark.parametrize("track,nutzer", [
    (None, _Nutzer(1)),
    (_Track(), None),
    (None, None),
])
def test_fehlende_angaben_verweigern(track, nutzer):
    """Fail-closed: Wo nichts da ist, wird nicht gelesen."""
    assert can_read_track(track, nutzer) is False


# ── 1b. Der oeffentliche Leser darf NUR lesen ───────────────────────────────

@pytest.mark.parametrize("recht", [
    "can_add_waypoints",
    "can_edit_waypoints",
    "can_delete_waypoints",
    "can_invite_others",
    "can_edit_track",
    "can_manage_collaborators",
])
def test_oeffentlicher_leser_darf_nicht_schreiben(recht):
    """Von XCodeCodex zurecht als Pflicht-Gate verlangt.

    Der Struktur-Test weiter unten prueft, dass kein Schreibpfad
    can_read_track benutzt. Dieser hier prueft die andere Seite derselben
    Sache: dass der oeffentliche Leser ueberhaupt keine Schreibrechte
    besitzt. Beide zusammen decken den Fall ab — wuerde jemand einen
    Schreibpfad falsch verdrahten, muesste er ausserdem an einem Recht
    vorbei, das gar nicht gesetzt ist.
    """
    track = _Track(created_by=1, visibility="public")
    rechte = get_user_permissions(track, 99)
    assert rechte.can_view is True, "oeffentlich lesen muss erlaubt sein"
    assert getattr(rechte, recht) is not True, (
        f"oeffentlicher Leser haette {recht} — der Track waere fuer jeden "
        f"authentifizierten Nutzer veraenderbar"
    )
    assert rechte.is_owner is not True


# ── 2. Bleibt die Lese/Schreib-Trennung erhalten? ───────────────────────────

def _handler_bloecke():
    """Zerlegt alle Route-Dateien in (Datei, Zeile, HTTP-Verb, Rumpf)."""
    for pfad in sorted(ROUTES_DIR.glob("*.py")):
        zeilen = pfad.read_text().split("\n")
        dekoratoren = [
            (i, m.group(1))
            for i, l in enumerate(zeilen)
            if (m := re.match(r"\s*@router\.(get|post|put|patch|delete)\(", l))
        ]
        for idx, (i, verb) in enumerate(dekoratoren):
            ende = dekoratoren[idx + 1][0] if idx + 1 < len(dekoratoren) else len(zeilen)
            yield pfad.name, i + 1, verb, "\n".join(zeilen[i:ende])


def test_struktur_kein_schreibpfad_nutzt_can_read_track():
    """DER wichtige Test.

    can_read_track ist bei einem oeffentlichen Track fuer jeden wahr. Steht es
    in einem Schreibpfad, darf jeder authentifizierte Nutzer den Track
    veraendern. Schreibpfade gehoeren auf can_edit_waypoints / can_edit_track /
    is_owner — je nach Operation.
    """
    treffer = [
        f"{datei}:{zeile} ({verb.upper()})"
        for datei, zeile, verb, rumpf in _handler_bloecke()
        if verb != "get" and "can_read_track" in rumpf
    ]
    assert not treffer, (
        "can_read_track in Schreibpfad(en) — macht oeffentliche Tracks fuer "
        "jeden beschreibbar:\n  " + "\n  ".join(treffer)
    )


def test_struktur_kein_lesepfad_prueft_roh_auf_eigentuemer():
    """Rueckfall-Schutz gegen Issue #673.

    Ein neuer GET-Endpunkt, der wieder direkt auf created_by prueft, sperrt
    oeffentliche Tracks aus — und faellt niemandem auf, weil der Eigentuemer
    (und damit die Web-App mit ihrem Schluessel) weiterhin alles sieht.
    """
    treffer = [
        f"{datei}:{zeile}"
        for datei, zeile, verb, rumpf in _handler_bloecke()
        if verb == "get" and "created_by != current_user.id" in rumpf
    ]
    assert not treffer, (
        "GET-Endpunkt(e) pruefen roh auf created_by statt can_read_track — "
        "oeffentliche Tracks bleiben dort gesperrt:\n  " + "\n  ".join(treffer)
    )
