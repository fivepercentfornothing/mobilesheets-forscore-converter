import gzip
import plistlib
import struct
import unittest

import msb_to_forscore as converter
import msf_to_forscore as core


class MobileSheetsBackupTests(unittest.TestCase):
    def test_embedded_sqlite_bounds(self):
        raw = bytearray(1024 + 24 + 5)
        raw[:16] = converter.SQLITE_MAGIC
        struct.pack_into(">H", raw, 16, 1024)
        struct.pack_into(">I", raw, 28, 1)
        raw[1024 + 24 : 1024 + 29] = b"%PDF-"
        self.assertEqual(converter.embedded_database_bounds(raw), (0, 1024))

    def test_pdf_record_is_mapped_by_song_id(self):
        pdf = b"%PDF-%%EOF"
        raw = bytearray(converter.PDF_RECORD_HEADER_SIZE + len(pdf))
        struct.pack_into(">I", raw, 12, 58)
        raw[converter.PDF_RECORD_HEADER_SIZE :] = pdf
        result = converter.extract_pdf_records(raw, 0, [(161, 58, "score.pdf", len(pdf), 1)])
        self.assertEqual(result, {58: pdf})

    def test_database_point_blob_splits_subpaths(self):
        values = (0.0, 6.0, 2.0, 1.0, 2.0, converter.PDF_SENTINEL, converter.PDF_SENTINEL, 3.0, 4.0)
        blob = struct.pack("<9d", *values)
        self.assertEqual(
            converter.parse_point_blob(blob, 9, 7),
            [[(1.0, 2.0)], [(3.0, 4.0)]],
        )

    def test_4ss_archive_round_trip_preserves_list_and_score(self):
        score = {
            "pdfData": b"%PDF-%%EOF",
            "Score.pdf|title": "Score",
            "Score.pdf|labels": "Collection A, Collection B",
        }
        raw = converter.make_4ss_archive(
            [("Score", "test-id", "Score.pdf")],
            [score],
        )
        archive = plistlib.loads(gzip.decompress(raw))
        entries = [
            converter.resolve_archive_dictionary(archive, node)
            for node in converter.resolve_archive_array(archive, "setlist")
        ]
        files = [
            converter.resolve_archive_dictionary(archive, node)
            for node in converter.resolve_archive_array(archive, "files")
        ]
        self.assertEqual(entries[0]["FilePath"], "Score.pdf")
        self.assertEqual(files[0]["Score.pdf|labels"], "Collection A, Collection B")
        self.assertEqual(files[0]["pdfData"], b"%PDF-%%EOF")

    def test_4sb_archive_round_trip_preserves_index_pdfs_layers_and_setlists(self):
        pdf = b"%PDF-%%EOF"
        image = b"\x89PNG\r\n\x1a\nlayer"
        song = converter.SongInfo(
            song_id=7,
            title="Score",
            pdf_name="Score.pdf",
            pdf_size=len(pdf),
            page_count=1,
            metadata={"composer": "Composer"},
            memberships=["Collection A", "Collection B"],
            setlists=["List"],
            warnings=[],
        )
        database = converter.DatabaseContents(
            songs={7: song},
            pdf_bytes={7: pdf},
            setlists=[(3, "List", [7])],
            annotations={},
            skipped_annotations={},
            page_settings_review=[],
            unusual_metadata=[],
            database_sha256="test-database",
        )
        package = {
            "pdfData": pdf,
            "Score.pdf|title": "Score",
            "Score.pdf|composer": "Composer",
            "Score.pdf|keywords": "Collection A, Collection B",
            "Score.pdf|1.png": image,
        }
        metadata, assets = converter.build_4sb_contents(database, {7: package})
        raw = converter.make_4sb_archive("test.4sb", metadata, assets)
        archive_name, unpacked_metadata, unpacked_assets = converter.parse_4sb_archive(raw)

        self.assertEqual(archive_name, "test.4sb")
        self.assertEqual(unpacked_metadata["Score.pdf|keywords"], "Collection A, Collection B")
        self.assertEqual(unpacked_metadata["&SYS;setlists"], ["&SET;List"])
        self.assertEqual(unpacked_metadata["&SET;List"][0]["FilePath"], "Score.pdf")
        self.assertEqual(
            unpacked_assets,
            [
                ("{%DOCUMENTS_DIR%}/Score.pdf", pdf),
                ("{%AUX_DIR%}/Score.pdf|1.png", image),
            ],
        )

    def test_multiple_mobile_collections_become_comma_separated_labels(self):
        package = {}
        core.add_forscore_metadata(package, "Score.pdf", {"collections": ["Collection A", "Collection B"]})
        self.assertEqual(package["Score.pdf|labels"], "Collection A, Collection B")

    def test_database_collections_can_be_written_as_forscore_tags(self):
        package = {"Score.pdf|keywords": "Choir"}
        core.add_forscore_metadata(
            package,
            "Score.pdf",
            {"collections": ["Collection A", "Collection B"], "collection_field": "keywords"},
        )
        self.assertEqual(package["Score.pdf|keywords"], "Choir, Collection A, Collection B")

    def test_pdfinfo_property_parser_keeps_creator_and_producer(self):
        fields = converter.parse_pdfinfo_output(
            "Title: Score\nCreator: Example PDF editor\nProducer: Example PDF printer\nPages: 4\n"
        )
        self.assertEqual(fields["Creator"], "Example PDF editor")
        self.assertEqual(fields["Producer"], "Example PDF printer")
        self.assertNotIn("Pages", fields)


if __name__ == "__main__":
    unittest.main()
