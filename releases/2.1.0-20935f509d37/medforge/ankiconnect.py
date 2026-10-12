"""MedForge AnkiConnect integration for direct deck synchronization."""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import genanki

from medforge.utils import slugify


# Default AnkiConnect endpoint
ANKICONNECT_URL = "http://127.0.0.1:8765"
ANKICONNECT_VERSION = 6


@dataclass
class AnkiConnectError(Exception):
    """Raised when AnkiConnect request fails."""
    message: str
    code: Optional[int] = None


def _ankiconnect_request(action: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Send a request to AnkiConnect."""
    payload = {"action": action, "version": ANKICONNECT_VERSION}
    if params:
        payload["params"] = params

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ANKICONNECT_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise AnkiConnectError(f"Cannot connect to AnkiConnect at {ANKICONNECT_URL}: {e}")
    except json.JSONDecodeError as e:
        raise AnkiConnectError(f"Invalid JSON response from AnkiConnect: {e}")

    if result.get("error"):
        raise AnkiConnectError(result["error"], code=result.get("code"))
    return result.get("result")


def check_ankiconnect() -> bool:
    """Check if AnkiConnect is available."""
    try:
        result = _ankiconnect_request("version")
        return result == ANKICONNECT_VERSION
    except AnkiConnectError:
        return False


def ensure_model(model_name: str = "MedForge Evidence Card") -> int:
    """Ensure the MedForge model exists, return its ID."""
    result = _ankiconnect_request("modelNames")
    if model_name in result:
        # Get model ID
        models = _ankiconnect_request("modelFieldNames", {"modelName": model_name})
        if models is not None:
            # Get model info to extract ID
            model_info = _ankiconnect_request("modelTemplates", {"modelName": model_name})
            # Find model ID by checking decks that use it
            return _get_model_id(model_name)
    # Create model
    model_id = int(hashlib.sha1(b"medforge-model-v2").hexdigest()[:8], 16)
    _ankiconnect_request("createModel", {
        "modelName": model_name,
        "inOrderFields": ["Question", "Answer", "Sources"],
        "isCloze": False,
        "cardTemplates": [{
            "Name": "Card 1",
            "Front": "{{Question}}",
            "Back": "{{FrontSide}}<hr id=answer>{{Answer}}<br><br><small>{{Sources}}</small>",
        }],
        "css": ".card { font-family: Arial; font-size: 22px; text-align:left; padding:18px; }",
    })
    return model_id


def _get_model_id(model_name: str) -> int:
    """Get model ID by creating a temporary note and checking its model ID."""
    # Use findNotes to get a note with this model, then get its model ID
    notes = _ankiconnect_request("findNotes", {"query": f"\"note:{model_name}\""})
    if notes:
        info = _ankiconnect_request("notesInfo", {"notes": notes[:1]})
        if info:
            return info[0].get("modelId", 0)
    # Fallback: create model if not found
    model_id = int(hashlib.sha1(b"medforge-model-v2").hexdigest()[:8], 16)
    return model_id


def ensure_deck(deck_name: str) -> int:
    """Ensure deck exists, return its ID."""
    result = _ankiconnect_request("deckNames")
    if deck_name in result:
        decks = _ankiconnect_request("deckNamesAndIds")
        return next(d for d in decks if d["name"] == deck_name)["id"]
    # Create deck
    deck_id = _ankiconnect_request("createDeck", {"deck": deck_name})
    return deck_id


def add_notes_to_deck(deck_name: str, notes: List[Dict[str, Any]]) -> List[int]:
    """Add multiple notes to a deck. Returns list of note IDs."""
    if not notes:
        return []
    note_ids = _ankiconnect_request("addNotes", {"notes": notes})
    return note_ids


def create_medforge_note(question: str, answer: str, sources: str,
                          topic: str, deck_name: str, model_name: str = "MedForge Evidence Card") -> Dict[str, Any]:
    """Create a note dict for AnkiConnect."""
    return {
        "deckName": deck_name,
        "modelName": "MedForge Evidence Card",
        "fields": {
            "Question": question,
            "Answer": answer,
            "Sources": sources,
        },
        "options": {
            "allowDuplicate": False,
            "duplicateScope": "deck",
            "duplicateScopeOptions": {
                "deckName": deck_name,
                "checkChildren": False,
                "checkAllModels": False,
            },
        },
        "tags": ["MedForge", slugify(topic), "NeedsReview"],
    }


def sync_deck_from_csv(csv_path: Path, deck_name: str, topic: str) -> Dict[str, Any]:
    """Sync a flashcards.csv to an Anki deck via AnkiConnect."""
    import csv
    from medforge.utils import slugify

    # Read CSV
    cards: List[Tuple[str, str, str]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 3:
                q, a, s = row[0], row[1], row[2]
                if q and a:
                    cards.append((q, a, s))

    if not cards:
        return {"added": 0, "updated": 0, "errors": ["no valid cards in CSV"]}

    # Ensure model and deck
    model_name = "MedForge Evidence Card"
    try:
        ensure_model(model_name)
    except AnkiConnectError as e:
        return {"added": 0, "updated": 0, "errors": [f"model error: {e}"]}

    try:
        deck_id = ensure_deck(deck_name)
    except AnkiConnectError as e:
        return {"added": 0, "updated": 0, "errors": [f"deck error: {e}"]}

    # Build notes
    notes = []
    for q, a, s in cards:
        notes.append(create_medforge_note(q, a, s, topic, deck_name))

    # Add notes
    try:
        note_ids = add_notes_to_deck(deck_name, notes)
        return {"added": len(note_ids), "updated": 0, "note_ids": note_ids}
    except AnkiConnectError as e:
        return {"added": 0, "updated": 0, "errors": [f"add notes error: {e}"]}


def sync_deck_from_product(product_path: Path, deck_name: Optional[str] = None) -> Dict[str, Any]:
    """Sync a generated product's flashcards.csv to Anki."""
    if deck_name is None:
        deck_name = f"MedForge::{product_path.parent.name}"

    csv_path = product_path / "flashcards.csv"
    if not csv_path.is_file():
        return {"added": 0, "updated": 0, "errors": [f"no flashcards.csv at {csv_path}"]}

    topic = product_path.parent.name
    return sync_deck_from_csv(csv_path, deck_name, topic)


def import_from_genanki(apkg_path: Path, deck_name: Optional[str] = None) -> Dict[str, Any]:
    """Import a genanki-generated .apkg into Anki via AnkiConnect.
    
    This reads the .apkg with genanki, extracts notes, and adds them via AnkiConnect.
    """
    import tempfile
    import zipfile
    
    if deck_name is None:
        deck_name = f"MedForge::{apkg_path.stem}"

    # Extract apkg
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(apkg_path, "r") as z:
            z.extractall(tmpdir)
        
        # Find the collection.anki2 SQLite database
        import sqlite3
        db_path = Path(tmpdir) / "collection.anki2"
        if not db_path.is_file():
            return {"added": 0, "errors": ["no collection.anki2 in apkg"]}
        
        con = sqlite3.connect(str(db_path))
        try:
            # Get notes with fields
            rows = con.execute(
                "SELECT mid, flds, tags FROM notes"
            ).fetchall()
        finally:
            con.close()

    if not rows:
        return {"added": 0, "errors": ["no notes in apkg"]}

    model_name = "MedForge Evidence Card"
    try:
        ensure_model(model_name)
    except AnkiConnectError as e:
        return {"added": 0, "errors": [f"model error: {e}"]}

    try:
        deck_id = ensure_deck(deck_name)
    except AnkiConnectError as e:
        return {"added": 0, "errors": [f"deck error: {e}"]}

    notes = []
    for mid, flds, tags in rows:
        fields = flds.split("\x1f")
        if len(fields) >= 3:
            notes.append(create_medforge_note(
                fields[0], fields[1], fields[2],
                "imported", deck_name
            ))

    try:
        note_ids = add_notes_to_deck(deck_name, notes)
        return {"added": len(note_ids), "note_ids": note_ids}
    except AnkiConnectError as e:
        return {"added": 0, "errors": [f"add notes error: {e}"]}


# Add hashlib import
import hashlib