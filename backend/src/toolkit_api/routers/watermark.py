"""Watermark Remover: propose masks, take the human-corrected ones, inpaint.

Thin over the `watermark` engine package. The flow is upload → auto-mask (one
PNG per image, refetched as the sensitivity slider moves) → run with the
human-approved masks (base64 PNGs, white = remove). Results go through the
shared artifact store, progress through the shared job registry. The lama
inpainter is probed with find_spec per request, so importing this module —
and therefore creating the app — never touches torch.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from toolkit_engine.fsutil import dedupe_filenames
from watermark import imgio
from watermark.detect import DEFAULT_SENSITIVITY, propose_mask
from watermark.inpaint import DEVICE_ENV, INPAINTERS, get_inpainter, lama_available
from watermark.pipeline import DEFAULT_DILATE_PX, IMAGE_TYPES, remove_watermark

from ..deps import StateDep, WatermarksDep
from ..schemas import JobStartedOut
from ..uploads import read_uploads

router = APIRouter(prefix="/watermark", tags=["watermark"])

# The review step renders every image on its own canvas editor; past ~20 the
# page (and the person correcting masks) is the bottleneck, not the backend.
MAX_IMAGES = 20


class WatermarkImageOut(BaseModel):
    id: str
    name: str
    width: int
    height: int


class WatermarkBatchOut(BaseModel):
    batch_id: str
    images: list[WatermarkImageOut]


class WatermarkHealthOut(BaseModel):
    lama: bool
    device: str


class WatermarkRunIn(BaseModel):
    batch_id: str
    inpainter: str = "lama"
    # image id -> base64 PNG of the human-approved mask (white = remove).
    # Only listed images are processed, so deselecting one is just omitting it.
    masks: dict[str, str]
    dilate_px: int = DEFAULT_DILATE_PX


@router.get("/health", response_model=WatermarkHealthOut)
def health() -> WatermarkHealthOut:
    return WatermarkHealthOut(
        lama=lama_available(),
        device=os.environ.get(DEVICE_ENV, "cpu"),
    )


@router.post("/batch", response_model=WatermarkBatchOut)
def create_batch(
    watermarks: WatermarksDep,
    files: list[UploadFile] | None = None,
) -> WatermarkBatchOut:
    if not files:
        raise HTTPException(
            status_code=400, detail="❌ Please select at least one image first."
        )
    if len(files) > MAX_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"❌ Too many images ({len(files)}) — "
                f"the limit is {MAX_IMAGES} per batch."
            ),
        )
    names = []
    for upload in files:
        # Basenames only: the name comes back as a zip entry and a download
        # filename, so a "../" smuggled in here must die at the door.
        safe = Path(upload.filename or "").name
        if not safe:
            raise HTTPException(status_code=400, detail="❌ Invalid filename.")
        if safe.rsplit(".", 1)[-1].lower() not in IMAGE_TYPES or "." not in safe:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"❌ Unsupported file type: {safe}. "
                    f"Accepted: {', '.join(IMAGE_TYPES)}"
                ),
            )
        names.append(safe)

    staged = []
    for name, content in zip(dedupe_filenames(names), read_uploads(files), strict=True):
        try:
            rgb = imgio.load_rgb(content)
        except Exception as e:  # Pillow's decode errors are many and unhelpful
            raise HTTPException(
                status_code=400, detail=f"❌ Could not read {name}: {e}"
            ) from e
        height, width = rgb.shape[:2]
        staged.append((name, imgio.encode_png(rgb), width, height))

    batch = watermarks.create(staged)
    return WatermarkBatchOut(
        batch_id=batch["id"],
        images=[
            WatermarkImageOut(
                id=entry["id"],
                name=entry["name"],
                width=entry["width"],
                height=entry["height"],
            )
            for entry in batch["images"]
        ],
    )


@router.get("/{batch_id}/{image_id}/image")
def working_copy(
    batch_id: str, image_id: str, watermarks: WatermarksDep
) -> FileResponse:
    """The normalized (EXIF-upright, RGB) PNG the canvas editor draws on."""
    entry = watermarks.image(batch_id, image_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown or expired batch.")
    return FileResponse(entry["path"], media_type="image/png")


@router.get("/{batch_id}/{image_id}/mask")
def auto_mask(
    batch_id: str,
    image_id: str,
    watermarks: WatermarksDep,
    sensitivity: Annotated[int, Query(ge=0, le=100)] = DEFAULT_SENSITIVITY,
) -> Response:
    """The proposed mask as a PNG (white = watermark), recomputed per call."""
    entry = watermarks.image(batch_id, image_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown or expired batch.")
    rgb = imgio.load_rgb(entry["path"].read_bytes())
    mask = propose_mask(rgb, sensitivity)
    return Response(content=imgio.encode_png(mask), media_type="image/png")


@router.post("/run", response_model=JobStartedOut)
def run(req: WatermarkRunIn, state: StateDep, watermarks: WatermarksDep):
    if req.inpainter not in INPAINTERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"❌ Unknown inpainter: {req.inpainter}. "
                f"Choose from: {', '.join(INPAINTERS)}."
            ),
        )
    if req.inpainter == "lama" and not lama_available():
        raise HTTPException(
            status_code=400,
            detail=(
                "❌ The LaMa inpainter needs torch — run "
                "`uv sync --extra watermark` (or `make install`), "
                "or pick the cv2 inpainter."
            ),
        )
    if not 0 <= req.dilate_px <= 64:
        raise HTTPException(
            status_code=400, detail="❌ dilate_px must be between 0 and 64."
        )
    batch = watermarks.get(req.batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired batch — upload the images again.",
        )
    if not req.masks:
        raise HTTPException(
            status_code=400, detail="❌ No masks to apply — nothing was selected."
        )
    known = {entry["id"] for entry in batch["images"]}
    unknown = sorted(set(req.masks) - known)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"❌ Unknown image id(s): {', '.join(unknown)}.",
        )
    # Decode every mask up front so a malformed payload is a 400 now, not a
    # failed job later.
    masks: dict[str, bytes] = {}
    for image_id, encoded in req.masks.items():
        try:
            masks[image_id] = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"❌ The mask for image {image_id} is not valid base64.",
            ) from e

    selected = [entry for entry in batch["images"] if entry["id"] in masks]
    # Output names are the input stems as .png; two stems can collide even
    # after upload dedup ("a.png" + "a.jpg"), so dedupe again on the way out.
    out_names = dedupe_filenames(
        [f"{Path(entry['name']).stem}.png" for entry in selected]
    )

    def worker(job):
        inpaint = get_inpainter(req.inpainter)
        if req.inpainter == "lama":
            job.set_message("Loading LaMa — the first run downloads a ~200 MB model…")
        done: list[str] = []
        failed: list[tuple[str, str]] = []
        file_results: list[dict] = []
        buffer = io.BytesIO()
        archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED)
        # Pinned for the whole run: images are read lazily, one per iteration,
        # so an unpinned batch could be swept between two of its own images.
        with watermarks.pin(req.batch_id):
            for idx, (entry, out_name) in enumerate(
                zip(selected, out_names, strict=True)
            ):
                if job.cancelled:
                    break
                job.update_item(idx, state="running")
                job.set_message(
                    f"Inpainting {idx + 1}/{len(selected)} — {entry['name']}…"
                )
                try:
                    rgb = imgio.load_rgb(entry["path"].read_bytes())
                    mask = imgio.load_mask(masks[entry["id"]], rgb.shape[:2])
                    png = imgio.encode_png(
                        remove_watermark(rgb, mask, inpaint, req.dilate_px)
                    )
                except Exception as e:  # noqa: BLE001 — per-file, batch goes on
                    job.update_item(idx, pct=100, state="failed", error=str(e))
                    failed.append((entry["name"], str(e)))
                    continue
                artifact_id = state.artifacts.put_bytes(out_name, png, "image/png")
                archive.writestr(out_name, png)
                file_results.append(
                    {
                        "image_id": entry["id"],
                        "name": out_name,
                        "artifact_id": artifact_id,
                        # Dimensions travel with the result so the before/after
                        # view can size itself from the snapshot alone.
                        "width": entry["width"],
                        "height": entry["height"],
                    }
                )
                done.append(out_name)
                job.update_item(idx, pct=100, state="done")
        archive.close()

        # batch_id rides along so the results view survives a page unmount:
        # the snapshot outlives this page's local state, and the "before"
        # image is fetched from the batch.
        result = {
            "batch_id": req.batch_id,
            "done": done,
            "failed": failed,
            "files": file_results,
        }
        if done:
            result["artifact_id"] = state.artifacts.put_bytes(
                "cleaned_images.zip", buffer.getvalue(), "application/zip"
            )
            result["filename"] = "cleaned_images.zip"
        return result

    job = state.jobs.submit("watermark", [entry["name"] for entry in selected], worker)
    return JobStartedOut(job_id=job.id)
