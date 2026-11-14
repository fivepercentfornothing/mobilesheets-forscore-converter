# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import gzip
import io
import plistlib
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import msf_to_forscore as converter


def text_entry(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return b"\x00\x01" + struct.pack(">H", len(encoded)) + encoded


def synthetic_msf() -> tuple[bytes, bytes]:
    title = b"Synthetic Score"
    header = bytearray(converter.TITLE_MARKER)
    header.extend(struct.pack(">H", len(title)))
    header.extend(title)
    header.extend(text_entry("C major"))
    header.extend(text_entry("Test Collection"))
    header.extend(converter.METADATA_END)

    record = bytearray(47)
    record[:4] = converter.ANNOTATION_START
    record[8] = 1  # Pen
    record[9] = 255
    struct.pack_into(">ff", record, 22, 200.0, 300.0)
    struct.pack_into(">I", record, 31, 0xFF202060)
    struct.pack_into(">f", record, 35, 5.0)
    points = (0.0, 4.0, 0.0, 20.0, 30.0, 180.0, 270.0)
    struct.pack_into(">I", record, 43, len(points))
    header.extend(record)
    header.extend(struct.pack(f">{len(points)}f", *points))
    header.extend(converter.ANNOTATION_END)

    pdf = b"%PDF-1.4\n1 0 obj << /MediaBox [0 0 200 300] >>\n%%EOF\n"
    return bytes(header) + pdf, pdf


def reference_layer(width: int = 64, height: int = 96) -> bytes:
    png = bytearray(24)
    png[:8] = b"\x89PNG\r\n\x1a\n"
    struct.pack_into(">II", png, 16, width, height)
    return bytes(png)


@unittest.skipUnless(shutil.which("magick"), "ImageMagick 7 is required for annotation rendering")
class MobileSheetsScoreConversionTests(unittest.TestCase):
    def test_end_to_end_conversion_renders_layer_and_routes_collection(self):
        raw_msf, pdf = synthetic_msf()
        reference = {
            "Reference.pdf|title": "Reference",
            "Reference.pdf|1.png": reference_layer(),
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            msf_path = temp / "synthetic.msf"
            reference_path = temp / "reference.4sc"
            msf_path.write_bytes(raw_msf)
            reference_path.write_bytes(gzip.compress(plistlib.dumps(reference), mtime=0))

            for option, target_field in ((None, "labels"), ("keywords", "keywords")):
                with self.subTest(target_field=target_field):
                    output_path = temp / f"converted-{target_field}.4sc"
                    kwargs = {"smoothing": 0}
                    if option is not None:
                        kwargs["collection_field"] = option
                    with contextlib.redirect_stdout(io.StringIO()):
                        converter.convert(msf_path, reference_path, output_path, **kwargs)

                    package = converter.read_gzip_plist(output_path)
                    self.assertEqual(package["pdfData"], pdf)
                    self.assertEqual(package["Synthetic Score.pdf|title"], "Synthetic Score")
                    self.assertEqual(package[f"Synthetic Score.pdf|{target_field}"], "Test Collection")
                    other_field = "keywords" if target_field == "labels" else "labels"
                    self.assertNotIn(f"Synthetic Score.pdf|{other_field}", package)

                    layer = package["Synthetic Score.pdf|1.png"]
                    self.assertTrue(layer.startswith(b"\x89PNG\r\n\x1a\n"))
                    self.assertEqual(converter.png_dimensions(layer), (64, 96))


if __name__ == "__main__":
    unittest.main()
