"""Image to PDF: combine uploaded images into a single downloadable PDF.

Thin over toolkit_engine.imgpdf — the validations and their exact messages
(❌ included) carry over from the Streamlit page; the page's Desktop write
becomes a direct download response.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from toolkit_engine.imgpdf import images_to_pdf_bytes

router = APIRouter(tags=["img-to-pdf"])


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
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
