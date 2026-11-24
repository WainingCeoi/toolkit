"""WhatsApp exports against synthetic native-client SQLite tables."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import closing
from urllib.parse import unquote
from zipfile import ZipFile

import pytest

from toolkit_api.routers.whatsapp import get_whatsapp_database_path

CHAT_NAME = "同名会话"
UNSAFE_NAMES = ("周末🌿/计划", "周末🌿\\计划")
FIRST_STANZA = "SYNTHETICFIRST1"
DUPLICATE_STANZA = "SYNTHETICDUPLICATE"
DIRECT_SENDER = "100001@s.whatsapp.net"
GROUP_MEMBER = "100002@s.whatsapp.net"
OTHER_MEMBER = "100003@s.whatsapp.net"
SPECIAL_TEXT = '中文，emoji 😀, "双引号"\r\n第二行'


def _varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _bytes_field(number, value):
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _quote(stanza, author, excerpt):
    return (
        _bytes_field(5, stanza.encode())
        + _bytes_field(6, author.encode())
        + _bytes_field(19, _bytes_field(1, excerpt.encode()))
    )


@pytest.fixture
def whatsapp_db(tmp_path):
    path = tmp_path / "WhatsApp local #1" / "ChatStorage.sqlite"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZPARTNERNAME TEXT, ZCONTACTJID TEXT,
                ZSESSIONTYPE INTEGER, ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMETADATA BLOB);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZSTANZAID TEXT, ZMESSAGEDATE REAL,
                ZTEXT TEXT, ZMESSAGETYPE INTEGER, ZISFROMME INTEGER, ZFROMJID TEXT,
                ZPARENTMESSAGE INTEGER, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER,
                ZCHATSESSION INTEGER, ZSORT INTEGER
            );
            """)
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.executemany(
            "INSERT INTO ZWACHATSESSION VALUES (?, ?, ?, ?, ?)",
            [
                (17, CHAT_NAME, DIRECT_SENDER, 0, 200),
                (45, CHAT_NAME, "100@g.us", 1, 210),
                (61, UNSAFE_NAMES[0], "101@g.us", 1, 220),
                (62, UNSAFE_NAMES[1], "102@g.us", 1, 230),
            ],
        )
        writer.executemany(
            "INSERT INTO ZWAGROUPMEMBER VALUES (?, ?)",
            [(1, GROUP_MEMBER), (2, OTHER_MEMBER)],
        )
        writer.executemany(
            "INSERT INTO ZWAMEDIAITEM VALUES (?, ?)",
            [
                (1, _quote(FIRST_STANZA, DIRECT_SENDER, "hi")),
                (2, _quote(FIRST_STANZA, OTHER_MEMBER, SPECIAL_TEXT)),
                (3, _quote(DUPLICATE_STANZA, GROUP_MEMBER, "duplicate snapshot")),
                (4, b"\x2a\x7ftruncated"),
            ],
        )
        writer.executemany(
            """INSERT INTO ZWAMESSAGE (
                Z_PK, ZSTANZAID, ZSORT, ZMESSAGEDATE, ZTEXT, ZISFROMME,
                ZFROMJID, ZPARENTMESSAGE, ZMEDIAITEM, ZCHATSESSION, ZMESSAGETYPE
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 17, 0)""",
            [
                (102, FIRST_STANZA, 10, 120, "hi", 0, DIRECT_SENDER, None, None),
                (101, "SYNTHETICSECOND1", 20, 110, "11", 1, None, 102, None),
                (103, "SYNTHETICREPLY1", 30, 130, "引用", 1, None, 101, 1),
            ],
        )
        writer.executemany(
            """INSERT INTO ZWAMESSAGE (
                Z_PK, ZSTANZAID, ZSORT, ZTEXT, ZGROUPMEMBER, ZMEDIAITEM, ZMESSAGETYPE,
                ZCHATSESSION, ZISFROMME, ZFROMJID
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 45, 0, '100@g.us')""",
            [
                (104, DUPLICATE_STANZA, 10, "duplicate one", 1, None, 0),
                (105, DUPLICATE_STANZA, 20, "duplicate two", 2, None, 0),
                (106, "SYNTHETICMISSING", 30, SPECIAL_TEXT, 1, 2, 0),
                (107, "SYNTHETICAMBIGUOUS", 40, "ambiguous reply", 2, 3, 0),
                (108, "SYNTHETICMALFORMED", 50, "unrecognized metadata", 1, 4, 3),
            ],
        )
        writer.commit()
        # All fixture data is only in the WAL while the writer stays open.
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        writer.execute(
            """INSERT INTO ZWAMESSAGE (Z_PK, ZSTANZAID, ZTEXT, ZCHATSESSION, ZSORT)
            VALUES (109, 'SYNTHETICUNCOMMITTED', 'uncommitted', 17, 40)""",
        )
        yield path


@pytest.fixture
def whatsapp_client(client, whatsapp_db):
    client.app.dependency_overrides[get_whatsapp_database_path] = lambda: whatsapp_db
    return client


@pytest.fixture
def client_with_media(whatsapp_client, whatsapp_db, tmp_path):
    path = tmp_path / "ChatStorage.sqlite"
    with (
        closing(sqlite3.connect(whatsapp_db.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(path)) as target,
    ):
        source.backup(target)
        target.executescript("""
            ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMEDIALOCALPATH TEXT;
            ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZAUTHORNAME TEXT;
            ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZXMPPTHUMBPATH TEXT;
            ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEINFO INTEGER;
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZRECEIPTINFO BLOB);
            """)
    whatsapp_client.app.dependency_overrides[get_whatsapp_database_path] = lambda: path
    return whatsapp_client, path


def _add_media_messages(path, rows):
    with closing(sqlite3.connect(path)) as db:
        db.executemany(
            """INSERT INTO ZWAMEDIAITEM
            (Z_PK, ZMEDIALOCALPATH, ZAUTHORNAME, ZXMPPTHUMBPATH) VALUES (?, ?, ?, ?)""",
            [(row_id, local, title, thumb) for row_id, _, local, title, thumb in rows],
        )
        db.executemany(
            """INSERT INTO ZWAMESSAGE
            (Z_PK, ZSORT, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM, ZCHATSESSION)
            VALUES (?, ?, ?, ?, ?, 17)""",
            [
                (row_id, row_id, kind, "media message", row_id)
                for row_id, kind, *_ in rows
            ],
        )
        db.commit()


def _export_zip(client):
    response = client.post("/api/whatsapp/chats/export", json={"chat_ids": [17]})
    assert response.status_code == 200
    return ZipFile(io.BytesIO(response.content))


def _unpack_chat(archive):
    folder = f"WhatsApp_Chats/{CHAT_NAME}"
    messages = json.loads(archive.read(f"{folder}/messages.json"))["messages"]
    with io.TextIOWrapper(
        archive.open(f"{folder}/messages.csv"), encoding="utf-8-sig", newline=""
    ) as stream:
        rows = {int(row["row_id"]): row for row in csv.DictReader(stream)}
    return folder, {message["row_id"]: message for message in messages}, rows


def _export(client, chat_id, export_format):
    response = client.get(
        f"/api/whatsapp/chats/{chat_id}/export", params={"format": export_format}
    )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"filename*=UTF-8''{CHAT_NAME}.{export_format}" in unquote(disposition)
    assert response.headers["cache-control"] == "no-store"
    return response


def _csv_rows(response):
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    return list(
        csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline=""))
    )


def test_chat_list_only_exposes_selection_metadata(whatsapp_client):
    response = whatsapp_client.get("/api/whatsapp/chats")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert sorted(response.json()["chats"], key=lambda chat: chat["id"]) == [
        {"id": 17, "name": CHAT_NAME, "type": "direct"},
        {"id": 45, "name": CHAT_NAME, "type": "group"},
        {"id": 61, "name": UNSAFE_NAMES[0], "type": "group"},
        {"id": 62, "name": UNSAFE_NAMES[1], "type": "group"},
    ]


def test_direct_json_and_csv_match_stanza_in_committed_wal(whatsapp_client):
    response = _export(whatsapp_client, 17, "json")
    assert response.headers["content-type"] == "application/json"
    exported = response.json()
    assert (exported["chat_id"], exported["chat_name"]) == (17, CHAT_NAME)
    messages = exported["messages"]
    assert [message["text"] for message in messages] == ["hi", "11", "引用"]
    assert messages[0]["message_id"] == FIRST_STANZA
    assert messages[0]["sent_at"] == "2001-01-01T00:02:00+00:00"
    assert messages[0]["sender_id"] == DIRECT_SENDER
    assert messages[0]["message_type"] == 0
    assert messages[0]["from_me"] is False
    assert messages[1]["from_me"] is True
    assert messages[1]["reply_to"] is None
    assert messages[2]["reply_to"] == {
        "message_id": FIRST_STANZA,
        "quoted_author_id": DIRECT_SENDER,
        "quoted_text": "hi",
        "status": "matched",
        "row_id": 102,
        "original_text": "hi",
    }

    rows = _csv_rows(_export(whatsapp_client, 17, "csv"))
    assert [row["text"] for row in rows] == ["hi", "11", "引用"]
    for row in rows[:2]:
        assert row["reply_to_message_id"] == row["quoted_author_id"] == ""
        assert row["quoted_text"] == row["reply_status"] == ""
    assert rows[2]["reply_to_message_id"] == rows[0]["message_id"] == FIRST_STANZA
    assert rows[2]["quoted_author_id"] == DIRECT_SENDER
    assert rows[2]["quoted_text"] == "hi"
    assert rows[2]["reply_status"] == "matched"


def test_group_export_preserves_missing_ambiguous_and_malformed(whatsapp_client):
    exported = _export(whatsapp_client, 45, "json").json()
    assert exported["chat_id"] == 45
    duplicates = exported["messages"][:2]
    assert [message["sender_id"] for message in duplicates] == [
        GROUP_MEMBER,
        OTHER_MEMBER,
    ]
    missing, ambiguous, malformed = exported["messages"][2:]
    assert missing["text"] == SPECIAL_TEXT
    assert missing["reply_to"] == {
        "message_id": FIRST_STANZA,
        "quoted_author_id": OTHER_MEMBER,
        "quoted_text": SPECIAL_TEXT,
        "status": "missing_in_chat",
        "row_id": None,
    }
    assert ambiguous["reply_to"]["message_id"] == DUPLICATE_STANZA
    assert ambiguous["reply_to"]["status"] == "ambiguous"
    assert ambiguous["reply_to"]["row_id"] is None
    assert malformed["text"] == "unrecognized metadata"
    assert malformed["metadata_status"] == "unrecognized_format"
    assert malformed["reply_to"] is None

    rows = _csv_rows(_export(whatsapp_client, 45, "csv"))
    assert len(rows) == 5
    assert rows[2]["text"] == rows[2]["quoted_text"] == SPECIAL_TEXT
    assert rows[2]["reply_to_message_id"] == FIRST_STANZA
    assert rows[2]["reply_status"] == "missing_in_chat"
    assert rows[3]["reply_status"] == "ambiguous"
    assert rows[4]["reply_status"] == ""


def test_export_rejects_unknown_chat_and_format(whatsapp_client):
    assert (
        whatsapp_client.get("/api/whatsapp/chats/999/export?format=json").status_code
        == 404
    )
    assert (
        whatsapp_client.get("/api/whatsapp/chats/17/export?format=xml").status_code
        == 422
    )


def test_zip_exports_both_formats_for_each_selected_chat(whatsapp_client):
    response = whatsapp_client.post(
        "/api/whatsapp/chats/export", json={"chat_ids": [45, 17, 45]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="WhatsApp_Chats.zip"'
    )
    assert response.headers["cache-control"] == "no-store"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        expected = {
            f"WhatsApp_Chats/{CHAT_NAME} ({chat_id})/messages.{export_format}": _export(
                whatsapp_client, chat_id, export_format
            ).content
            for chat_id in (17, 45)
            for export_format in ("json", "csv")
        }
        assert sorted(archive.namelist()) == sorted(expected)
        for path, content in expected.items():
            assert archive.read(path) == content


def test_zip_keeps_chat_folders_distinct_after_sanitizing_names(whatsapp_client):
    response = whatsapp_client.post(
        "/api/whatsapp/chats/export", json={"chat_ids": [61, 62]}
    )
    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == [
            "WhatsApp_Chats/周末🌿_计划 (61)/messages.csv",
            "WhatsApp_Chats/周末🌿_计划 (61)/messages.json",
            "WhatsApp_Chats/周末🌿_计划 (62)/messages.csv",
            "WhatsApp_Chats/周末🌿_计划 (62)/messages.json",
        ]


def test_zip_links_to_original_document_sticker_and_thumbnail(client_with_media):
    client, path = client_with_media
    media = path.parent / "Message/Media"
    media.mkdir(parents=True)
    document = "合成附件内容".encode()
    sticker = b"RIFF\x04\x00\x00\x00WEBP"
    thumbnail = b"\xff\xd8thumbnail"
    (media / "generated.txt").write_bytes(document)
    (media / "sticker.webp").write_bytes(sticker)
    (media / "document.thumb").write_bytes(thumbnail)
    _add_media_messages(
        path,
        [
            (201, 8, "Media/generated.txt", "附件.txt", "Media/document.thumb"),
            (202, 15, "Media/sticker.webp", None, None),
        ],
    )

    with _export_zip(client) as archive:
        folder, messages, rows = _unpack_chat(archive)
        for row_id, label, content in [
            (201, "附件.txt", document),
            (202, "sticker.webp", sticker),
        ]:
            message, row = messages[row_id], rows[row_id]
            assert message["attachment_name"] == row["attachment_name"] == label
            assert (
                message["attachment_status"] == row["attachment_status"] == "exported"
            )
            attachment = message["attachment_path"]
            assert attachment == row["attachment_path"] == f"media/{row_id}-{label}"
            assert archive.read(f"{folder}/{attachment}") == content
        preview = messages[201]["thumbnail_path"]
        assert preview == rows[201]["thumbnail_path"]
        assert preview.endswith(".jpg")
        assert archive.read(f"{folder}/{preview}") == thumbnail


def test_zip_keeps_records_for_missing_and_outside_attachments(client_with_media):
    client, path = client_with_media
    media = path.parent / "Message/Media"
    media.mkdir(parents=True)
    outside = path.parent / "outside.txt"
    outside.write_bytes(b"not part of the chat")
    (media / "symlink.txt").symlink_to(outside)
    _add_media_messages(
        path,
        [
            (201, 8, "Media/missing.txt", "Missing.txt", None),
            (202, 8, "Media/../../outside.txt", "Traversal.txt", None),
            (203, 8, "Media/symlink.txt", "Symlink.txt", None),
        ],
    )

    with _export_zip(client) as archive:
        folder, messages, rows = _unpack_chat(archive)
        assert set(archive.namelist()) == {
            f"{folder}/messages.json",
            f"{folder}/messages.csv",
        }
        for row_id in (201, 202, 203):
            message, row = messages[row_id], rows[row_id]
            assert message["text"] == row["text"] == "media message"
            assert message["attachment_status"] == "missing_local"
            assert row["attachment_status"] == "missing_local"
            assert "attachment_path" not in message
            assert row["attachment_path"] == ""


def test_zip_keeps_own_and_participant_reactions_on_target_message(client_with_media):
    client, path = client_with_media
    millis = 1_700_000_000_000
    participant = (
        _bytes_field(1, b"SYNTHETICOTHER001")
        + _bytes_field(2, DIRECT_SENDER.encode())
        + _bytes_field(3, "👍🏽".encode())
        + _varint(4 << 3)
        + _varint(millis)
    )
    owner = (
        _bytes_field(1, b"SYNTHETICOWNER001")
        + _bytes_field(2, "❤️".encode())
        + _varint(3 << 3)
        + _varint(millis + 123)
    )
    receipt = _bytes_field(7, _bytes_field(1, participant) + _bytes_field(2, owner))
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO ZWAMESSAGEINFO VALUES (1, ?)", (receipt,))
        db.execute("UPDATE ZWAMESSAGE SET ZMESSAGEINFO = 1 WHERE Z_PK = 102")
        db.commit()

    with _export_zip(client) as archive:
        _, messages, rows = _unpack_chat(archive)
        reactions = messages[102]["reactions"]
        assert rows[102]["message_id"] == messages[102]["message_id"] == FIRST_STANZA
        assert reactions == json.loads(rows[102]["reactions"])
        assert reactions == [
            {
                "reaction_id": "SYNTHETICOTHER001",
                "emoji": "👍🏽",
                "sender_id": DIRECT_SENDER,
                "from_me": False,
                "sent_at": "2023-11-14T22:13:20+00:00",
            },
            {
                "reaction_id": "SYNTHETICOWNER001",
                "emoji": "❤️",
                "sender_id": None,
                "from_me": True,
                "sent_at": "2023-11-14T22:13:20.123000+00:00",
            },
        ]
        assert "reactions" not in messages[103]
        assert rows[103]["reactions"] == ""


@pytest.mark.parametrize("chat_ids, status_code", [([], 422), ([17, 999], 404)])
def test_zip_rejects_empty_selection_or_missing_chat(
    whatsapp_client, chat_ids, status_code
):
    response = whatsapp_client.post(
        "/api/whatsapp/chats/export", json={"chat_ids": chat_ids}
    )
    assert response.status_code == status_code
    assert "content-disposition" not in response.headers


def test_unavailable_database_is_not_created(client, tmp_path):
    missing = tmp_path / "ChatStorage.sqlite"
    client.app.dependency_overrides[get_whatsapp_database_path] = lambda: missing
    response = client.get("/api/whatsapp/chats")
    assert response.status_code == 503
    assert not missing.exists()
