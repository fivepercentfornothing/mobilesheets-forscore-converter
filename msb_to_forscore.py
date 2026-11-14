#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert a MobileSheets .msb library backup into forScore scores and archives.

The reader is based on the MobileSheets 3.9.45 backup layout observed in the
project's sample export: a versioned header, an embedded SQLite database, then
PDF records keyed by MobileSheets song ID. It writes one editable .4sc per
score and one .4ss per MobileSheets setlist. Collections become forScore Labels
by default. An optional .4sb archive packages the library, metadata, editable
annotation layers, and setlists. Unsupported annotation types are reported.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import mmap
import math
import plistlib
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msf_to_forscore as core


MSB_MAGIC = bytes.fromhex("499602d7")
SQLITE_MAGIC = b"SQLite format 3\0"
PDF_MAGIC = b"%PDF-"
PDF_RECORD_HEADER_SIZE = 24
PDF_SENTINEL = 3.4028234663852886e38
ARCHIVE_FORMAT_VERSION = 100000
FORSCORE_BACKUP_MAGIC = b"<--4SBV03-->"
FORSCORE_DOCUMENTS_PREFIX = "{%DOCUMENTS_DIR%}/"
FORSCORE_AUX_PREFIX = "{%AUX_DIR%}/"
FORSCORE_BACKUP_METADATA_FIELDS = {
    "title", "composer", "genre", "key", "keywords", "labels", "reference",
    "minutes", "seconds", "bpm", "tempo", "rating", "difficulty",
}


@dataclass
class SongInfo:
    song_id: int
    title: str
    pdf_name: str
    pdf_size: int
    page_count: int
    metadata: dict[str, object]
    memberships: list[str]
    setlists: list[str]
    warnings: list[str]


@dataclass
class DatabaseContents:
    songs: dict[int, SongInfo]
    pdf_bytes: dict[int, bytes]
    setlists: list[tuple[int, str, list[int]]]
    annotations: dict[int, list[core.StrokeRecord]]
    skipped_annotations: dict[int, Counter[int]]
    page_settings_review: list[tuple[int, str, int, int, int, int, int, int]]
    unusual_metadata: list[tuple[str, str, str]]
    database_sha256: str


def embedded_database_bounds(raw: mmap.mmap) -> tuple[int, int]:
    """Return the offset and byte length of the embedded SQLite database."""
    database_start = raw.find(SQLITE_MAGIC)
    first_pdf = raw.find(PDF_MAGIC, max(database_start, 0))
    if database_start < 0 or first_pdf < 0 or database_start > first_pdf:
        raise ValueError("Could not locate the embedded SQLite database and PDF section")
    header = raw[database_start : database_start + 100]
    if len(header) < 100:
        raise ValueError("The embedded SQLite header is incomplete")
    page_size = struct.unpack_from(">H", header, 16)[0]
    if page_size == 1:
        page_size = 65536
    page_count = struct.unpack_from(">I", header, 28)[0]
    if not page_size or not page_count:
        raise ValueError("The embedded SQLite database has an invalid page size or page count")
    database_length = page_size * page_count
    database_end = database_start + database_length
    if database_end > first_pdf or database_end > len(raw):
        raise ValueError("The embedded SQLite database overlaps the PDF records")
    return database_start, database_length


def extract_pdf_records(
    raw: mmap.mmap,
    database_end: int,
    file_rows: list[tuple[int, int, str, int, int]],
) -> dict[int, bytes]:
    """Read [24-byte record header][PDF bytes] entries after the database."""
    expected = {song_id: (file_id, name, size, pages) for file_id, song_id, name, size, pages in file_rows}
    if len(expected) != len(file_rows):
        raise ValueError("This backup contains multiple PDF files for one song; multi-file songs need separate handling")

    result: dict[int, bytes] = {}
    cursor = database_end
    for _ in range(len(file_rows)):
        header = raw[cursor : cursor + PDF_RECORD_HEADER_SIZE]
        if len(header) != PDF_RECORD_HEADER_SIZE:
            raise ValueError("The PDF record header is truncated")
        pdf_start = cursor + PDF_RECORD_HEADER_SIZE
        if raw[pdf_start : pdf_start + len(PDF_MAGIC)] != PDF_MAGIC:
            raise ValueError(f"Expected a PDF at backup offset {pdf_start}; the record layout is not recognized")
        song_id = struct.unpack_from(">I", header, 12)[0]
        if song_id not in expected:
            raise ValueError(f"PDF record references unknown MobileSheets song ID {song_id}")
        _file_id, file_name, file_size, _page_count = expected[song_id]
        if song_id in result:
            raise ValueError(f"The backup contains more than one PDF record for song ID {song_id}")
        pdf_end = pdf_start + file_size
        if pdf_end > len(raw):
            raise ValueError(f"The embedded PDF for {file_name!r} is truncated")
        pdf_data = bytes(raw[pdf_start:pdf_end])
        if not pdf_data.startswith(PDF_MAGIC) or b"%%EOF" not in pdf_data[-128:]:
            raise ValueError(f"The embedded PDF for {file_name!r} does not match its database size")
        result[song_id] = pdf_data
        cursor = pdf_end

    missing = set(expected) - set(result)
    if missing:
        raise ValueError(f"No embedded PDF record found for song IDs: {sorted(missing)}")
    return result


def parse_point_blob(blob: bytes, count: int, annotation_id: int) -> list[list[tuple[float, float]]]:
    """Decode the MobileSheets database's little-endian double point array.

    The first three values are a path flag, coordinate-value count, and width;
    subsequent x/y pairs are float values widened to double precision. The
    float32-max pair separates multiple paths, matching the sampled .msf
    representation.
    """
    if len(blob) % 8:
        raise ValueError(f"Annotation {annotation_id} has a point blob with a partial value")
    values = struct.unpack("<" + "d" * (len(blob) // 8), blob)
    if count != len(values) or len(values) < 3:
        raise ValueError(f"Annotation {annotation_id} has an inconsistent point count")
    coordinate_count = int(round(values[1]))
    if coordinate_count != len(values) - 3 or coordinate_count % 2:
        raise ValueError(f"Annotation {annotation_id} has an unrecognized point-array layout")

    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    coords = values[3:]
    for index in range(0, len(coords), 2):
        x, y = coords[index : index + 2]
        if x == PDF_SENTINEL and y == PDF_SENTINEL:
            if current:
                paths.append(current)
                current = []
            continue
        if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > 1e7 or abs(y) > 1e7:
            raise ValueError(f"Annotation {annotation_id} contains invalid coordinates")
        current.append((x, y))
    if current:
        paths.append(current)
    return paths


def decode_database_annotations(connection: sqlite3.Connection, songs: dict[int, SongInfo]) -> tuple[
    dict[int, list[core.StrokeRecord]], dict[int, Counter[int]]
]:
    annotations: dict[int, list[core.StrokeRecord]] = defaultdict(list)
    skipped: dict[int, Counter[int]] = defaultdict(Counter)
    query = """
        SELECT a.Id, a.SongId, a.Page, a.Type, a.Opacity,
               a.SourcePageWidth, a.SourcePageHeight,
               d.LineColor, d.FillColor, d.LineWidth,
               p.Points, p.Count
        FROM AnnotationsBase AS a
        LEFT JOIN DrawAnnotations AS d ON d.BaseId = a.Id
        LEFT JOIN AnnotationPoints AS p ON p.AnnotationId = a.Id
        ORDER BY a.SongId, a.Id
    """
    for (
        annotation_id,
        song_id,
        page,
        tool_id,
        opacity,
        source_width,
        source_height,
        line_color,
        fill_color,
        line_width,
        point_blob,
        point_count,
    ) in connection.execute(query):
        if song_id not in songs:
            continue
        if tool_id not in core.SUPPORTED_TOOLS:
            skipped[song_id][tool_id] += 1
            continue
        if not isinstance(point_blob, bytes) or point_count is None or line_width is None:
            skipped[song_id][tool_id] += 1
            continue

        paths = parse_point_blob(point_blob, point_count, annotation_id)
        argb = (line_color if line_color not in (None, 0) else fill_color or 0) & 0xFFFFFFFF
        color = ((argb >> 16) & 255, (argb >> 8) & 255, argb & 255)
        tool = core.SUPPORTED_TOOLS[tool_id]
        annotations[song_id].append(
            core.StrokeRecord(
                # SQLite pages are zero-based; the .4sc layer keys are one-based.
                page=int(page) + 1,
                page_width=float(source_width or 0),
                page_height=float(source_height or 0),
                width=float(line_width),
                alpha=int(opacity if opacity is not None else 100),
                color=color,
                tool=tool,
                paths=paths,
            )
        )
    return dict(annotations), dict(skipped)


def normalize_mobile_key(name: str) -> str | None:
    """Expand MobileSheets' shorthand key name to the key parser's form."""
    match = re.fullmatch(r"\s*([A-G])([#b]?)(m?)\s*", name, flags=re.IGNORECASE)
    if not match:
        return name if core.forscore_key_code(name) is not None else None
    root, accidental, minor = match.groups()
    return f"{root.upper()}{accidental}{' minor' if minor else ' major'}"


def unusual_metadata_value(value: str) -> bool:
    lowered = value.casefold()
    flags = ("pdf printer", "convert pdf", "converter", "unspecified", "unknownapplication")
    return any(flag in lowered for flag in flags)


PDF_INFO_FIELDS = ("Title", "Subject", "Keywords", "Author", "Creator", "Producer", "CreationDate", "ModDate")


def parse_pdfinfo_output(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() in PDF_INFO_FIELDS:
            fields[name.strip()] = value.strip()
    return fields


def inspect_pdf_properties(pdf: bytes, name: str, temp_dir: Path) -> dict[str, str]:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        return {"Inspection": "pdfinfo not installed; PDF data preserved unchanged"}
    path = temp_dir / safe_filename(name)
    path.write_bytes(pdf)
    result = subprocess.run([pdfinfo, str(path)], capture_output=True, text=True)
    fields = parse_pdfinfo_output(result.stdout)
    if result.returncode:
        fields["Inspection"] = result.stderr.strip() or f"pdfinfo returned {result.returncode}"
    return fields


def read_database_contents(database_path: Path, raw: mmap.mmap, database_start: int, database_length: int) -> DatabaseContents:
    digest = hashlib.sha256(raw[database_start : database_start + database_length]).hexdigest()
    temporary_db = database_path
    temporary_db.write_bytes(raw[database_start : database_start + database_length])
    connection = sqlite3.connect(f"file:{temporary_db}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError(f"Embedded SQLite integrity check failed: {integrity}")

        composers: dict[int, list[str]] = defaultdict(list)
        for song_id, name in connection.execute(
            "SELECT SongId, Composer.Name FROM ComposerSongs JOIN Composer ON Composer.Id=ComposerSongs.ComposerId ORDER BY ComposerSongs.Id"
        ):
            composers[song_id].append(name)
        genres: dict[int, list[str]] = defaultdict(list)
        for song_id, name in connection.execute(
            "SELECT SongId, Genres.Type FROM GenresSongs JOIN Genres ON Genres.Id=GenresSongs.GenreId ORDER BY GenresSongs.Id"
        ):
            genres[song_id].append(name)
        keys: dict[int, list[str]] = defaultdict(list)
        for song_id, name in connection.execute(
            'SELECT SongId, "Key".Name FROM KeySongs JOIN "Key" ON "Key".Id=KeySongs.KeyId ORDER BY KeySongs.Id'
        ):
            keys[song_id].append(name)
        collections: dict[int, list[str]] = defaultdict(list)
        for song_id, name in connection.execute(
            "SELECT SongId, Collections.Name FROM CollectionSong JOIN Collections ON Collections.Id=CollectionSong.CollectionId ORDER BY CollectionSong.Id"
        ):
            collections[song_id].append(name)
        setlist_names = {setlist_id: name for setlist_id, name in connection.execute("SELECT Id, Name FROM Setlists")}
        setlist_song_ids: dict[int, list[int]] = defaultdict(list)
        song_setlists: dict[int, list[str]] = defaultdict(list)
        for setlist_id, song_id in connection.execute("SELECT SetlistId, SongId FROM SetlistSong ORDER BY Id"):
            setlist_song_ids[setlist_id].append(song_id)
            if setlist_id in setlist_names:
                song_setlists[song_id].append(setlist_names[setlist_id])
        tempos: dict[int, list[int]] = defaultdict(list)
        for song_id, tempo in connection.execute("SELECT SongId, Tempo FROM Tempos ORDER BY SongId, TempoIndex, Id"):
            if tempo:
                tempos[song_id].append(int(tempo))

        file_rows = list(
            connection.execute(
                "SELECT Id, SongId, Path, FileSize, SourceFilePageCount FROM Files ORDER BY Id"
            )
        )
        raw_songs = list(connection.execute(
            "SELECT Id, Title, Difficulty, Duration, Stars, Keywords, Custom, Custom2 FROM Songs ORDER BY Id"
        ))
        songs: dict[int, SongInfo] = {}
        unusual: list[tuple[str, str, str]] = []
        files_by_song: dict[int, tuple[int, str, int, int]] = {}
        for file_id, song_id, pdf_name, pdf_size, page_count in file_rows:
            if song_id in files_by_song:
                raise ValueError(f"Song ID {song_id} has multiple PDF files; conversion currently requires one PDF per score")
            files_by_song[song_id] = (file_id, pdf_name, int(pdf_size), int(page_count or 0))

        for song_id, title, difficulty, duration, stars, keywords, custom, custom2 in raw_songs:
            file_info = files_by_song.get(song_id)
            if not file_info:
                raise ValueError(f"Song ID {song_id} ({title!r}) has no file entry")
            _file_id, pdf_name, pdf_size, page_count = file_info
            metadata: dict[str, object] = {"title": title}
            composer_values = list(dict.fromkeys(composers.get(song_id, [])))
            genre_values = list(dict.fromkeys(genres.get(song_id, [])))
            collection_values = list(dict.fromkeys(collections.get(song_id, [])))
            key_values = list(dict.fromkeys(keys.get(song_id, [])))
            if composer_values:
                metadata["composer"] = ", ".join(composer_values)
            if genre_values:
                metadata["genre"] = ", ".join(genre_values)
            if collection_values:
                metadata["collections"] = collection_values
            if keywords:
                metadata["keywords"] = keywords
            if len(key_values) == 1:
                key = normalize_mobile_key(key_values[0])
                if key:
                    metadata["key_text"] = key
                else:
                    metadata["unmapped_text"] = f"Key: {key_values[0]}"
            elif key_values:
                metadata["unmapped_text"] = "Keys: " + ", ".join(key_values)
            if int(duration or 0) > 0:
                metadata["duration_seconds"] = int(duration)
            if int(stars or 0) > 0:
                metadata["rating"] = int(stars)
            if int(difficulty or 0) > 0:
                metadata["difficulty"] = int(difficulty)
            if tempos.get(song_id):
                metadata["tempo"] = tempos[song_id][0]
            for custom_name, custom_value in (("Custom", custom), ("Custom2", custom2)):
                if custom_value:
                    metadata.setdefault("unmapped_text", "")
                    previous = str(metadata["unmapped_text"])
                    metadata["unmapped_text"] = " | ".join(item for item in (previous, f"{custom_name}: {custom_value}") if item)

            warnings: list[str] = []
            for category, values in (("composer", composer_values), ("genre", genre_values)):
                for value in values:
                    if unusual_metadata_value(value):
                        warnings.append(f"Review {category} value: {value}")
                        unusual.append((category, value, title))
            if len(composer_values) > 1:
                warnings.append("Multiple composer values joined with commas")
            if len(genre_values) > 1:
                warnings.append("Multiple genre values joined with commas")
            if len(collection_values) > 1:
                warnings.append("Multiple collections retained in the selected forScore field")
            songs[song_id] = SongInfo(
                song_id=song_id,
                title=title,
                pdf_name=Path(pdf_name).name,
                pdf_size=pdf_size,
                page_count=page_count,
                metadata=metadata,
                memberships=collection_values,
                setlists=list(dict.fromkeys(song_setlists.get(song_id, []))),
                warnings=warnings,
            )

        setlists = [
            (setlist_id, setlist_names[setlist_id], song_ids)
            for setlist_id, song_ids in sorted(setlist_song_ids.items())
            if setlist_id in setlist_names
        ]
        pdf_bytes = extract_pdf_records(raw, database_start + database_length, file_rows)
        annotations, skipped = decode_database_annotations(connection, songs)
        page_settings_review = []
        for song_id, page, left, top, right, bottom, rotation in connection.execute(
            "SELECT SongId, Page, Left, Top, Right, Bottom, Rotation FROM Crop "
            "WHERE Left!=0 OR Top!=0 OR Right!=0 OR Bottom!=0 OR Rotation!=0 ORDER BY SongId, Page"
        ):
            if song_id in songs:
                page_settings_review.append((
                    song_id,
                    songs[song_id].title,
                    int(page) + 1,
                    int(left),
                    int(top),
                    int(right),
                    int(bottom),
                    int(rotation),
                ))
        return DatabaseContents(songs, pdf_bytes, setlists, annotations, skipped, page_settings_review, unusual, digest)
    finally:
        connection.close()


def canvas_size(reference_path: Path) -> tuple[int, int]:
    reference = core.read_gzip_plist(reference_path)
    for key, value in reference.items():
        if isinstance(key, str) and key.endswith(".png") and isinstance(value, bytes):
            return core.png_dimensions(value)
    raise ValueError("The supplied forScore reference has no annotated PNG layer")


def render_package(
    info: SongInfo,
    pdf: bytes,
    records: list[core.StrokeRecord],
    canvas: tuple[int, int],
    output_path: Path,
    smoothing: float,
    pen_width_scale: float | None,
    highlighter_width_scale: float | None,
    collection_field: str,
) -> tuple[dict[str, Any], int]:
    base_name = info.pdf_name
    output: dict[str, Any] = {"pdfData": pdf, f"{base_name}|title": info.title}
    metadata = dict(info.metadata)
    metadata["collection_field"] = collection_field
    core.add_forscore_metadata(output, base_name, metadata)
    if records:
        page_boxes = core.page_boxes(pdf, info.page_count, records)
        width_multipliers = {"pen": pen_width_scale, "highlighter": highlighter_width_scale}
        magick = shutil.which("magick")
        if not magick:
            raise RuntimeError("ImageMagick 7 ('magick') is required to render editable forScore annotation layers")
        with tempfile.TemporaryDirectory(prefix="msb-to-forscore-") as temp_dir:
            temp = Path(temp_dir)
            for page in sorted({record.page for record in records}):
                if page < 1 or page > len(page_boxes):
                    raise ValueError(f"{info.title}: annotation points to page {page}, PDF has {len(page_boxes)} pages")
                page_records = [record for record in records if record.page == page]
                svg_path = temp / f"page-{page}.svg"
                png_path = temp / f"page-{page}.png"
                svg_path.write_text(
                    core.svg_for_page(canvas[0], canvas[1], page_boxes[page - 1], page_records, width_multipliers, smoothing),
                    encoding="utf-8",
                )
                subprocess.run(
                    [magick, "-background", "none", str(svg_path), f"PNG32:{png_path}"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                output[f"{base_name}|{page}.png"] = png_path.read_bytes()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(gzip.compress(plistlib.dumps(output, fmt=plistlib.FMT_BINARY, sort_keys=False), compresslevel=9, mtime=0))
    return output, sum(1 for key in output if isinstance(key, str) and key.endswith(".png"))


def keyed_dictionary(objects: list[Any], pairs: list[tuple[str, Any]], class_ref: plistlib.UID) -> plistlib.UID:
    key_refs = []
    value_refs = []
    for key, value in pairs:
        objects.append(key)
        key_refs.append(plistlib.UID(len(objects) - 1))
        objects.append(value)
        value_refs.append(plistlib.UID(len(objects) - 1))
    objects.append({"NS.keys": key_refs, "NS.objects": value_refs, "$class": class_ref})
    return plistlib.UID(len(objects) - 1)


def make_4ss_archive(
    list_items: list[tuple[str, str, str]],
    packages: list[dict[str, Any]],
) -> bytes:
    """Write the NSKeyedArchiver layout observed in a forScore 15 .4SS."""
    objects: list[Any] = ["$null"]

    def add(value: Any) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    mutable_array_class = add({"$classname": "NSMutableArray", "$classes": ["NSMutableArray", "NSArray", "NSObject"]})
    array_class = add({"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]})
    mutable_dictionary_class = add({"$classname": "NSMutableDictionary", "$classes": ["NSMutableDictionary", "NSDictionary", "NSObject"]})
    dictionary_class = add({"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]})
    setlist_ref = add({"NS.objects": [], "$class": mutable_array_class})
    files_ref = add({"NS.objects": [], "$class": array_class})

    setlist_items = []
    for title, identifier, file_path in list_items:
        setlist_items.append(keyed_dictionary(
            objects,
            [("Title", title), ("Identifier", identifier), ("FilePath", file_path)],
            mutable_dictionary_class,
        ))
    file_items = [keyed_dictionary(objects, list(package.items()), dictionary_class) for package in packages]
    objects[setlist_ref.data]["NS.objects"] = setlist_items
    objects[files_ref.data]["NS.objects"] = file_items
    archive = {
        "$version": ARCHIVE_FORMAT_VERSION,
        "$archiver": "NSKeyedArchiver",
        "$top": {"setlist": setlist_ref, "files": files_ref},
        "$objects": objects,
    }
    return gzip.compress(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY, sort_keys=False), compresslevel=9, mtime=0)


def make_4sb_archive(
    archive_name: str,
    metadata: dict[str, Any],
    assets: list[tuple[str, bytes]],
) -> bytes:
    """Write the .4SBV03 framing observed in a forScore 15.2.6 archive."""
    name_bytes = archive_name.encode("utf-8")
    if not name_bytes or Path(archive_name).name != archive_name:
        raise ValueError("The .4SB archive name must be a non-empty filename")
    index_data = gzip.compress(
        plistlib.dumps(metadata, fmt=plistlib.FMT_BINARY, sort_keys=False),
        compresslevel=9,
        mtime=0,
    )
    output = bytearray(FORSCORE_BACKUP_MAGIC)
    output.extend(f"{len(name_bytes):16d}{len(index_data):16d}".encode("ascii"))
    output.extend(name_bytes)
    output.extend(index_data)

    seen_paths: set[str] = set()
    for asset_path, asset_data in assets:
        if asset_path in seen_paths:
            raise ValueError(f"Duplicate forScore archive asset path {asset_path!r}")
        seen_paths.add(asset_path)
        path_bytes = asset_path.encode("utf-8")
        compressed_data = gzip.compress(asset_data, compresslevel=9, mtime=0)
        output.extend(f"{len(path_bytes):16d}{len(compressed_data):16d}".encode("ascii"))
        output.extend(path_bytes)
        output.extend(compressed_data)
    return bytes(output)


def parse_4sb_archive(raw: bytes) -> tuple[str, dict[str, Any], list[tuple[str, bytes]]]:
    """Read and validate the sampled forScore backup framing."""
    if not raw.startswith(FORSCORE_BACKUP_MAGIC):
        raise ValueError("The file does not begin with a recognized .4SBV03 header")
    cursor = len(FORSCORE_BACKUP_MAGIC)

    def read_sizes() -> tuple[int, int]:
        nonlocal cursor
        header = raw[cursor : cursor + 32]
        if len(header) != 32:
            raise ValueError("The .4SB archive has a truncated size header")
        try:
            first = int(header[:16])
            second = int(header[16:])
        except ValueError as exc:
            raise ValueError("The .4SB archive contains an invalid size header") from exc
        cursor += 32
        if first < 0 or second < 0:
            raise ValueError("The .4SB archive contains a negative size")
        return first, second

    name_length, index_length = read_sizes()
    name_end = cursor + name_length
    if name_end > len(raw):
        raise ValueError("The .4SB archive name is truncated")
    try:
        archive_name = raw[cursor:name_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The .4SB archive name is not valid UTF-8") from exc
    cursor = name_end
    index_end = cursor + index_length
    if index_end > len(raw):
        raise ValueError("The .4SB archive index is truncated")
    try:
        metadata = plistlib.loads(gzip.decompress(raw[cursor:index_end]))
    except Exception as exc:
        raise ValueError("The .4SB archive index is not a readable compressed property list") from exc
    if not isinstance(metadata, dict):
        raise ValueError("The .4SB archive index is not a dictionary")
    cursor = index_end

    assets: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    while cursor < len(raw):
        path_length, compressed_length = read_sizes()
        path_end = cursor + path_length
        compressed_end = path_end + compressed_length
        if not path_length or compressed_end > len(raw):
            raise ValueError("A .4SB archive asset record is truncated")
        try:
            asset_path = raw[cursor:path_end].decode("utf-8")
            asset_data = gzip.decompress(raw[path_end:compressed_end])
        except Exception as exc:
            raise ValueError("A .4SB archive asset has an invalid path or gzip stream") from exc
        if asset_path in seen_paths:
            raise ValueError(f"The .4SB archive repeats asset path {asset_path!r}")
        seen_paths.add(asset_path)
        assets.append((asset_path, asset_data))
        cursor = compressed_end
    return archive_name, metadata, assets


def build_4sb_contents(
    data: DatabaseContents,
    packages: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """Build a forScore archive index and assets from converted score packages."""
    metadata: dict[str, Any] = {
        "&SYS;ARCurrentVersion": "2",
        "&SYS;setlists": [],
        "&SYS;setlistFolders": [],
        "&SYS;setlistLibraries": {},
    }
    pdf_assets: list[tuple[str, bytes]] = []
    annotation_assets: list[tuple[str, bytes]] = []
    seen_pdf_names: set[str] = set()
    for song_id in sorted(data.songs):
        info = data.songs[song_id]
        package = packages.get(song_id)
        if not isinstance(package, dict):
            raise ValueError(f"No converted forScore package was supplied for {info.title!r}")
        pdf = package.get("pdfData")
        if not isinstance(pdf, bytes) or not pdf.startswith(PDF_MAGIC) or len(pdf) != info.pdf_size:
            raise ValueError(f"The converted package for {info.title!r} has an invalid PDF")
        original_pdf = data.pdf_bytes.get(song_id)
        if original_pdf is not None and pdf != original_pdf:
            raise ValueError(f"The converted package for {info.title!r} does not contain its original PDF")
        folded_name = info.pdf_name.casefold()
        if folded_name in seen_pdf_names:
            raise ValueError(f"The backup contains duplicate PDF filenames: {info.pdf_name!r}")
        seen_pdf_names.add(folded_name)
        pdf_assets.append((FORSCORE_DOCUMENTS_PREFIX + info.pdf_name, pdf))

        prefix = info.pdf_name + "|"
        page_layers: list[tuple[int, str, bytes]] = []
        for key, value in package.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            suffix = key[len(prefix) :]
            if suffix in FORSCORE_BACKUP_METADATA_FIELDS:
                metadata[key] = value
                continue
            layer_match = re.fullmatch(r"(\d+)\.png", suffix)
            if layer_match and isinstance(value, bytes) and value.startswith(b"\x89PNG\r\n\x1a\n"):
                page_layers.append((int(layer_match.group(1)), key, value))
        for _page, key, image_data in sorted(page_layers):
            annotation_assets.append((FORSCORE_AUX_PREFIX + key, image_data))

    setlist_keys: list[str] = []
    for setlist_id, name, song_ids in data.setlists:
        key = "&SET;" + name
        if key in metadata:
            raise ValueError(f"Duplicate forScore setlist name {name!r}")
        setlist_keys.append(key)
        items = []
        for song_id in song_ids:
            info = data.songs.get(song_id)
            if info is None:
                raise ValueError(f"Setlist {name!r} references missing song ID {song_id}")
            identifier = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"mobilesheets:{data.database_sha256}:{song_id}")
            ).upper()
            items.append({"Title": info.title, "Identifier": identifier, "FilePath": info.pdf_name})
        metadata[key] = items
    metadata["&SYS;setlists"] = setlist_keys
    return metadata, pdf_assets + annotation_assets


def write_4sb_archive(
    data: DatabaseContents,
    score_paths: dict[int, Path],
    output_path: Path,
) -> dict[str, int]:
    """Create a whole-library .4SB archive from already converted .4SC files."""
    packages = {song_id: core.read_gzip_plist(path) for song_id, path in score_paths.items()}
    metadata, assets = build_4sb_contents(data, packages)
    archive_data = make_4sb_archive(output_path.name, metadata, assets)
    archive_name, parsed_metadata, parsed_assets = parse_4sb_archive(archive_data)
    if archive_name != output_path.name or parsed_metadata != metadata or parsed_assets != assets:
        raise ValueError("The generated .4SB archive did not round-trip exactly")

    expected_setlists = ["&SET;" + name for _setlist_id, name, _song_ids in data.setlists]
    if parsed_metadata.get("&SYS;setlists") != expected_setlists:
        raise ValueError("The generated .4SB archive does not preserve MobileSheets setlist order")
    expected_asset_data = {path: content for path, content in assets}
    if len(expected_asset_data) != len(assets):
        raise ValueError("The generated .4SB archive contains duplicate assets")
    pdf_count = sum(path.startswith(FORSCORE_DOCUMENTS_PREFIX) for path in expected_asset_data)
    layer_count = sum(path.startswith(FORSCORE_AUX_PREFIX) for path in expected_asset_data)
    if pdf_count != len(data.songs):
        raise ValueError("The generated .4SB archive does not contain one PDF for every score")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(archive_data)
    return {"scores": pdf_count, "annotation_layers": layer_count, "setlists": len(data.setlists), "bytes": len(archive_data)}


def resolve_archive_array(archive: dict[str, Any], key: str) -> list[Any]:
    objects = archive["$objects"]
    ref = archive["$top"][key]
    value = objects[ref.data]
    if not isinstance(value, dict) or not isinstance(value.get("NS.objects"), list):
        raise ValueError(f"Generated .4SS archive has an invalid {key} array")
    return [objects[entry.data] for entry in value["NS.objects"]]


def resolve_archive_dictionary(archive: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    objects = archive["$objects"]
    keys = node.get("NS.keys")
    values = node.get("NS.objects")
    if not isinstance(keys, list) or not isinstance(values, list) or len(keys) != len(values):
        raise ValueError("Generated .4SS archive contains a malformed dictionary")
    resolved: dict[str, Any] = {}
    for key_ref, value_ref in zip(keys, values):
        if not isinstance(key_ref, plistlib.UID) or not isinstance(value_ref, plistlib.UID):
            raise ValueError("Generated .4SS archive dictionary entries must use object references")
        key, value = objects[key_ref.data], objects[value_ref.data]
        if not isinstance(key, str):
            raise ValueError("Generated .4SS archive contains a non-text dictionary key")
        resolved[key] = value
    return resolved


def verify_4sc(path: Path, expected_pdf_size: int) -> dict[str, Any]:
    package = core.read_gzip_plist(path)
    pdf = package.get("pdfData")
    if not isinstance(pdf, bytes) or len(pdf) != expected_pdf_size or not pdf.startswith(PDF_MAGIC):
        raise ValueError(f"Generated {path.name} has an invalid embedded PDF")
    for key, value in package.items():
        if isinstance(key, str) and key.endswith(".png") and isinstance(value, bytes):
            core.png_dimensions(value)
    return package


def make_output_readme(
    path: Path,
    counts: dict[str, int],
    canvas: tuple[int, int],
    collection_field: str = "labels",
) -> None:
    collection_name = "Tags" if collection_field == "keywords" else "Labels"
    alternate_field, alternate_name = ("labels", "Labels") if collection_field == "keywords" else ("keywords", "Tags")
    path.write_text(
        "MobileSheets → forScore conversion\n\n"
        f"Scores: {counts['scores']}\nSetlists: {counts['setlists']}\n"
        f"MobileSheets collections represented as forScore {collection_name}: {counts['collections']}\n"
        f"Converted pen/highlighter annotation records: {counts['supported_annotations']}\n"
        f"Unsupported annotation records: {counts['unsupported_annotations']}\n"
        f"Non-default MobileSheets crop settings not applied: {counts['cropped_pages']} pages across {counts['cropped_scores']} scores\n"
        f"PDF property rows inspected: {counts['pdf_properties_inspected']}\n"
        f"Annotation layer size: {canvas[0]} × {canvas[1]} pixels\n\n"
        "Import the .4SC files from scores/ to add individual scores. Each .4SS setlist package includes the scores it references. Importing both every .4SC and every .4SS may add duplicates depending on forScore's duplicate handling. For a first import, try one .4SS setlist and inspect the result before importing the remaining files.\n\n"
        f"Collection memberships are written to forScore {collection_name} by default; pass --collection-field {alternate_field} to use {alternate_name}. Unusual composer/genre strings are preserved and listed in metadata-review.csv. embedded-pdf-properties.csv lists the original PDF properties, which remain embedded in the unchanged source PDFs. The manifest records unsupported MobileSheets annotation types. MobileSheets page crop settings are not applied; page-settings-review.csv lists the non-default source pages. Annotations on cropped pages may not align as they did in MobileSheets, so verify those pages after import. Bookmarks and per-score playback/metronome settings are not converted in this pass.\n",
        encoding="utf-8",
    )


def convert_database(
    source: Path,
    output_dir: Path,
    reference_4sc: Path,
    smoothing: float = core.DEFAULT_SMOOTHING,
    pen_width_scale: float | None = None,
    highlighter_width_scale: float | None = None,
    collection_field: str = "labels",
    zip_path: Path | None = None,
    archive_4sb_path: Path | None = None,
) -> dict[str, int]:
    if not math.isfinite(smoothing) or not 0 <= smoothing <= 1:
        raise ValueError("Smoothing must be a number between 0 and 1")
    if pen_width_scale is not None and (not math.isfinite(pen_width_scale) or pen_width_scale < 0):
        raise ValueError("Pen width scale must be finite and non-negative")
    if highlighter_width_scale is not None and (not math.isfinite(highlighter_width_scale) or highlighter_width_scale < 0):
        raise ValueError("Highlighter width scale must be finite and non-negative")
    if reference_4sc is None:
        raise ValueError("Library conversion requires an annotated forScore .4sc canvas reference")
    if collection_field not in {"keywords", "labels"}:
        raise ValueError("Collection field must be 'keywords' (forScore Tags) or 'labels'")
    if archive_4sb_path and archive_4sb_path.resolve() == source.resolve():
        raise ValueError("The generated .4SB archive path cannot overwrite the MobileSheets backup")
    output_dir.mkdir(parents=True, exist_ok=True)
    scores_dir = output_dir / "scores"
    setlists_dir = output_dir / "setlists"
    scores_dir.mkdir(exist_ok=True)
    setlists_dir.mkdir(exist_ok=True)
    canvas = canvas_size(reference_4sc)

    with source.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if raw[:4] != MSB_MAGIC:
            raise ValueError("This file does not have the MobileSheets backup signature")
        version = struct.unpack_from(">I", raw, 4)[0]
        if version != 1:
            raise ValueError(f"MobileSheets backup version {version} is not supported; this reader was calibrated on version 1")
        database_start, database_length = embedded_database_bounds(raw)
        with tempfile.TemporaryDirectory(prefix="msb-to-forscore-db-") as temp_dir:
            database_path = Path(temp_dir) / "library.sqlite"
            data = read_database_contents(database_path, raw, database_start, database_length)

    if len(data.songs) != len(data.pdf_bytes):
        raise ValueError(f"The backup contains {len(data.songs)} songs but {len(data.pdf_bytes)} embedded PDFs")

    output_paths: dict[int, Path] = {}
    identifiers: dict[int, str] = {}
    manifest_rows: list[dict[str, str | int]] = []
    pdf_property_rows: list[dict[str, str | int]] = []
    total_supported = 0
    total_unsupported = 0
    pdfinfo_available = bool(shutil.which("pdfinfo"))
    with tempfile.TemporaryDirectory(prefix="msb-pdf-properties-") as pdfinfo_temp:
        pdfinfo_dir = Path(pdfinfo_temp)
        for index, song_id in enumerate(sorted(data.songs), start=1):
            info = data.songs[song_id]
            properties = inspect_pdf_properties(data.pdf_bytes[song_id], f"{song_id:03d}.pdf", pdfinfo_dir)
            pdf_property_rows.append({
                "MobileSheets song ID": song_id,
                "Score title": info.title,
                "Source PDF": info.pdf_name,
                **{field: properties.get(field, "") for field in PDF_INFO_FIELDS},
                "Inspection": properties.get("Inspection", ""),
            })

    for index, song_id in enumerate(sorted(data.songs), start=1):
        info = data.songs[song_id]
        records = data.annotations.get(song_id, [])
        skipped = data.skipped_annotations.get(song_id, Counter())
        output_path = scores_dir / f"{song_id:03d} - {safe_filename(info.title)}.4sc"
        package, layer_count = render_package(
            info,
            data.pdf_bytes[song_id],
            records,
            canvas,
            output_path,
            smoothing,
            pen_width_scale,
            highlighter_width_scale,
            collection_field,
        )
        verify_4sc(output_path, info.pdf_size)
        output_paths[song_id] = output_path
        identifiers[song_id] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mobilesheets:{data.database_sha256}:{song_id}")).upper()
        tool_counts = Counter(record.tool for record in records)
        total_supported += len(records)
        total_unsupported += sum(skipped.values())
        skip_text = ", ".join(f"type {key}: {value}" for key, value in sorted(skipped.items()))
        warning_text = "; ".join(info.warnings + ([f"Skipped {skip_text}"] if skip_text else []))
        manifest_rows.append({
            "MobileSheets song ID": song_id,
            "Title": info.title,
            "Source PDF": info.pdf_name,
            "forScore file": str(output_path.relative_to(output_dir)),
        "MobileSheets collections": ", ".join(info.memberships),
        "forScore collection field": collection_field if info.memberships else "",
            "Composer": str(info.metadata.get("composer", "")),
            "Genre": str(info.metadata.get("genre", "")),
            "Setlists": ", ".join(info.setlists),
            "Pen annotation records": tool_counts["pen"],
            "Highlighter annotation records": tool_counts["highlighter"],
            "Unsupported annotations": skip_text,
            "Annotation PNG pages": layer_count,
            "PDF bytes": info.pdf_size,
            "Warnings": warning_text,
        })
        print(f"[{index}/{len(data.songs)}] {info.title} — {len(records)} annotation records, {layer_count} page layer(s)")

    for setlist_id, setlist_name, song_ids in data.setlists:
        list_items = []
        packages = []
        for song_id in song_ids:
            if song_id not in output_paths:
                raise ValueError(f"Setlist {setlist_name!r} refers to missing song ID {song_id}")
            info = data.songs[song_id]
            list_items.append((info.title, identifiers[song_id], info.pdf_name))
            packages.append(core.read_gzip_plist(output_paths[song_id]))
        archive_data = make_4ss_archive(list_items, packages)
        safe_title = safe_filename(setlist_name)
        output_path = setlists_dir / f"{setlist_id:02d} - {safe_title}.4ss"
        output_path.write_bytes(archive_data)
        decoded = plistlib.loads(gzip.decompress(archive_data))
        resolved_items = [resolve_archive_dictionary(decoded, node) for node in resolve_archive_array(decoded, "setlist")]
        resolved_files = [resolve_archive_dictionary(decoded, node) for node in resolve_archive_array(decoded, "files")]
        if len(resolved_items) != len(song_ids):
            raise ValueError(f"Generated .4SS setlist count does not match {setlist_name!r}")
        if len(resolved_files) != len(song_ids):
            raise ValueError(f"Generated .4SS score-file count does not match {setlist_name!r}")
        for list_item, package, source_item in zip(resolved_items, resolved_files, list_items):
            if list_item.get("Title") != source_item[0] or list_item.get("FilePath") != source_item[2]:
                raise ValueError(f"Generated .4SS order/metadata does not match {setlist_name!r}")
            if not isinstance(package.get("pdfData"), bytes) or not any(
                isinstance(key, str) and key.endswith("|title") for key in package
            ):
                raise ValueError(f"Generated .4SS has an incomplete score package in {setlist_name!r}")
        print(f"Setlist: {setlist_name} — {len(song_ids)} scores")

    manifest_path = output_dir / "conversion-manifest.csv"
    if manifest_rows:
        with manifest_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0]))
            writer.writeheader()
            writer.writerows(manifest_rows)

    review_path = output_dir / "metadata-review.csv"
    with review_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(("Source field", "Value preserved", "Score title"))
        writer.writerows(data.unusual_metadata)

    page_review_path = output_dir / "page-settings-review.csv"
    with page_review_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(("MobileSheets song ID", "Score title", "Page", "Crop left", "Crop top", "Crop right", "Crop bottom", "Crop rotation", "Converted pen/highlighter annotations on page"))
        for song_id, title, page, left, top, right, bottom, rotation in data.page_settings_review:
            has_paths = any(record.page == page for record in data.annotations.get(song_id, []))
            writer.writerow((song_id, title, page, left, top, right, bottom, rotation, "yes" if has_paths else "no"))

    pdf_review_path = output_dir / "embedded-pdf-properties.csv"
    with pdf_review_path.open("w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["MobileSheets song ID", "Score title", "Source PDF", *PDF_INFO_FIELDS, "Inspection"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(pdf_property_rows)

    unsupported_total = sum(sum(counter.values()) for counter in data.skipped_annotations.values())
    counts = {
        "scores": len(data.songs),
        "setlists": len(data.setlists),
        "collections": len({label for info in data.songs.values() for label in info.memberships}),
        "supported_annotations": total_supported,
        "unsupported_annotations": unsupported_total,
        "cropped_pages": len(data.page_settings_review),
        "cropped_scores": len({row[0] for row in data.page_settings_review}),
        "pdf_properties_inspected": len(pdf_property_rows) if pdfinfo_available else 0,
    }
    make_output_readme(output_dir / "README.txt", counts, canvas, collection_field)
    if archive_4sb_path:
        archive_counts = write_4sb_archive(data, output_paths, archive_4sb_path)
        print(
            f"forScore archive: {archive_4sb_path} — {archive_counts['scores']} scores, "
            f"{archive_counts['setlists']} setlists, {archive_counts['annotation_layers']} annotation layers, "
            f"{archive_counts['bytes']:,} bytes"
        )
    if zip_path:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(output_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(output_dir))
    print(
        f"\nFinished: {counts['scores']} scores, {counts['setlists']} setlists, "
        f"{counts['collections']} collections, {counts['supported_annotations']} supported annotation records, "
        f"{counts['unsupported_annotations']} unsupported annotation records."
    )
    if zip_path:
        print(f"Bundle: {zip_path} ({zip_path.stat().st_size:,} bytes)")
    return counts


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip().rstrip(".")
    return cleaned or "untitled"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="MobileSheets .msb database export")
    parser.add_argument("output_dir", type=Path, help="Directory for generated .4SC and .4SS files")
    parser.add_argument("--reference-4sc", type=Path, required=True, help="Annotated forScore .4sc used to read annotation-layer dimensions")
    parser.add_argument("--collection-field", choices=("labels", "keywords"), default="labels", help="forScore destination for MobileSheets collections: Labels by default, or Tags via keywords")
    parser.add_argument("--archive-4sb", type=Path, help="Also build a full forScore backup archive from the converted library")
    parser.add_argument("--smoothing", type=float, default=core.DEFAULT_SMOOTHING, help="Pen smoothing from 0 (off) to 1 (strong)")
    parser.add_argument("--pen-width-scale", type=float, default=None, help="Override the empirical pen width curve")
    parser.add_argument("--highlighter-width-scale", type=float, default=None, help="Override the empirical highlighter width curve")
    parser.add_argument("--zip", dest="zip_path", type=Path, help="Optional ZIP file containing the generated scores and setlists")
    args = parser.parse_args()
    convert_database(
        args.backup,
        args.output_dir,
        reference_4sc=args.reference_4sc,
        smoothing=args.smoothing,
        pen_width_scale=args.pen_width_scale,
        highlighter_width_scale=args.highlighter_width_scale,
        collection_field=args.collection_field,
        zip_path=args.zip_path,
        archive_4sb_path=args.archive_4sb,
    )


if __name__ == "__main__":
    main()
