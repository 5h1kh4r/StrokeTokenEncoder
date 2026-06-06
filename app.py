from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory

from src.drawing_rng.stroke_token_encoder import encode_json_payload
import os
from supabase import create_client, Client

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "datasets"))
SAMPLES = DATA_DIR / "stroke_samples"
SAMPLES.mkdir(parents=True, exist_ok=True)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client | None = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
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

    if supabase is None:
        return jsonify({
            "error": "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        }), 500

    strokes = payload.get("strokes", [])
    if not isinstance(strokes, list) or len(strokes) == 0:
        return jsonify({"error": "No strokes submitted"}), 400

    row = {
        "participant_id": payload.get("participant_id"),
        "concept": payload.get("concept"),
        "redraw_id": payload.get("redraw_id"),
        "sample_name": payload.get("name") or payload.get("sample_name") or "sample",
        "notes": payload.get("notes", ""),

        "strokes": strokes,
        "params": payload.get("params", {}),
        "canvas_size": payload.get("canvas_size"),
        "serialized": payload.get("serialized"),

        "ui_version": payload.get("ui_version", "unknown"),
        "user_agent": request.headers.get("User-Agent", ""),
    }

    try:
        result = (
            supabase
            .table("stroke_samples")
            .insert(row)
            .execute()
        )

        inserted = result.data[0] if result.data else {}
        return jsonify({
            "ok": True,
            "id": inserted.get("id"),
            "message": "Saved to Supabase"
        })

    except Exception as exc:
        return jsonify({"error": f"Supabase insert failed: {exc}"}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
