"""Image to PDF: combine uploaded images into a single downloadable PDF.

Thin over toolkit_engine.imgpdf — the validations and their exact messages
(❌ included) carry over from the Streamlit page; the page's Desktop write
becomes a direct download response.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from toolkit_engine.imgpdf import images_to_pdf_bytes

router = APIRouter(tags=["img-to-pdf"])


def _content_disposition(out_name: str) -> str:
    """Build a Content-Disposition header value that survives latin-1 encoding.

    Mirrors Starlette's FileResponse: latin-1-safe names keep the ``filename="..."``
    form (with backslash/quote escaped), while non-latin-1 names fall back to the
    RFC 5987 ``filename*=utf-8''<percent-encoded>`` form.
    """
    try:
        out_name.encode("latin-1")
    except UnicodeEncodeError:
        return f"attachment; filename*=utf-8''{quote(out_name)}"
    escaped = out_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{escaped}"'


@router.post("/img-to-pdf")
def img_to_pdf(
    name: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> Response:
    if not files:
        raise HTTPException(
            status_code=400, detail="❌ Please select at least one image first."
        )
    if not name.strip():
        raise HTTPException(status_code=400, detail="❌ Please enter a PDF file name.")

    out_name = name.strip()
    if not out_name.lower().endswith(".pdf"):
        out_name += ".pdf"

    named_files = [(f.filename or "", f.file.read()) for f in files]
    try:
        pdf_bytes = images_to_pdf_bytes(named_files)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"❌ An error occurred: {e}") from e

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(out_name)},
    )
