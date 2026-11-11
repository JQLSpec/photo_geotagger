# Contributing to gpxtag

Thanks for your interest in improving `gpxtag`! This document covers how to
set up, make changes, and submit them.

## Setup

You need:

- Python 3.9 or newer
- [exiftool](https://exiftool.org) 10.28 or newer on your `PATH`
  (`brew install exiftool` or `apt install libimage-exiftool-perl`)

There are no Python packages to install. Clone the repo and run:

```sh
python3 gpxtag.py --help
```

## Design principles

Please keep these in mind; changes that break them are unlikely to be merged.

- **One file, standard library only.** `gpxtag.py` should stay a single script
  that can be copied onto `PATH` and run with no `pip install`.
- **Don't damage photos.** Default behaviour must stay conservative: skip
  photos that already have GPS, preserve file modification times, and make
  `--dry-run` show exactly what a real run would do.
- **Explain, don't guess.** When a photo can't be tagged, say why and, where
  possible, what option would fix it. Prefer a clear skip over a wrong
  position.
- **Fast on large folders.** exiftool is started once for reading and once for
  writing, regardless of how many photos there are.
- **Respect the minimum exiftool version (10.28).** If a change needs a newer
  exiftool feature, raise `MIN_EXIFTOOL` in `gpxtag.py` and update the docs to
  match.

## Making changes

1. Create a branch from `main`.
2. Make your change in `gpxtag.py`. Follow the existing style:
   - Google-style docstrings (`Args:`, `Returns:`, `Raises:`) written in short,
     plain sentences.
   - Type hints using `typing` (`List`, `Optional`, `Tuple`) for Python 3.9
     compatibility.
   - Friendly, actionable wording for anything printed to the user.
3. If you add or change an option, default, output format or exit code, update
   the `argparse` help, the module docstring examples, and `README.md`.

## Testing

There is no automated test suite yet — contributions that add one (using
`unittest` from the standard library) are welcome.

Before submitting, please at least:

```sh
python3 -m py_compile gpxtag.py
python3 gpxtag.py --help
```

and run your change against real data. **Work on copies of photos**, and use
`--dry-run` first:

```sh
mkdir /tmp/gpxtag-test && cp ~/Photos/some-trip/*.jpg /tmp/gpxtag-test/
python3 gpxtag.py track.gpx /tmp/gpxtag-test --dry-run
python3 gpxtag.py track.gpx /tmp/gpxtag-test
exiftool -gps:all /tmp/gpxtag-test
```

Useful cases to check, depending on what you changed:

- Photos with and without a stored UTC offset (`OffsetTimeOriginal`),
  including older photos that have no EXIF 2.31 offset tags
- `--tz` with a place name across a daylight-saving change, and with a fixed
  offset (including negative values like `--tz -8`)
- `--shift` with positive and negative values
- Multiple GPX files, multi-segment tracks, and tracks with gaps longer than
  `--max-gap`
- GPX files with no timestamps (routes or waypoints only)
- File names with non-ASCII characters
- Photos that already have GPS, with and without `--overwrite`

## Submitting

- Keep pull requests focused on one change.
- Describe what the change does and how you tested it, including the commands
  you ran and, if relevant, sample output.
- If you're fixing a bug, include the inputs (or a minimal GPX snippet and the
  relevant EXIF fields) that reproduce it.

## Reporting bugs

Please include:

- The exact command you ran and its full output (a `--dry-run` is fine)
- Your Python version (`python3 --version`) and exiftool version
  (`exiftool -ver`)
- The camera or phone model, and the output of
  `exiftool -time:all -gps:all PHOTO` for an affected photo
- If possible, the relevant part of the GPX file
