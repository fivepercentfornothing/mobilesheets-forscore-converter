# MobileSheets → forScore Converter

> **AI disclosure:** This is a 100% AI-coded project. The project owner sets its goals and validates the results in the target apps.

An experimental, local-first tool for moving a MobileSheets music library to forScore. It converts individual MobileSheets score exports (`.msf`) or a library backup (`.msb`) into forScore scores (`.4sc`), setlists (`.4ss`), and an optional full-library archive (`.4sb`).

**License:** GNU General Public License v3.0 or any later version. See [`LICENSE`](LICENSE).

## Independent project

This is a user-made, independent project. It is not affiliated with, endorsed by, or sponsored by forScore LLC or Zubersoft. “forScore” and “MobileSheets” refer to their respective products; their names and trademarks belong to their respective owners.

## What it transfers

- Original score PDFs and supported score metadata.
- Pen and highlighter marks as transparent forScore annotation layers. The layers can be erased in forScore, but MobileSheets strokes are not preserved as individually selectable vector paths.
- Collection memberships as forScore Labels by default, or Tags when requested.
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

## Disclaimer

This software is provided “AS IS,” without warranties of any kind, to the extent permitted by applicable law. Use it at your own risk. Conversion can produce missing, altered, misaligned, damaged, or unusable scores, annotations, metadata, setlists, or archives.

Keep separate backups of your original library and review the converted files in forScore before replacing or deleting originals, or relying on the converted library. To the extent permitted by law, the authors and contributors are not liable for data loss, data corruption, inaccurate output, or other damages arising from using or being unable to use this software. Nothing in this notice limits liability that cannot legally be excluded. The warranty disclaimer and liability limitation in [`LICENSE`](LICENSE) also apply.

## Requirements

- Python 3.10 or newer.
- ImageMagick 7 (`magick`) to render transparent annotation layers.
- `pdfinfo` is optional; when installed, it adds PDF properties to the review report.

The converters use Python's standard library and have no third-party Python package dependencies.

## Prepare an annotated forScore reference

Both conversion commands require an annotated forScore `.4sc` file. forScore stores annotations in PNG page layers; the converter reads the pixel width and height from the reference's first PNG layer and uses that exact canvas size for the annotations it creates. This avoids guessing a canvas size. The reference's PDF, annotations, and metadata are not copied into the converted scores.

To create the reference:

1. On the forScore device where you will use the converted files, choose a score page with an orientation representative of your library. If you already have a score with annotations, you can use it.
2. If you do not have an annotated score, make a small temporary pen mark on a disposable score or a copy. forScore needs to create an annotation layer for the export to contain the dimensions the converter reads.
3. In forScore, use **Share** from the Tools menu or the score's title-bar menu. Choose the **4SC** format, then save the exported file to Files or transfer it to the computer running the converter. Do not export a PDF: PDFs do not contain the editable PNG annotation layer used to read the canvas size. See forScore's [sharing instructions](https://forscore.co/kb/sharing-scores/).
4. Use that `.4sc` file as the reference argument shown below. The temporary mark remains only in the reference file and is not copied into converted scores.

The selected dimensions are used for every annotation layer in that conversion run. For a library conversion, choose a reference page with the same orientation and forScore setup as the scores you are converting.

## Convert a library backup

```sh
python3 msb_to_forscore.py \
  /path/to/library.msb \
  /path/to/converted-library \
  --reference-4sc /path/to/annotated-reference.4sc \
  --archive-4sb /path/to/converted-library.4sb
```

The output folder contains `scores/` (`.4sc`), `setlists/` (`.4ss`), a conversion manifest, and review reports. Add `--zip /path/to/converted-library.zip` to create a ZIP bundle as well.

The `--reference-4sc` argument is required; the library converter does not assume a default canvas size.

MobileSheets collections are written to forScore Labels by default. Use `--collection-field keywords` to write them as Tags instead. Pen smoothing defaults to `0.75`; set `--smoothing 0` to disable it. The width curves can be replaced with fixed scale values using `--pen-width-scale` and `--highlighter-width-scale`.

## Convert one score export

The annotated forScore `.4sc` reference is required for the annotation-layer dimensions; see [Prepare an annotated forScore reference](#prepare-an-annotated-forscore-reference) for why and how to create it. The reference can be a different score. If you supply `--template`, that clean `.4sc` provides the score PDF; otherwise, the PDF embedded in the MobileSheets file is used.

```sh
python3 msf_to_forscore.py \
  /path/to/score.msf \
  /path/to/annotated-reference.4sc \
  /path/to/converted-score.4sc \
  --template /path/to/clean-score.4sc
```

Omit `--template` to use the PDF embedded in the MobileSheets export.

Collections go to forScore Labels by default. Add `--collection-field keywords` to use Tags instead.

## Check the converter

Run the parser, archive, and synthetic end-to-end conversion tests with:

```sh
python3 -m unittest -v
```

The suite includes parser and archive checks, plus a synthetic end-to-end `.msf` conversion. That test builds its own tiny score and reference file in a temporary directory, runs ImageMagick to render an annotation PNG, and checks the converted PDF, layer dimensions, and collection destination. It does not need any real score or sample files. The end-to-end test is skipped if ImageMagick 7 (`magick`) is not installed.

The full-library command also checks generated score packages, setlist contents and order, and the optional archive's internal round trip. A successful file-generation check does not replace restoring the archive in forScore and visually inspecting the results.

## Optional AI-assisted review

An AI assistant can help review the conversion manifest and review reports, explain skipped records, or compare a few representative output scores with their source. Treat its feedback as a second opinion and verify important results in forScore. Conversion reports and score files can contain private metadata and copyrighted music; only share them with an AI service if you are comfortable with that service receiving those files.

## Privacy

Conversion runs locally and does not upload files. Generated scores, archives, manifests, and reports contain the source library's music, annotations, titles, metadata, and other identifying details. Keep the output private unless you have reviewed it and intend to share it. The source PDFs are retained as-is, including their embedded document properties.

## Project status

This experimental migration tool may change as more file variants are understood. Contributions and distribution are covered by the license listed above.
