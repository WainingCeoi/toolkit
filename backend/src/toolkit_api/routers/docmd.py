"""Doc to Markdown: convert PDFs, Office docs, and images with MinerU.

The conversion runs as a job (one MinerU subprocess per file). /health reports
whether the MinerU CLI and its ML backend (torch) are installed, probed per
request so importing this module never touches torch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, UploadFile
from pydantic import BaseModel

from toolkit_engine import docmd
from toolkit_engine.fsutil import dedupe_filenames

from ..deps import StateDep
from ..schemas import JobStartedOut
from ..uploads import read_uploads

router = APIRouter(prefix="/doc-to-markdown", tags=["doc-to-markdown"])


class MarkdownHealthOut(BaseModel):
    mineru: bool
    backend_ready: bool


class _CancelledError(Exception):
    """Raised by the progress callback to stop a cancelled job between items."""


@router.get("/health", response_model=MarkdownHealthOut)
def health() -> MarkdownHealthOut:
    # The base `mineru` package ships the CLI but no ML backend — torch (and
    # the pipeline/vlm deps) live in optional extras. Detect that up front so
    # the UI can warn before a conversion fails deep inside the subprocess.
    return MarkdownHealthOut(
        mineru=docmd.find_mineru() is not None,
        backend_ready=importlib.util.find_spec("torch") is not None,
    )


@router.post("", response_model=JobStartedOut)
def convert(
    state: StateDep,
    files: list[UploadFile] | None = None,
    backend: Annotated[str, Form()] = "hybrid-engine",
    method: Annotated[str, Form()] = "auto",
    lang: Annotated[str, Form()] = "ch",
    effort: Annotated[str, Form()] = "medium",
    formula: Annotated[bool, Form()] = True,
    table: Annotated[bool, Form()] = True,
) -> JobStartedOut:
    if not files:
        raise HTTPException(
            status_code=400,
            detail="❌ Please select at least one file first.",
        )
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower().lstrip(".")
        if suffix not in docmd.ACCEPTED_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"❌ Unsupported file type: {upload.filename}. "
                    f"Accepted: {', '.join(docmd.ACCEPTED_TYPES)}"
                ),
            )
    mineru_cmd = docmd.find_mineru()
    if mineru_cmd is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required tool: MinerU (`uv add mineru`).",
        )

    # Uploads are request-scoped — read every file (size-capped) before
    # returning. Disambiguate duplicate basenames so two same-named uploads
    # don't collide to one tree in the result zip (dropping one output).
    unique_names = dedupe_filenames([upload.filename for upload in files])
    named = list(zip(unique_names, read_uploads(files), strict=True))
    options = {
        "backend": backend,
        "method": method,
        "lang": lang,
        "effort": effort,
        "formula": formula,
        "table": table,
    }

    def worker(job):
        def on_progress(pct, text):
            if job.cancelled:
                raise _CancelledError
            job.set_message(text)

        try:
            zip_bytes, done, failed = docmd.convert_batch(
                named, options, on_progress, mineru_cmd
            )
        except _CancelledError:
            return None

        # Key per-item state by input index — two uploads with the same name
        # must not cross-contaminate each other's success/failure state.
        failed_by_idx = {idx: error for idx, _name, error in failed}
        for idx in range(len(named)):
            if idx in failed_by_idx:
                job.update_item(
                    idx, pct=100, state="failed", error=failed_by_idx[idx]
                )
            else:
                job.update_item(idx, pct=100, state="done")

        result = {
            "done": done,
            "failed": [(name, error) for _idx, name, error in failed],
        }
        if zip_bytes is not None:
            result["artifact_id"] = state.artifacts.put_bytes(
                "markdown.zip", zip_bytes, "application/zip"
            )
            result["filename"] = "markdown.zip"
        return result

    job = state.jobs.submit("doc-to-markdown", [name for name, _ in named], worker)
    return JobStartedOut(job_id=job.id)
