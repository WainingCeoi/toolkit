"""List local WhatsApp chats and export selected conversations."""

from __future__ import annotations

from contextlib import ExitStack
from functools import partial
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from toolkit_engine import whatsapp

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


class ChatOut(BaseModel):
    id: int
    name: str
    type: Literal["direct", "group"]


class ChatsOut(BaseModel):
    chats: list[ChatOut]


ChatId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class ChatExportIn(BaseModel):
    chat_ids: list[ChatId] = Field(min_length=1)


def get_whatsapp_database_path() -> Path:
    return whatsapp.DATABASE


DatabasePath = Annotated[Path, Depends(get_whatsapp_database_path)]


@router.get("/chats")
def list_chats(response: Response, db_path: DatabasePath) -> ChatsOut:
    try:
        chats = whatsapp.list_chats(db_path=db_path)
    except whatsapp.WhatsAppDatabaseError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return ChatsOut(chats=[ChatOut(**chat) for chat in chats])


@router.post("/chats/export", response_class=Response)
def export_chats(selection: ChatExportIn, db_path: DatabasePath) -> Response:
    with ExitStack() as cleanup:
        try:
            archive = cleanup.enter_context(TemporaryFile())
            whatsapp.write_chat_archive(selection.chat_ids, archive, db_path)
            length = archive.tell()
            archive.seek(0)
        except whatsapp.WhatsAppChatNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except whatsapp.WhatsAppDatabaseError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=507,
                detail=(
                    "Unable to create the ZIP. Check disk space and media file "
                    "access on the Toolkit Mac."
                ),
            ) from exc

        response = StreamingResponse(
            iter(partial(archive.read, 1024 * 1024), b""),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{whatsapp.ARCHIVE_NAME}.zip"'
                ),
                "Content-Length": str(length),
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(archive.close),
        )
        cleanup.pop_all()
        return response
