"""Shared Pydantic schemas (jobs, tools manifest, health).

Per-tool request/response models live next to their router so each tool
stays self-contained; the cross-tool contracts live here.
"""

from __future__ import annotations

from pydantic import BaseModel


class JobItemOut(BaseModel):
    name: str
    pct: int
    state: str  # pending | running | done | failed
    error: str | None = None


class JobOut(BaseModel):
    id: str
    tool: str
    state: str  # running | done | failed | cancelled
    message: str
    items: list[JobItemOut]
    result: dict | None = None
    error: str | None = None
    created_at: str


class JobStartedOut(BaseModel):
    job_id: str


class ToolOut(BaseModel):
    slug: str
    title: str  # emoji included, e.g. "🧲 Magnet Scraper"
    description: str


class CategoryOut(BaseModel):
    name: str  # emoji included, e.g. "🎬 Media"
    tools: list[ToolOut]


class HealthOut(BaseModel):
    ok: bool = True
    ffmpeg: bool
    soffice: bool
    mineru: bool


class PickFolderIn(BaseModel):
    start_dir: str | None = None


class PickFolderOut(BaseModel):
    path: str | None = None  # None when the user cancels the dialog
