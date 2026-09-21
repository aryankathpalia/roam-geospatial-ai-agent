---
title: ROAM Backend
emoji: 🗺️
colorFrom: green
colorTo: blue
sdk: docker
pinned: false
---

# ROAM Backend

FastAPI backend for ROAM (Reasoning-Oriented Agent for Maps) — a geospatial
document intelligence and spatial validation agent.

Current focus: land records / deeds / survey documents → OCR + document
understanding → extract geospatial entities/attributes → reconstruct
GIS-ready geometry → spatial validation → map + report + GeoJSON.

Current pipeline: PDF inspection → page rendering → a fine-tuned
DocLayout-YOLO layout model (6 classes: Text, Table, Picture, Seal,
ParcelMap, ScannedPrintout) → per-class region cropping. Per-class OCR/
vision extraction and spatial validation are in progress.
