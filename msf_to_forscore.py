#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental MobileSheets .msf to forScore .4sc converter.

The current parser reads score metadata and supports MobileSheets pen and
highlighter paths found in the supplied 3.9.45 samples. It writes metadata and
paths into forScore .4sc packages with transparent per-page annotation layers.
"""

from __future__ import annotations

import argparse
import gzip
import math
import plistlib
import re
import shutil
import struct
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


ANNOTATION_START = b"\xff\xff\x01\x26"
ANNOTATION_END = b"\xff\xff\x01\x35"
PAGE_END = b"\xff\xff\x01\x39"
TITLE_MARKER = b"\xff\xff\x01\x20"
METADATA_END = b"\xff\xff\x01\x21"
MAX_FLOAT_BITS = 0x7F7FFFFF
SUPPORTED_TOOLS = {1: "pen", 2: "highlighter"}
UNSUPPORTED_TOOL_NAMES = {3: "stamp"}
DEFAULT_SMOOTHING = 0.75
# The selected MobileSheets brush width is stored at record offset +35. Width
# 1 keeps the measured minimum that matched the forScore reference. At widths
# 25 and above, use the MobileSheets page-space width directly; transparent
# forScore PNG layers can represent strokes wider than its UI slider maximum.
HIGHLIGHTER_WIDTH_CALIBRATION = (
    (0.3115, 1.2566),
    (7.7866, 7.7866),
    (15.5733, 15.5733),
    (31.1465, 31.1465),
    (46.7198, 46.7198),
    (62.2930, 62.2930),
)
PEN_WIDTH_CALIBRATION = (
    (0.3115, 1.2566),
    (1.5573, 1.8848),
    (3.1147, 2.7225),
)


@dataclass
class StrokeRecord:
    page: int
    page_width: float
    page_height: float
    width: float
    alpha: int
    color: tuple[int, int, int]
    tool: str
    paths: list[list[tuple[float, float]]]


@dataclass
class ParsedSample:
    pdf: bytes
    title: str
    metadata: dict[str, object]
    records: list[StrokeRecord]
    page_count: int
    skipped: Counter[int]


def read_gzip_plist(path: Path) -> dict:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    value = plistlib.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a plist dictionary")
    return value


def png_dimensions(raw: bytes) -> tuple[int, int]:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        raise ValueError("The forScore reference does not contain a PNG page layer")
    width, height = struct.unpack_from(">II", raw, 16)
    if width == 0 or height == 0:
        raise ValueError("The forScore reference has invalid annotation-layer dimensions")
    return width, height


def parse_title(header: bytes, fallback: str) -> str:
    start = header.find(TITLE_MARKER)
    if start < 0 or start + 6 > len(header):
        return fallback
    size = struct.unpack_from(">H", header, start + 4)[0]
    title_bytes = header[start + 6 : start + 6 + size]
    try:
        return title_bytes.decode("utf-8") or fallback
    except UnicodeDecodeError:
        return fallback


def parse_msf_metadata(
    header: bytes,
    fallback_title: str,
    collection_hint: str | None = None,
) -> dict[str, str | int]:
    """Read the metadata fields observed in MobileSheets 3.9.45 exports.

    The sample's text values are stored as type-1, length-prefixed UTF-8
    entries before the metadata-end marker. Fully populated entries follow
    the observed genre, composer, key, empty slot, collection order. Sparse
    records omit empty fields, so their remaining values are matched using
    recognizable key strings and an optional known collection label. Four
    aligned numeric slots after a recognized key hold duration, rating,
    difficulty, and tempo. This layout remains version-bound.
    """
    title = parse_title(header, fallback_title)
    metadata: dict[str, object] = {"title": title}
    title_start = header.find(TITLE_MARKER)
    if title_start < 0 or title_start + 6 > len(header):
        return metadata

    title_length = struct.unpack_from(">H", header, title_start + 4)[0]
    search_start = title_start + 6 + title_length
    metadata_end = header.find(METADATA_END, search_start)
    if metadata_end < 0:
        metadata_end = header.find(ANNOTATION_START, search_start)
    if metadata_end < 0:
        return metadata

    entries: list[tuple[int, str, int]] = []
    cursor = search_start
    while cursor + 4 <= metadata_end:
        if header[cursor : cursor + 2] == b"\x00\x01":
            byte_count = struct.unpack_from(">H", header, cursor + 2)[0]
            value_start = cursor + 4
            value_end = value_start + byte_count
            if byte_count <= 128 and value_end <= metadata_end:
                try:
                    value = header[value_start:value_end].decode("utf-8")
                except UnicodeDecodeError:
                    value = ""
                if all(character.isprintable() for character in value):
                    entries.append((cursor, value, value_end))
                    cursor = value_end
                    continue
        cursor += 1

    nonempty_entries = [entry for entry in entries if entry[1]]
    key_entry = next(
        (entry for entry in nonempty_entries if forscore_key_code(entry[1]) is not None),
        None,
    )
    collection_entry = None
    if collection_hint:
        collection_entry = next(
            (entry for entry in reversed(nonempty_entries) if entry[1].casefold() == collection_hint.casefold()),
            None,
        )
    elif len(nonempty_entries) >= 2:
        candidate = nonempty_entries[-1]
        if candidate is not key_entry:
            collection_entry = candidate

    unmapped_text: list[str] = []
    if len(entries) >= 5:
        # Fully populated metadata has stable slots, including explicit empty
        # text entries. This retains the genre field from the calibrated pair.
        if entries[0][1]:
            metadata["genre"] = entries[0][1]
        if entries[1][1]:
            metadata["composer"] = entries[1][1]
        if entries[2][1]:
            metadata["key_text"] = entries[2][1]
        if entries[4][1]:
            metadata["collection"] = entries[4][1]
        unmapped_text.extend(entry[1] for entry in entries[5:] if entry[1])
    else:
        if key_entry:
            metadata["key_text"] = key_entry[1]
        remaining = [entry for entry in nonempty_entries if entry is not key_entry and entry is not collection_entry]
        if remaining:
            # When fields are sparse, the composer follows any descriptive
            # text in the observed records, so use the last remaining value.
            metadata["composer"] = remaining[-1][1]
            unmapped_text.extend(entry[1] for entry in remaining[:-1])
        if collection_entry:
            metadata["collection"] = collection_entry[1]

    if unmapped_text:
        metadata["unmapped_text"] = " | ".join(unmapped_text)
    if collection_hint:
        # A collection export supplies group membership even when a score's
        # own optional collection text field is empty.
        metadata["collection"] = collection_hint

    if key_entry:
        numeric_start = ((key_entry[2] + 3) // 4) * 4
        numeric_end = collection_entry[0] if collection_entry else metadata_end
        for offset in range(numeric_start, numeric_end - 15, 4):
            slots = [header[offset + index * 4 : offset + (index + 1) * 4] for index in range(4)]
            if any(len(slot) != 4 or slot[:2] != b"\x00\x00" or slot[3] != 0 for slot in slots):
                continue
            duration, rating, difficulty, tempo = [int.from_bytes(slot[2:4], "little") for slot in slots]
            if duration <= 24 * 60 * 60 and rating <= 5 and difficulty <= 15 and tempo <= 1000:
                if duration:
                    metadata["duration_seconds"] = duration
                metadata["rating"] = rating
                if difficulty:
                    metadata["difficulty"] = difficulty
                if tempo:
                    metadata["tempo"] = tempo
                break

    return metadata


def split_paths(raw: bytes, start: int, value_count: int) -> list[list[tuple[float, float]]]:
    values_start = start + 47
    values = struct.unpack_from(f">{value_count}f", raw, values_start)
    if len(values) < 3 or int(round(values[1])) != len(values) - 3:
        raise ValueError("Unexpected MobileSheets stroke coordinate array")
    remaining = value_count - 3
    if remaining % 2:
        raise ValueError("MobileSheets coordinate data has an unmatched value")

    paths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    payload_start = values_start + 12
    for index in range(0, remaining, 2):
        pair_offset = payload_start + index * 4
        x_bits, y_bits = struct.unpack_from(">II", raw, pair_offset)
        x, y = struct.unpack_from(">ff", raw, pair_offset)
        if x_bits == MAX_FLOAT_BITS and y_bits == MAX_FLOAT_BITS:
            if current:
                paths.append(current)
                current = []
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Unexpected non-finite MobileSheets coordinate")
        current.append((x, y))
    if current:
        paths.append(current)
    return paths


def smooth_path(path: list[tuple[float, float]], intensity: float) -> list[tuple[float, float]]:
    """Dampen point-to-point jitter while keeping stroke endpoints fixed.

    Intensity is in the range 0..1. Two lightweight Laplacian passes preserve
    the point count and endpoints while moving interior points toward the
    midpoint of their neighbors. Sparse paths are left alone to avoid bending
    short gestures too aggressively.
    """
    if intensity <= 0 or len(path) < 4:
        return path

    result = path
    for _ in range(2):
        previous = result
        result = [previous[0]]
        for index in range(1, len(previous) - 1):
            before_x, before_y = previous[index - 1]
            x, y = previous[index]
            after_x, after_y = previous[index + 1]
            midpoint_x = (before_x + after_x) / 2
            midpoint_y = (before_y + after_y) / 2
            result.append(
                (
                    x + (midpoint_x - x) * intensity,
                    y + (midpoint_y - y) * intensity,
                )
            )
        result.append(previous[-1])
    return result


def interpolate_width(
    width: float,
    calibration: tuple[tuple[float, float], ...],
    clamp_high: bool,
) -> float:
    """Map a MobileSheets brush width to an empirically matched forScore width."""
    if width <= calibration[0][0]:
        return calibration[0][1]
    for (source_low, target_low), (source_high, target_high) in zip(calibration, calibration[1:]):
        if width <= source_high:
            position = (width - source_low) / (source_high - source_low)
            return target_low + position * (target_high - target_low)
    if clamp_high:
        return calibration[-1][1]
    source_low, target_low = calibration[-2]
    source_high, target_high = calibration[-1]
    slope = (target_high - target_low) / (source_high - source_low)
    return calibration[-1][1] + (width - calibration[-1][0]) * slope


def calibrated_highlighter_width(width: float) -> float:
    """Map MobileSheets highlighter width to forScore PDF points."""
    return interpolate_width(width, HIGHLIGHTER_WIDTH_CALIBRATION, clamp_high=False)


def calibrated_pen_width(width: float) -> float:
    """Map MobileSheets pen width to forScore PDF points."""
    return interpolate_width(width, PEN_WIDTH_CALIBRATION, clamp_high=False)


def parse_msf(path: Path, collection_hint: str | None = None) -> ParsedSample:
    raw = path.read_bytes()
    pdf_offset = raw.find(b"%PDF-")
    if pdf_offset < 0:
        raise ValueError("Could not find the embedded PDF in the MobileSheets file")
    header, pdf = raw[:pdf_offset], raw[pdf_offset:]

    records: list[StrokeRecord] = []
    skipped: Counter[int] = Counter()
    current_page = 1
    page_count = 0
    group_has_record = False
    cursor = 0

    while cursor < len(header):
        record_start = header.find(ANNOTATION_START, cursor)
        page_end = header.find(PAGE_END, cursor)
        candidates = [position for position in (record_start, page_end) if position >= 0]
        if not candidates:
            break
        marker = min(candidates)

        if marker == page_end:
            page_count = max(page_count, current_page)
            current_page += 1
            group_has_record = False
            cursor = page_end + len(PAGE_END)
            continue

        if marker + 47 > len(header):
            raise ValueError("Incomplete MobileSheets annotation header")
        tool_id = header[marker + 8]

        # Unknown object types can use the same short header but have their
        # own payload layout. In particular, type 3 is a stamp reference, not
        # a path-point count. Find the common record terminator instead of
        # interpreting its resource ID as a coordinate count.
        if tool_id not in SUPPORTED_TOOLS:
            end = header.find(ANNOTATION_END, marker + 47)
            if end < 0:
                raise ValueError("Could not find the end of an unsupported annotation record")
            skipped[tool_id] += 1
            group_has_record = True
            cursor = end + len(ANNOTATION_END)
            continue

        value_count = struct.unpack_from(">I", header, marker + 43)[0]

        if value_count == 0:
            # Some object types use zero in the shared path-point count while
            # carrying their own payload. The observed type-6 records contain
            # two endpoints, but their meaning is unknown, so skip them for now.
            end = header.find(ANNOTATION_END, marker + 47)
            if end < 0:
                raise ValueError("Could not find the end of an empty annotation record")
            skipped[tool_id] += 1
            cursor = end + len(ANNOTATION_END)
            group_has_record = True
            continue

        values_start = marker + 47
        end = values_start + value_count * 4
        if end + len(ANNOTATION_END) > len(header):
            raise ValueError("Annotation point data runs beyond the MobileSheets header")
        if header[end : end + len(ANNOTATION_END)] != ANNOTATION_END:
            raise ValueError("Unrecognized MobileSheets annotation record layout")

        group_has_record = True
        page_width, page_height = struct.unpack_from(">ff", header, marker + 22)
        alpha = header[marker + 9]
        # The two floats at +10 and +14 are constant transform values in
        # the supplied files. The selected brush width is stored as a
        # float after the ARGB color at +35.
        brush_width = struct.unpack_from(">f", header, marker + 35)[0]
        if not math.isfinite(brush_width) or brush_width < 0:
            raise ValueError("Invalid MobileSheets brush width")
        argb = struct.unpack_from(">I", header, marker + 31)[0]
        color = ((argb >> 16) & 255, (argb >> 8) & 255, argb & 255)
        paths = split_paths(header, marker, value_count)
        records.append(
            StrokeRecord(
                page=current_page,
                page_width=page_width,
                page_height=page_height,
                width=brush_width,
                alpha=alpha,
                color=color,
                tool=SUPPORTED_TOOLS[tool_id],
                paths=paths,
            )
        )
        cursor = end + len(ANNOTATION_END)

    if group_has_record:
        page_count = max(page_count, current_page)
    metadata = parse_msf_metadata(header, path.stem, collection_hint)
    title = str(metadata["title"])
    return ParsedSample(pdf, title, metadata, records, max(page_count, 1), skipped)


def read_boxes(pdf: bytes, box_name: bytes) -> list[tuple[float, float, float, float]]:
    pattern = rb"/" + box_name + rb"\s*\[\s*([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s+([-+\d.]+)\s*\]"
    return [tuple(float(part) for part in match.groups()) for match in re.finditer(pattern, pdf)]  # type: ignore[misc]


def page_boxes(pdf: bytes, page_count: int, records: list[StrokeRecord]) -> list[tuple[float, float, float, float]]:
    media = read_boxes(pdf, b"MediaBox")
    crops = read_boxes(pdf, b"CropBox")
    fallback_width = records[0].page_width
    fallback_height = records[0].page_height
    if not media:
        media = [(0.0, 0.0, fallback_width, fallback_height)]

    result: list[tuple[float, float, float, float]] = []
    for index in range(page_count):
        media_box = media[min(index, len(media) - 1)]
        box = crops[min(index, len(crops) - 1)] if crops else media_box
        result.append(box)
    return result


def svg_for_page(
    width: int,
    height: int,
    page_box: tuple[float, float, float, float],
    records: list[StrokeRecord],
    width_multipliers: dict[str, float | None],
    smoothing: float,
) -> str:
    left, bottom, right, top = page_box
    page_width, page_height = right - left, top - bottom
    scale = min(width / page_width, height / page_height)
    offset_x = (width - page_width * scale) / 2
    offset_y = (height - page_height * scale) / 2

    # MobileSheets coordinates in the samples use a top-left page origin.
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for record in records:
        r, g, b = record.color
        color = f"rgb({r},{g},{b})"
        # MobileSheets pen records carry the same auxiliary alpha byte as its
        # highlighter records, but the user confirmed the pen is opaque.
        opacity = max(0, min(record.alpha, 255)) / 255 if record.tool == "highlighter" else 1.0
        multiplier = width_multipliers[record.tool]
        if multiplier is None:
            target_width = (
                calibrated_highlighter_width(record.width)
                if record.tool == "highlighter"
                else calibrated_pen_width(record.width)
            )
        else:
            target_width = record.width * multiplier
        stroke_width = target_width * scale
        crop_top = record.page_height - top
        paths = record.paths
        if record.tool == "pen" and smoothing:
            paths = [smooth_path(path, smoothing) for path in paths]
        for path in paths:
            if not path:
                continue
            commands = []
            for index, (x, y) in enumerate(path):
                sx = offset_x + (x - left) * scale
                sy = offset_y + (y - crop_top) * scale
                commands.append(("M" if index == 0 else "L") + f"{sx:.3f},{sy:.3f}")
            svg.append(
                f'<path d="{" ".join(commands)}" stroke="{color}" '
                f'stroke-opacity="{opacity:.5f}" stroke-width="{stroke_width:.3f}"/>'
            )
    svg.extend(("</g>", "</svg>"))
    return "\n".join(svg)


def forscore_package(template_path: Path | None, sample: ParsedSample) -> tuple[dict, str, bytes]:
    if template_path:
        template = read_gzip_plist(template_path)
        title_key = next((key for key in template if isinstance(key, str) and key.endswith("|title")), None)
        if title_key is None or not isinstance(template.get(title_key), str):
            raise ValueError("The clean forScore template has no title field")
        pdf = template.get("pdfData")
        if not isinstance(pdf, bytes):
            raise ValueError("The clean forScore template has no PDF data")
        return template, title_key[:-6], pdf

    base_name = sample.title + ".pdf"
    title_key = base_name + "|title"
    return {"pdfData": sample.pdf, title_key: sample.title}, base_name, sample.pdf


def forscore_key_code(key_text: str) -> int | None:
    """Encode a key label using the confirmed forScore key pattern.

    Samples support C# major/minor as 120/121, C major as 110, B-flat major
    as 700, E# minor as 321, and D major as 210. These confirm a three-digit
    encoding: note position, accidental (flat/natural/sharp = 0/1/2), then
    mode (major/minor = 0/1).
    """
    normalized = key_text.strip().lower().replace("♯", "#").replace("♭", "b")
    normalized = re.sub(r"[\s-]+", "", normalized)
    normalized = normalized.replace("sharp", "#").replace("flat", "b")
    match = re.fullmatch(r"([a-g])([#b]?)(major|minor|maj|min|m)", normalized)
    if not match:
        return None

    root, accidental, mode_name = match.groups()
    note_code = {"c": 1, "d": 2, "e": 3, "f": 4, "g": 5, "a": 6, "b": 7}[root]
    accidental_code = {"b": 0, "": 1, "#": 2}[accidental]
    mode_code = 1 if mode_name in {"minor", "min", "m"} else 0
    return note_code * 100 + accidental_code * 10 + mode_code


def add_forscore_metadata(output: dict, base_name: str, metadata: dict[str, object]) -> tuple[list[str], list[str]]:
    """Copy mapped MobileSheets metadata into the forScore package plist."""
    transferred: list[str] = []
    notes: list[str] = []
    unmapped_text = metadata.get("unmapped_text")
    if isinstance(unmapped_text, str) and unmapped_text:
        notes.append(f"Unmapped MobileSheets text metadata: {unmapped_text}")
    direct_fields = {
        "title": "title",
        "composer": "composer",
        "genre": "genre",
        "keywords": "keywords",
        "reference": "reference",
        "rating": "rating",
        "tempo": "bpm",
    }
    for source_field, target_field in direct_fields.items():
        value = metadata.get(source_field)
        if value is not None and value != "":
            output[f"{base_name}|{target_field}"] = value
            transferred.append(target_field)

    collection_field = metadata.get("collection_field", "labels")
    collection_values: list[str] = []
    collection = metadata.get("collection")
    if isinstance(collection, str) and collection.strip():
        collection_values.append(collection.strip())
    collections = metadata.get("collections")
    if isinstance(collections, (list, tuple)):
        collection_values.extend(str(value).strip() for value in collections if str(value).strip())
    if collection_values:
        if collection_field not in ("labels", "keywords"):
            notes.append(f"Unsupported forScore collection field {collection_field!r}; collections were not added")
        else:
            values: list[str] = []
            existing = output.get(f"{base_name}|{collection_field}")
            if isinstance(existing, str):
                values.extend(part.strip() for part in existing.split(",") if part.strip())
            values.extend(collection_values)
            values = list(dict.fromkeys(values))
            # forScore Tags (keywords) and Labels both use comma-separated values.
            output[f"{base_name}|{collection_field}"] = ", ".join(values)
            transferred.append(collection_field)

    duration = metadata.get("duration_seconds")
    if isinstance(duration, int):
        minutes, seconds = divmod(duration, 60)
        output[f"{base_name}|minutes"] = minutes
        output[f"{base_name}|seconds"] = seconds
        transferred.extend(("minutes", "seconds"))

    difficulty = metadata.get("difficulty")
    if isinstance(difficulty, int):
        if 1 <= difficulty <= 15:
            # MobileSheets 1–15 into forScore's three dots: 1–5 -> 1,
            # 6–10 -> 2, and 11–15 -> 3.
            output[f"{base_name}|difficulty"] = (difficulty - 1) // 5 + 1
            transferred.append("difficulty")
        else:
            notes.append(f"MobileSheets difficulty {difficulty} is outside the supported 1–15 range")

    key_text = metadata.get("key_text")
    if isinstance(key_text, str) and key_text:
        key_code = forscore_key_code(key_text)
        if key_code is None:
            notes.append(f"No verified forScore key code is known for {key_text!r}; key was not changed")
        else:
            output[f"{base_name}|key"] = key_code
            transferred.append("key")

    return transferred, notes


def convert(
    msf_path: Path,
    reference_path: Path,
    output_path: Path,
    template_path: Path | None = None,
    smoothing: float = DEFAULT_SMOOTHING,
    pen_width_scale: float | None = None,
    highlighter_width_scale: float | None = None,
    collection_label: str | None = None,
    collection_field: str = "labels",
) -> None:
    if not math.isfinite(smoothing) or not 0 <= smoothing <= 1:
        raise ValueError("Smoothing must be a number between 0 and 1")
    if pen_width_scale is not None and (not math.isfinite(pen_width_scale) or pen_width_scale < 0):
        raise ValueError("Pen width scale must be a finite, non-negative number")
    if highlighter_width_scale is not None and (
        not math.isfinite(highlighter_width_scale) or highlighter_width_scale < 0
    ):
        raise ValueError("Width scales must be finite, non-negative numbers")
    if collection_field not in {"labels", "keywords"}:
        raise ValueError("Collection field must be 'labels' or 'keywords' (forScore Tags)")

    sample = parse_msf(msf_path, collection_label)
    output, base_name, pdf = forscore_package(template_path, sample)
    metadata = {**sample.metadata, "collection_field": collection_field}
    transferred_metadata, metadata_notes = add_forscore_metadata(output, base_name, metadata)
    reference = read_gzip_plist(reference_path)
    png_reference = next(
        (value for key, value in reference.items() if isinstance(key, str) and key.endswith(".png") and isinstance(value, bytes)),
        None,
    )
    if png_reference is None:
        raise ValueError("The forScore reference package needs at least one annotated page")
    canvas_width, canvas_height = png_dimensions(png_reference)

    magick = shutil.which("magick")
    if not magick:
        raise RuntimeError("ImageMagick 7 ('magick') is required to render transparent forScore layers")

    boxes = page_boxes(pdf, sample.page_count, sample.records) if sample.records else []
    width_multipliers = {"pen": pen_width_scale, "highlighter": highlighter_width_scale}
    for key in list(output):
        if isinstance(key, str) and key.startswith(base_name + "|") and key.endswith(".png"):
            del output[key]

    with tempfile.TemporaryDirectory(prefix="msf-to-forscore-") as temp_dir:
        temp = Path(temp_dir)
        for page in sorted({record.page for record in sample.records}):
            if page < 1 or page > len(boxes):
                raise ValueError(f"Annotation points to page {page}, but the PDF has {len(boxes)} pages")
            page_records = [record for record in sample.records if record.page == page]
            svg_path = temp / f"page-{page}.svg"
            png_path = temp / f"page-{page}.png"
            svg_path.write_text(
                svg_for_page(
                    canvas_width,
                    canvas_height,
                    boxes[page - 1],
                    page_records,
                    width_multipliers,
                    smoothing,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [magick, "-background", "none", str(svg_path), f"PNG32:{png_path}"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            output[f"{base_name}|{page}.png"] = png_path.read_bytes()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    binary_plist = plistlib.dumps(output, fmt=plistlib.FMT_BINARY, sort_keys=False)
    output_path.write_bytes(gzip.compress(binary_plist, compresslevel=9, mtime=0))

    tool_counts = Counter(record.tool for record in sample.records)
    print(f"Wrote {output_path}")
    if sample.records:
        print(f"Converted {len(sample.records)} records across {sample.page_count} page group(s): {dict(tool_counts)}")
    else:
        print("No supported pen or highlighter annotations; preserved the score PDF and metadata")
    if sample.skipped:
        details = ", ".join(
            f"type {tool_id} ({UNSUPPORTED_TOOL_NAMES.get(tool_id, 'unknown')}): {count}"
            for tool_id, count in sorted(sample.skipped.items())
        )
        print(f"Skipped unsupported annotation records: {details}")
    if transferred_metadata:
        print(f"Transferred metadata field(s): {', '.join(transferred_metadata)}")
    for note in metadata_notes:
        print(f"Metadata note: {note}")
    if highlighter_width_scale is None:
        print("Applied the empirical highlighter width curve")
    if pen_width_scale is None:
        print("Applied the empirical pen width curve")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("msf", type=Path, help="MobileSheets .msf file")
    parser.add_argument("reference_4sc", type=Path, help="Annotated forScore .4sc from the target device, used for page-layer dimensions")
    parser.add_argument("output", type=Path, help="Destination .4sc file")
    parser.add_argument("--template", type=Path, help="Optional clean forScore .4sc for the same score; otherwise the embedded MobileSheets PDF is used")
    parser.add_argument("--collection-label", help="Optional forScore label to apply to every converted score in a collection export")
    parser.add_argument(
        "--collection-field",
        choices=("labels", "keywords"),
        default="labels",
        help="forScore destination for MobileSheets collections: Labels by default, or Tags via keywords",
    )
    parser.add_argument(
        "--smoothing",
        type=float,
        default=DEFAULT_SMOOTHING,
        help=f"Pen-path smoothing intensity from 0 (off) to 1 (strong); defaults to {DEFAULT_SMOOTHING}",
    )
    parser.add_argument(
        "--pen-width-scale",
        type=float,
        default=None,
        help="Optional fixed pen-width multiplier; by default, use the empirical calibration curve",
    )
    parser.add_argument(
        "--highlighter-width-scale",
        type=float,
        default=None,
        help="Optional fixed highlighter multiplier; by default, use the empirical calibration curve",
    )
    args = parser.parse_args()
    convert(
        args.msf,
        args.reference_4sc,
        args.output,
        args.template,
        smoothing=args.smoothing,
        pen_width_scale=args.pen_width_scale,
        highlighter_width_scale=args.highlighter_width_scale,
        collection_label=args.collection_label,
        collection_field=args.collection_field,
    )


if __name__ == "__main__":
    main()
