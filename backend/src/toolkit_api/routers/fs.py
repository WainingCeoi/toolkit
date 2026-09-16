"""Native folder picking — same-machine only (osascript dialog on this Mac)."""

from __future__ import annotations

from fastapi import APIRouter

from toolkit_engine.picker import pick_folder

from ..schemas import PickFolderIn, PickFolderOut

router = APIRouter(prefix="/fs", tags=["fs"])


@router.post("/pick-folder", response_model=PickFolderOut)
def pick(req: PickFolderIn) -> PickFolderOut:
    # Keep this a sync def so the blocking dialog runs on the threadpool.
    picked = pick_folder(req.start_dir or None, packages=req.packages)
    return PickFolderOut(path=picked or None)
