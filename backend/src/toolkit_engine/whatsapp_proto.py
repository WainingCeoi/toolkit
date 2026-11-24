"""Read the Protobuf wire types used in local WhatsApp metadata."""

from collections.abc import Iterator


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid protobuf varint")


def iter_fields(data: bytes) -> Iterator[tuple[int, bytes | int]]:
    """Yield length-delimited and varint fields in order, including repeats."""
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, kind = key >> 3, key & 7
        if not number:
            raise ValueError("Invalid protobuf tag")
        if kind == 0:
            value, offset = _read_varint(data, offset)
            yield number, value
            continue
        if kind == 2:
            size, offset = _read_varint(data, offset)
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
        else:
            raise ValueError("Unsupported protobuf wire type")
        end = offset + size
        if end > len(data):
            raise ValueError("Truncated protobuf field")
        if kind == 2:
            yield number, data[offset:end]
        offset = end


def bytes_fields(data: bytes, selected: set[int]) -> dict[int, bytes]:
    return {
        number: value
        for number, value in iter_fields(data)
        if number in selected and isinstance(value, bytes)
    }
