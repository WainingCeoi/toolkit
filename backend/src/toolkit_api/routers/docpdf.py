"""Doc to PDF: clean Word docs (accept changes, drop comments), export to PDF."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from toolkit_engine import docpdf
from toolkit_engine.fsutil import dedupe_filenames

from ..deps import StateDep
from ..schemas import JobStartedOut
from ..uploads import read_uploads

router = APIRouter(prefix="/doc-to-pdf", tags=["doc-to-pdf"])


class _CancelledError(Exception):
    """Raised by the progress callback to stop a cancelled job between items."""


@router.post("", response_model=JobStartedOut)
def convert(state: StateDep, files: list[UploadFile] | None = None) -> JobStartedOut:
    if not files:
        raise HTTPException(
            status_code=400,
            detail="❌ Please select at least one Word (.docx) file first.",
        )
    for upload in files:
        if Path(upload.filename or "").suffix.lower() != ".docx":
            raise HTTPException(
                status_code=400,
                detail="❌ Only Word (.docx) files are supported.",
            )
    soffice = docpdf.find_soffice()
    if soffice is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing required tool: LibreOffice (`brew install --cask libreoffice`)"
            ),
        )

    # Uploads are request-scoped: read them all before returning.
    unique_names = dedupe_filenames([upload.filename for upload in files])
    named = list(zip(unique_names, read_uploads(files), strict=True))

    def worker(job):
        def on_progress(pct, text):
            if job.cancelled:
                raise _CancelledError
            job.set_message(text)

        # One shared LibreOffice profile — conversions must not overlap. Poll the
        # lock rather than block on it, so a queued job still answers Cancel.
        while not state.soffice_lock.acquire(timeout=0.5):
            if job.cancelled:
                return None
        try:
            zip_bytes, done, failed = docpdf.convert_batch(
                named, on_progress, soffice, lambda: job.cancelled
            )
        except _CancelledError:
            return None
        finally:
            state.soffice_lock.release()

        failed_by_idx = {idx: error for idx, _name, error in failed}
        for idx in range(len(named)):
            if idx in failed_by_idx:
                job.update_item(idx, pct=100, state="failed", error=failed_by_idx[idx])
            else:
                job.update_item(idx, pct=100, state="done")

        result = {
            "done": done,
            "failed": [(name, error) for _idx, name, error in failed],
        }
        if zip_bytes is not None:
            result["artifact_id"] = state.artifacts.put_bytes(
                "converted_pdfs.zip", zip_bytes, "application/zip"
            )
            result["filename"] = "converted_pdfs.zip"
        return result

    job = state.jobs.submit("doc-to-pdf", [name for name, _ in named], worker)
    return JobStartedOut(job_id=job.id)
