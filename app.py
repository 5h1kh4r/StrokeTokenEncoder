from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory

from src.drawing_rng.stroke_token_encoder import encode_json_payload

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
import os

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "datasets"))
SAMPLES = DATA_DIR / "stroke_samples"
SAMPLES.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC), static_url_path="")


def safe_slug(value: str, fallback: str = "sample") -> str:
    value = str(value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value).strip("_")
    return value or fallback


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "Drawing-RNG stroke-token encoder v0.4-clean"})


@app.post("/api/tokenize")
def tokenize():
    payload = request.get_json(force=True, silent=False)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON body must be an object"}), 400
    try:
        result = encode_json_payload(payload)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/save_sample")
def save_sample():
    payload = request.get_json(force=True, silent=False)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON body must be an object"}), 400

    if not payload.get("consent_to_research"):
        return jsonify({"error": "consent_to_research must be true before saving"}), 400

    name = safe_slug(payload.get("name"), "sample")
    if not name.endswith(".json"):
        name += ".json"

    concept = safe_slug(payload.get("concept"), "unknown")
    participant_id = safe_slug(payload.get("participant_id"), "anon")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{stamp}_{participant_id}_{concept}_{name}"
    path = SAMPLES / filename

    data: Dict[str, Any] = {
        "schema_version": "drng-stroke-sample-v1",
        "name": name,
        "saved_at_utc": stamp,
        "participant_id": participant_id,
        "concept": concept,
        "concept_label": str(payload.get("concept_label") or concept),
        "redraw_id": payload.get("redraw_id"),
        "ui_version": payload.get("ui_version", "unknown"),
        "consent_to_research": True,
        "data_notice": "Anonymous stroke sample. Do not collect names, signatures, initials, passwords, or identifying drawings.",
        "canvas_size": payload.get("canvas_size"),
        "strokes": payload.get("strokes", []),
        "params": payload.get("params", {}),
        "serialized": payload.get("serialized"),
        "notes": payload.get("notes", ""),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "filename": filename, "path": str(path.relative_to(ROOT))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
