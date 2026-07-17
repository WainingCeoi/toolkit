"""Native folder picking — same-machine only (osascript dialog on this Mac)."""

from __future__ import annotations

from fastapi import APIRouter

from toolkit_engine.picker import pick_folder

from ..schemas import PickFolderIn, PickFolderOut

router = APIRouter(prefix="/fs", tags=["fs"])


@router.post("/pick-folder", response_model=PickFolderOut)
def pick(req: PickFolderIn) -> PickFolderOut:
    # Blocking is fine: FastAPI runs sync endpoints in a threadpool, and the
    # dialog blocks only its own request while the user picks.
    picked = pick_folder(req.start_dir or None)
    return PickFolderOut(path=picked or None)
