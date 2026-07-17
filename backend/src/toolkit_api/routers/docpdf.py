"""Doc to PDF: clean Word docs (accept changes, drop comments), export to PDF.

The conversion runs as a job. LibreOffice conversions share one user profile,
so workers serialize on state.soffice_lock — they must not run concurrently.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from toolkit_engine import docpdf

from ..deps import StateDep
from ..schemas import JobStartedOut

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
                "Missing required tool: LibreOffice "
                "(`brew install --cask libreoffice`)"
            ),
        )

    # Uploads are request-scoped — read every file before returning.
    named = [(upload.filename, upload.file.read()) for upload in files]

    def worker(job):
        def on_progress(pct, text):
            if job.cancelled:
                raise _CancelledError
            job.set_message(text)

        # One shared LibreOffice profile — conversions must not overlap.
        with state.soffice_lock:
            try:
                zip_bytes, done, failed = docpdf.convert_batch(
                    named, on_progress, soffice
                )
            except _CancelledError:
                return None

        errors = {}
        for name, error in failed:
            errors.setdefault(name, error)
        for idx, (name, _) in enumerate(named):
            if name in errors:
                job.update_item(idx, pct=100, state="failed", error=errors[name])
            else:
                job.update_item(idx, pct=100, state="done")

        result = {"done": done, "failed": failed}
        if zip_bytes is not None:
            result["artifact_id"] = state.artifacts.put_bytes(
                "converted_pdfs.zip", zip_bytes, "application/zip"
            )
            result["filename"] = "converted_pdfs.zip"
        return result

    job = state.jobs.submit("doc-to-pdf", [name for name, _ in named], worker)
    return JobStartedOut(job_id=job.id)
