"""Read conversations from the native WhatsApp for Mac database."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict
from zipfile import ZIP_DEFLATED, ZipFile

from .whatsapp_media import archive_media, safe_export_name
from .whatsapp_proto import bytes_fields
from .whatsapp_reactions import decode_reactions

DATABASE = (
    Path.home()
    / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)
APPLE_EPOCH = 978307200
_DIRECT_DOMAINS = {"s.whatsapp.net", "c.us", "lid", "hosted", "hosted.lid"}
_ATTACHMENT_TYPES = {1, 2, 3, 8, 9, 11, 15, 60}
_DATABASE_ERROR = (
    "Unable to read the local WhatsApp database. Check that WhatsApp for Mac "
    "has synced and Toolkit has permission to access its files."
)
_MEDIA_COLUMNS = (
    "ZMEDIALOCALPATH",
    "ZTITLE",
    "ZAUTHORNAME",
    "ZFILESIZE",
    "ZTHUMBNAILLOCALPATH",
    "ZXMPPTHUMBPATH",
    "ZVCARDNAME",
    "ZVCARDSTRING",
    "ZLATITUDE",
    "ZLONGITUDE",
    "ZMOVIEDURATION",
)

_CHAT_COLUMNS = "Z_PK, ZPARTNERNAME, ZCONTACTJID"
_MESSAGE_QUERY = """
    SELECT m.Z_PK, m.ZSTANZAID, m.ZMESSAGEDATE, m.ZTEXT, m.ZMESSAGETYPE,
           m.ZISFROMME, m.ZFROMJID, m.ZPARENTMESSAGE, g.ZMEMBERJID, i.ZMETADATA,
           c.ZCONTACTJID, c.ZPARTNERNAME,
           {member_name} AS ZMEMBERNAME, {profile_name} AS ZPROFILENAME,
           {media_fields}, {receipt_field} AS ZRECEIPTINFO
    FROM ZWAMESSAGE m
    JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAMEDIAITEM i ON i.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER
    {receipt_join}
    WHERE m.ZCHATSESSION = ?
    ORDER BY m.ZSORT, m.Z_PK
"""


class WhatsAppDatabaseError(RuntimeError):
    """The local database cannot be opened or read."""


class WhatsAppChatNotFoundError(LookupError):
    """The requested ID does not identify an exportable conversation."""


class ChatInfo(TypedDict):
    id: int
    name: str
    type: Literal["direct", "group"]


class _Quote(TypedDict):
    message_id: str
    quoted_author_id: str | None
    quoted_text: str | None


@contextmanager
def _read_database(db_path: Path | None) -> Iterator[sqlite3.Connection]:
    try:
        uri = (db_path or DATABASE).expanduser().resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as error:
        raise WhatsAppDatabaseError(_DATABASE_ERROR) from error

    with closing(connection) as db:
        try:
            db.row_factory = sqlite3.Row
            # A single snapshot includes committed WAL entries while WhatsApp is open.
            db.execute("BEGIN")
            yield db
        except sqlite3.Error as error:
            raise WhatsAppDatabaseError(_DATABASE_ERROR) from error


def _chat_info(row: sqlite3.Row) -> ChatInfo | None:
    jid = row["ZCONTACTJID"] or ""
    _, separator, domain = jid.lower().rpartition("@")
    if not separator:
        return None
    chat_type: Literal["direct", "group"]
    if domain == "g.us":
        chat_type = "group"
    elif domain in _DIRECT_DOMAINS:
        chat_type = "direct"
    else:
        return None
    return {
        "id": row["Z_PK"],
        "name": (row["ZPARTNERNAME"] or "").strip() or jid,
        "type": chat_type,
    }


def list_chats(db_path: Path | None = None) -> list[ChatInfo]:
    """Return only the metadata needed to choose a direct or group chat."""
    with _read_database(db_path) as db:
        rows = db.execute(f"SELECT {_CHAT_COLUMNS} FROM ZWACHATSESSION")
        chats = [chat for row in rows if (chat := _chat_info(row)) is not None]
    return sorted(chats, key=lambda chat: (chat["name"].casefold(), chat["id"]))


def _decode_quote(metadata: bytes | None) -> _Quote | None:
    if metadata is None:
        return None
    if not isinstance(metadata, bytes):
        raise ValueError("Invalid WhatsApp media metadata")
    fields = bytes_fields(metadata, {5, 6, 19})
    stanza = fields.get(5, b"")
    if not (10 <= len(stanza) <= 64 and stanza.isalnum()):
        return None
    author = fields.get(6, b"").decode("utf-8", errors="replace")
    # Field 19 can contain an embedded message of a non-text type.
    try:
        text = bytes_fields(fields.get(19, b""), {1}).get(1)
        snapshot = text.decode("utf-8") if text is not None else None
    except ValueError:
        snapshot = None
    return {
        "message_id": stanza.decode("ascii"),
        "quoted_author_id": author if "@" in author else None,
        "quoted_text": snapshot,
    }


def _sent_at(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value + APPLE_EPOCH, UTC).isoformat()
    except OverflowError, OSError, ValueError:
        return None  # One unreadable date must not fail the whole export.


def _sender_name(row: sqlite3.Row, sender_id: object, from_me: bool) -> str | None:
    if from_me:
        return "Me"
    if not isinstance(sender_id, str):
        return None
    user, _, domain = sender_id.rpartition("@")
    if not user or domain.lower() not in _DIRECT_DOMAINS:
        return None

    direct_name = row["ZPARTNERNAME"] if sender_id == row["ZCONTACTJID"] else None
    member_name = row["ZMEMBERNAME"] if sender_id == row["ZMEMBERJID"] else None
    for value in (direct_name, member_name, row["ZPROFILENAME"]):
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if isinstance(value, str) and (name := value.strip()):
            if name not in (sender_id, user):
                return name
    return None


def _export_message(row: sqlite3.Row, by_stanza: dict[str, sqlite3.Row | None]) -> dict:
    from_me = bool(row["ZISFROMME"])
    sender_id = row["ZMEMBERJID"] or row["ZFROMJID"]
    record = {
        "row_id": row["Z_PK"],
        "message_id": row["ZSTANZAID"],
        "sent_at": _sent_at(row["ZMESSAGEDATE"]),
        "from_me": from_me,
        "sender_id": sender_id,
        "sender_name": _sender_name(row, sender_id, from_me),
        "text": row["ZTEXT"],
        "message_type": row["ZMESSAGETYPE"],
        "reply_to": None,
    }
    try:
        quote = _decode_quote(row["ZMETADATA"])
    except ValueError:
        quote = None
        record["metadata_status"] = "unrecognized_format"
    if quote is not None:
        stanza = quote["message_id"]
        match = by_stanza.get(stanza)
        if match is not None:
            record["reply_to"] = {
                **quote,
                "status": "matched",
                "row_id": match["Z_PK"],
                "original_text": match["ZTEXT"],
            }
        else:
            record["reply_to"] = {
                **quote,
                "status": "ambiguous" if stanza in by_stanza else "missing_in_chat",
                "row_id": None,
            }
    # Core Data's parent relationship is also used by features other than replies.
    if row["ZPARENTMESSAGE"] is not None:
        record["database_parent_row_id"] = row["ZPARENTMESSAGE"]

    message_type = row["ZMESSAGETYPE"]
    if message_type != 8 and isinstance(caption := row["ZTITLE"], str) and caption:
        record["media_caption"] = caption
    if message_type in (2, 3) and (duration := row["ZMOVIEDURATION"]):
        record["media_duration_seconds"] = duration
    if message_type == 4 and isinstance(card := row["ZVCARDSTRING"], str) and card:
        record["contact_card"] = {"name": row["ZVCARDNAME"], "vcard": card}
    if message_type == 5:
        lat, lon = row["ZLATITUDE"], row["ZLONGITUDE"]
        if (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            record["location"] = {"latitude": lat, "longitude": lon}
    return record


def _read_chat(db: sqlite3.Connection, chat_id: int) -> ChatInfo:
    row = db.execute(
        f"SELECT {_CHAT_COLUMNS} FROM ZWACHATSESSION WHERE Z_PK = ?", (chat_id,)
    ).fetchone()
    chat = _chat_info(row) if row is not None else None
    if chat is None:
        raise WhatsAppChatNotFoundError(f"WhatsApp chat {chat_id} could not be found.")
    return chat


def _message_query(db: sqlite3.Connection) -> str:
    def columns(table: str) -> set[str]:
        rows = db.execute("SELECT name FROM pragma_table_info(?)", (table,))
        return {row[0] for row in rows}

    media_columns = columns("ZWAMEDIAITEM")
    media_fields = ", ".join(
        f"i.{name}" if name in media_columns else f"NULL AS {name}"
        for name in _MEDIA_COLUMNS
    )
    has_receipts = "ZRECEIPTINFO" in columns("ZWAMESSAGEINFO")
    has_receipts = has_receipts and "ZMESSAGEINFO" in columns("ZWAMESSAGE")
    # Decode optional names separately so malformed text cannot stop the export.
    member_name = "NULL"
    if "ZCONTACTNAME" in columns("ZWAGROUPMEMBER"):
        member_name = """CASE WHEN typeof(g.ZCONTACTNAME) = 'text'
                         THEN CAST(g.ZCONTACTNAME AS BLOB) END"""
    profile_name = "NULL"
    if {"ZJID", "ZPUSHNAME"} <= columns("ZWAPROFILEPUSHNAME"):
        profile_name = """(
            SELECT CAST(MIN(TRIM(p.ZPUSHNAME)) AS BLOB)
            FROM ZWAPROFILEPUSHNAME p
            WHERE p.ZJID = COALESCE(NULLIF(g.ZMEMBERJID, ''), m.ZFROMJID)
              AND typeof(p.ZPUSHNAME) = 'text' AND TRIM(p.ZPUSHNAME) <> ''
            HAVING COUNT(DISTINCT TRIM(p.ZPUSHNAME)) = 1
        )"""
    return _MESSAGE_QUERY.format(
        member_name=member_name,
        profile_name=profile_name,
        media_fields=media_fields,
        receipt_field="info.ZRECEIPTINFO" if has_receipts else "NULL",
        receipt_join=(
            "LEFT JOIN ZWAMESSAGEINFO info ON info.Z_PK = m.ZMESSAGEINFO"
            if has_receipts
            else ""
        ),
    )


def _read_messages(
    db: sqlite3.Connection,
    query: str,
    chat_id: int,
    db_path: Path,
    archive: ZipFile,
    chat_folder: str,
) -> list[dict]:
    rows = db.execute(query, (chat_id,)).fetchall()
    by_stanza: dict[str, sqlite3.Row | None] = {}
    for row in rows:
        stanza = row["ZSTANZAID"]
        if stanza:
            # A present entry with no row records multiple matches in this chat.
            by_stanza[stanza] = None if stanza in by_stanza else row

    messages = []
    for row in rows:
        message = _export_message(row, by_stanza)
        if reactions := decode_reactions(row["ZRECEIPTINFO"]):
            message["reactions"] = reactions
        local_path = row["ZMEDIALOCALPATH"]
        thumbnail_path = row["ZTHUMBNAILLOCALPATH"] or row["ZXMPPTHUMBPATH"]
        message_type = row["ZMESSAGETYPE"]
        file_size = row["ZFILESIZE"]
        expected = (
            bool(local_path)
            or (isinstance(file_size, (int, float)) and file_size > 0)
            or message_type in _ATTACHMENT_TYPES
        )
        if expected or thumbnail_path:
            message.update(
                archive_media(
                    archive,
                    db_path=db_path,
                    chat_folder=chat_folder,
                    row_id=row["Z_PK"],
                    local_path=local_path,
                    title=(row["ZAUTHORNAME"] or row["ZTITLE"])
                    if message_type == 8
                    else None,
                    expected=expected,
                    thumbnail_path=thumbnail_path,
                )
            )
        messages.append(message)
    return messages


def _chat_metadata(chat: ChatInfo) -> dict[str, int | str]:
    return {"chat_id": chat["id"], "chat_name": chat["name"]}


def _json_safe(value: object) -> object:
    """Blank values JSON cannot hold, so one odd database cell cannot fail an export."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes) or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        return None
    return value


def write_chat_archive(
    chat_id: int, destination: BinaryIO, db_path: Path | None = None
) -> str:
    """Export one chat as JSONL and media files, returning the ZIP filename."""
    db_path = db_path or DATABASE
    with (
        _read_database(db_path) as db,
        ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive,
    ):
        chat = _read_chat(db, chat_id)
        filename = safe_export_name(f"WhatsApp_{chat['name']}", ".zip")
        chat_folder = filename.removesuffix(".zip")
        archive.mkdir(f"{chat_folder}/media")
        query = _message_query(db)
        messages = _read_messages(
            db, query, chat_id, db_path, archive=archive, chat_folder=chat_folder
        )
        with archive.open(
            f"{chat_folder}/messages.jsonl", "w", force_zip64=True
        ) as member:
            for record in chain((_chat_metadata(chat),), messages):
                line = json.dumps(_json_safe(record), ensure_ascii=False) + "\n"
                member.write(line.encode("utf-8"))
    return filename
