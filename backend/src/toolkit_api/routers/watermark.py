"""Watermark Remover: propose masks, take the human-corrected ones, inpaint."""

from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from toolkit_engine.fsutil import dedupe_filenames
from watermark import imgio
from watermark.detect import (
    AUTO,
    DEFAULT_DETECTOR,
    DEFAULT_SENSITIVITY,
    DETECTORS,
    PATTERN,
    collect_marks,
    propose_mask_detailed,
    repeating_evidence,
)
from watermark.inpaint import (
    INPAINTERS,
    get_inpainter,
    lama_available,
    resolve_device,
)
from watermark.pipeline import (
    DEFAULT_DILATE_PX,
    IMAGE_TYPES,
    CancelledError,
    remove_watermark,
    would_destroy_content,
)

from ..deps import StateDep, WatermarksDep
from ..schemas import JobStartedOut
from ..uploads import read_uploads

router = APIRouter(prefix="/watermark", tags=["watermark"])

# Past ~20 the canvas review page is the bottleneck, not the backend.
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
    # image id -> base64 PNG mask (white = remove); unlisted images are skipped.
    masks: dict[str, str]
    dilate_px: int = DEFAULT_DILATE_PX


@router.get("/health", response_model=WatermarkHealthOut)
def health() -> WatermarkHealthOut:
    # resolve_device imports torch; never call it at module import time.
    return WatermarkHealthOut(lama=lama_available(), device=resolve_device())


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
        # Basenames only: the name becomes a zip entry, so "../" must die here.
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


def _collect_marks(paths: list[Path]) -> list:
    """Marks the whole batch shares; independent of the sensitivity slider."""

    def each():
        # A generator: only one decoded image is held at a time.
        for path in paths:
            try:
                yield imgio.load_rgb(path.read_bytes())
            except Exception:  # noqa: BLE001,S112 — one unreadable copy is not fatal
                continue

    return collect_marks(each)


@router.get("/{batch_id}/{image_id}/mask")
def auto_mask(
    batch_id: str,
    image_id: str,
    watermarks: WatermarksDep,
    sensitivity: Annotated[int, Query(ge=0, le=100)] = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
) -> Response:
    """The proposed mask as a PNG (white = watermark), recomputed per call."""
    if detector not in DETECTORS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"❌ Unknown detector: {detector}. Choose from: {', '.join(DETECTORS)}."
            ),
        )
    entry = watermarks.image(batch_id, image_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown or expired batch.")
    rgb = imgio.load_rgb(entry["path"].read_bytes())
    marks = (
        watermarks.marks(batch_id, _collect_marks)
        if detector in (PATTERN, AUTO)
        else []
    )
    mask, used = propose_mask_detailed(rgb, sensitivity, detector, marks)
    return Response(
        content=imgio.encode_png(mask),
        media_type="image/png",
        headers={"X-Watermark-Detector": used},
    )


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
    # Decode up front so a malformed mask is a 400, not a failed job.
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
    # Stems can collide after upload dedup ("a.png" + "a.jpg"); dedupe again.
    out_names = dedupe_filenames(
        [f"{Path(entry['name']).stem}.png" for entry in selected]
    )

    def worker(job):
        inpaint = get_inpainter(req.inpainter)
        if req.inpainter == "lama":
            job.set_message("Loading LaMa — the first run downloads a ~200 MB model…")
        done: list[str] = []
        failed: list[tuple[str, str]] = []
        skipped: list[str] = []
        protected: list[str] = []
        cleaned: list[tuple[str, Path]] = []
        zip_id: str | None = None
        # Spooled to disk: outputs held in RAM would stack on LaMa's inpainting peak.
        # Inside the batch dir, which the store sweeps even after a kill mid-run.
        spool = Path(tempfile.mkdtemp(prefix="spool_", dir=batch["dir"]))

        def bundle(dest: Path) -> None:
            """Rebuild the zip of everything cleaned so far."""
            # ZIP_STORED: the members are PNGs, already compressed.
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as archive:
                for name, png in cleaned:
                    archive.write(png, arcname=name)

        def publish() -> dict:
            """Republish after every image, so a crash keeps what is already done."""
            nonlocal zip_id
            if cleaned:
                staging = spool / "cleaned_images.zip"
                bundle(staging)
                if zip_id is None:
                    zip_id = state.artifacts.put_file(
                        "cleaned_images.zip", staging, "application/zip"
                    )
                else:
                    state.artifacts.replace_file(zip_id, staging)
            partial = {
                "batch_id": req.batch_id,
                "done": list(done),
                "failed": list(failed),
                "skipped": list(skipped),
                "protected": list(protected),
            }
            if zip_id is not None:
                partial["artifact_id"] = zip_id
                partial["filename"] = "cleaned_images.zip"
            job.set_result(partial)
            return partial

        # Pin for the whole run, or the batch could be swept between two images.
        try:
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
                        if not mask.any():
                            # Not written back: an unchanged copy is not a result.
                            if repeating_evidence(rgb):
                                protected.append(entry["name"])
                            else:
                                skipped.append(entry["name"])
                            job.update_item(idx, pct=100, state="done")
                            publish()
                            continue
                        if would_destroy_content(rgb, mask, req.dilate_px):
                            protected.append(entry["name"])
                            job.update_item(idx, pct=100, state="done")
                            publish()
                            continue
                        spooled = spool / f"{idx}_{out_name}"
                        spooled.write_bytes(
                            imgio.encode_png(
                                remove_watermark(
                                    rgb,
                                    mask,
                                    inpaint,
                                    req.dilate_px,
                                    should_stop=lambda: job.cancelled,
                                )
                            )
                        )
                    except CancelledError:
                        break
                    except Exception as e:  # noqa: BLE001 — per-file, batch goes on
                        job.update_item(idx, pct=100, state="failed", error=str(e))
                        failed.append((entry["name"], str(e)))
                        publish()
                        continue
                    cleaned.append((out_name, spooled))
                    done.append(out_name)
                    job.update_item(idx, pct=100, state="done")
                    publish()

            return publish()
        finally:
            shutil.rmtree(spool, ignore_errors=True)

    job = state.jobs.submit("watermark", [entry["name"] for entry in selected], worker)
    return JobStartedOut(job_id=job.id)
