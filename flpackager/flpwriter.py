"""Surgical, byte-level rewriting of sample paths inside an .flp file.

Why not just use ``pyflp.save()``?
----------------------------------
``pyflp.save()`` re-serialises *every* event from its parsed representation.
Testing this against 471 real projects showed that is not lossless:

* ``PlaylistEvent`` (id 233) lost 16 bytes on some projects -- PyFLP's
  ``GreedyRange`` silently drops a trailing partial item when the event size
  isn't a multiple of a known struct size. That is dropped playlist data.
* ``TrackEvent`` (id 238) floats came back re-encoded (0x3f814afd -> 0x3f81ae47).
* ``ParametersEvent`` (id 215) differed in its tail bytes.

52 of 471 projects did not round-trip byte-for-byte. For a tool whose entire
promise is "this opens cleanly on your collaborator's machine", that is
unacceptable. So instead we copy the original event stream verbatim and splice
in replacements only for the specific ``SamplePath`` events we intend to
change. Every other byte of the project is preserved exactly.
"""

from __future__ import annotations

import io
from typing import Iterator, Mapping, NamedTuple

# Event ID class boundaries, mirroring pyflp._events.
_WORD = 64
_DWORD = 128
_TEXT = 192

#: ``ChannelID.SamplePath`` == TEXT + 4.
SAMPLE_PATH_ID = 196

#: "FLhd" + size + format + channel count + ppq + "FLdt" + data size.
HEADER_SIZE = 22
_DATA_SIZE_SLICE = slice(18, 22)


class RawEvent(NamedTuple):
    """One event as it physically appears in the file."""

    id: int
    start: int
    end: int


class FLPStructureError(Exception):
    """Raised when the .flp byte structure isn't what we expect."""


def _read_varint(stream: io.BytesIO) -> int:
    """Read a base-128 varint (LEB128), as ``construct.VarInt`` writes them."""
    value = shift = 0
    while True:
        byte = stream.read(1)
        if not byte:
            raise FLPStructureError("Unexpected end of file while reading a varint")
        octet = byte[0]
        value |= (octet & 0x7F) << shift
        if not octet & 0x80:
            return value
        shift += 7
        if shift > 63:
            raise FLPStructureError("Malformed varint")


def _write_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    out = bytearray()
    while True:
        octet = value & 0x7F
        value >>= 7
        if value:
            out.append(octet | 0x80)
        else:
            out.append(octet)
            return bytes(out)


def iter_raw_events(data: bytes) -> Iterator[RawEvent]:
    """Walk the event stream, yielding each event's ID and byte range.

    Mirrors ``pyflp.parse``'s reader so indexes line up 1:1 with
    ``Project.events``.
    """
    if len(data) < HEADER_SIZE:
        raise FLPStructureError("File is too small to be an .flp")
    if data[:4] != b"FLhd":
        raise FLPStructureError("Missing 'FLhd' magic; not an .flp file")
    if data[14:18] != b"FLdt":
        raise FLPStructureError("Missing 'FLdt' data chunk magic")

    stream = io.BytesIO(data)
    stream.seek(HEADER_SIZE)
    end = len(data)

    while stream.tell() < end:
        start = stream.tell()
        id_byte = stream.read(1)
        if not id_byte:
            break
        event_id = id_byte[0]

        if event_id < _WORD:
            stream.seek(1, io.SEEK_CUR)
        elif event_id < _DWORD:
            stream.seek(2, io.SEEK_CUR)
        elif event_id < _TEXT:
            stream.seek(4, io.SEEK_CUR)
        else:
            size = _read_varint(stream)
            stream.seek(size, io.SEEK_CUR)

        if stream.tell() > end:
            raise FLPStructureError(
                f"Event at offset {start} claims to run past the end of the file"
            )
        yield RawEvent(event_id, start, stream.tell())


def encode_path_event(path: str, *, unicode: bool) -> bytes:
    """Build a complete ``SamplePath`` event chunk for ``path``."""
    if unicode:
        payload = (path + "\0").encode("utf-16-le")
    else:
        payload = (path + "\0").encode("ascii", errors="replace")
    return bytes([SAMPLE_PATH_ID]) + _write_varint(len(payload)) + payload


def rewrite_sample_paths(
    data: bytes, replacements: Mapping[int, str], *, unicode: bool
) -> bytes:
    """Return a new .flp image with the given sample paths replaced.

    Args:
        data: The original file bytes. Never mutated.
        replacements: Maps *event index* (position in the event stream, which
            matches ``Project.events`` order) to the new path string.
        unicode: True for FL Studio >= 11.5 (UTF-16-LE strings), else ASCII.

    Raises:
        FLPStructureError: If an index doesn't point at a ``SamplePath`` event,
            which would mean our index mapping is wrong -- we refuse to write a
            file we might be corrupting.
    """
    events = list(iter_raw_events(data))

    for index in replacements:
        if not 0 <= index < len(events):
            raise FLPStructureError(
                f"Event index {index} is out of range (file has {len(events)} events)"
            )
        if events[index].id != SAMPLE_PATH_ID:
            raise FLPStructureError(
                f"Event index {index} is id {events[index].id}, "
                f"expected {SAMPLE_PATH_ID} (SamplePath)"
            )

    body = bytearray()
    for index, event in enumerate(events):
        if index in replacements:
            body.extend(encode_path_event(replacements[index], unicode=unicode))
        else:
            body.extend(data[event.start : event.end])

    out = bytearray(data[:HEADER_SIZE])
    out[_DATA_SIZE_SLICE] = len(body).to_bytes(4, "little")
    out.extend(body)
    return bytes(out)
