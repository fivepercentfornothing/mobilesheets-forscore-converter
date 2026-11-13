# MobileSheets → forScore Converter

> **AI disclosure:** This is a 100% AI-coded project. The project owner sets its goals and validates the results in the target apps.

An experimental, local-first tool for moving a MobileSheets music library to forScore. It converts individual MobileSheets score exports (`.msf`) or a library backup (`.msb`) into forScore scores (`.4sc`), setlists (`.4ss`), and an optional full-library archive (`.4sb`).

## What it transfers

- Original score PDFs and supported score metadata.
- Pen and highlighter marks as transparent forScore annotation layers. The layers can be erased in forScore, but MobileSheets strokes are not preserved as individually selectable vector paths.
- Collection memberships as forScore Tags by default, or Labels when requested.
- Setlist membership and order from a MobileSheets database backup.
- An optional `.4sb` archive for restoring the converted library in forScore.

The converter is based on reverse-engineered file layouts and has been checked with MobileSheets 3.9.45 and forScore 15.2.6 exports. Other app versions may use different layouts. Always inspect a conversion in forScore before relying on it as your only copy.

## Current limitations

- Only pen and highlighter annotations are supported. Stamps and unrecognized annotation types are skipped and reported.
- Page crop and zoom state, bookmarks, and playback or metronome settings are not transferred.
- Highlighter opacity and blend behavior are not an exact match. Pen smoothing is configurable, but does not reproduce MobileSheets' internal smoothing algorithm.
- The database reader expects one PDF per score. Scores with multiple source PDFs need additional handling.
- Width calibration and key-signature mapping are empirical and may need adjustment for other app versions or libraries.

The conversion writes review reports alongside the output, including unsupported annotations, unusual metadata, embedded PDF properties, and page settings that were not applied.

## Requirements

- Python 3.10 or newer.
- ImageMagick 7 (`magick`) to render transparent annotation layers.
- `pdfinfo` is optional; when installed, it adds PDF properties to the review report.

The converters use Python's standard library and have no third-party Python package dependencies.

## Convert a library backup

```sh
python3 msb_to_forscore.py \
  /path/to/library.msb \
  /path/to/converted-library \
  --archive-4sb /path/to/converted-library.4sb
```

The output folder contains `scores/` (`.4sc`), `setlists/` (`.4ss`), a conversion manifest, and review reports. Add `--zip /path/to/converted-library.zip` to create a ZIP bundle as well.

MobileSheets collections are written to forScore Tags by default. Use `--collection-field labels` to write them as Labels instead. Pen smoothing defaults to `0.75`; set `--smoothing 0` to disable it. The width curves can be replaced with fixed scale values using `--pen-width-scale` and `--highlighter-width-scale`.

## Convert one score export

Single-score conversion uses an annotated forScore `.4sc` from the target device to determine annotation-layer dimensions. A clean forScore `.4sc` for the same score can optionally be supplied as the PDF template.

```sh
python3 msf_to_forscore.py \
  /path/to/score.msf \
  /path/to/annotated-reference.4sc \
  /path/to/converted-score.4sc \
  --template /path/to/clean-score.4sc
```

Omit `--template` to use the PDF embedded in the MobileSheets export.

## Check the converter

Run the focused parser and archive tests with:

```sh
python3 -m unittest -v test_msb_to_forscore.py
```

The full-library command also checks generated score packages, setlist contents and order, and the optional archive's internal round trip. A successful file-generation check does not replace restoring the archive in forScore and visually inspecting the results.

## Optional AI-assisted review

An AI assistant can help review the conversion manifest and review reports, explain skipped records, or compare a few representative output scores with their source. Treat its feedback as a second opinion and verify important results in forScore. Conversion reports and score files can contain private metadata and copyrighted music; only share them with an AI service if you are comfortable with that service receiving those files.

## Privacy

Conversion runs locally and does not upload files. Generated scores, archives, manifests, and reports contain the source library's music, annotations, titles, metadata, and other identifying details. Keep the output private unless you have reviewed it and intend to share it. The source PDFs are retained as-is, including their embedded document properties.

## Project status

This experimental migration tool may change as more file variants are understood. The repository does not yet declare a software license; choose one before distributing or accepting contributions under specific terms.
