"""Decode reactions attached to a WhatsApp message receipt."""

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from .whatsapp_proto import iter_fields

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PARTICIPANT_DOMAINS = {"s.whatsapp.net", "c.us", "lid", "hosted", "hosted.lid"}


class Reaction(TypedDict):
    reaction_id: str
    emoji: str
    sender_id: str | None
    from_me: bool
    sent_at: str | None


def _text(value: int | bytes | None) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else ""


def _timestamp(value: int | bytes | None) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        return (_UNIX_EPOCH + timedelta(milliseconds=value)).isoformat()
    except OverflowError:
        return None


def _reaction(data: bytes, *, from_me: bool) -> Reaction | None:
    try:
        fields = dict(iter_fields(data))
        reaction_id = _text(fields.get(1))
        emoji = _text(fields.get(2 if from_me else 3))
        sender = None if from_me else _text(fields.get(2))
    except ValueError:
        return None
    if not (
        10 <= len(reaction_id) <= 64
        and reaction_id.isascii()
        and reaction_id.isalnum()
        and emoji.strip()
    ):
        return None
    if sender is not None:
        user, _, domain = sender.rpartition("@")
        if not user or domain.lower() not in _PARTICIPANT_DOMAINS:
            return None
    return {
        "reaction_id": reaction_id,
        "emoji": emoji,
        "sender_id": sender,
        "from_me": from_me,
        "sent_at": _timestamp(fields.get(3 if from_me else 4)),
    }


def decode_reactions(receipt: bytes | None) -> list[Reaction]:
    if not isinstance(receipt, bytes):
        return []
    reactions: list[Reaction] = []
    try:
        for number, wrapper in iter_fields(receipt):
            if number != 7 or not isinstance(wrapper, bytes):
                continue
            # The receipt stores other participants in field 1 and the owner in 2.
            for kind, data in iter_fields(wrapper):
                if kind in (1, 2) and isinstance(data, bytes):
                    reaction = _reaction(data, from_me=kind == 2)
                    if reaction is not None:
                        reactions.append(reaction)
    except ValueError:
        # Keep already parsed reactions if WhatsApp recorded an incomplete blob.
        pass
    return reactions
