# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project overview

`gpxtag` is a single-file Python CLI (`gpxtag.py`) that geotags photos from GPX
track logs. It reads photo capture times with exiftool, looks up the position
at that time in the GPX track(s), and writes GPS EXIF tags back with exiftool.
See `README.md` for user-facing behaviour.

## Layout

Everything lives in `gpxtag.py`, split into sections by `# ---` banner comments:

| Section | Key items |
| --- | --- |
| GPX parsing | `TrackPoint`, `parse_iso_time`, `_read_gpx`, `load_tracks` |
| Position lookup | `Fix`, `Track.locate` (exact → interpolate → nearest) |
| Time helpers | `parse_shift`, `resolve_tz`, `photo_offset`, `photo_timestamp`, `fmt_*` |
| exiftool I/O | `READ_TAGS`, `read_photos`, `write_gps` |
| Main | `main()` – argument parsing, the per-photo loop, summary, hints |

The module docstring doubles as the `--help` epilog (everything from
"Time zones are" onward), so edit it with that in mind.

## Hard constraints

- **Standard library only.** No third-party Python packages. exiftool is the
  only external dependency and is invoked via `subprocess`.
- **Keep it one file.** The tool is meant to be copied onto `PATH` and run.
  Don't split it into a package unless asked.
- **Python 3.9+** is the target. Keep the `zoneinfo` import fallback so fixed
  offsets still work without it. Use `typing.List`/`Optional`/`Tuple`, not
  `list[...]` / `X | None`, to match existing code.
- **Times are POSIX timestamps (float, UTC)** everywhere internally. Convert to
  local/clock time only for display.
- **Never interpolate across segments.** Each `<trkseg>` across all files gets
  a unique `seg` number from a shared `itertools.count`; preserve that.
- **exiftool 10.28+** (`MIN_EXIFTOOL`, checked at startup). 10.28 added the
  EXIF 2.31 time-zone tags. If a change needs a newer exiftool feature, look
  up the version in exiftool's `Changes` file and raise `MIN_EXIFTOOL`, the
  module docstring, README and CONTRIBUTING together.
- **Batch exiftool calls.** Reading is one `exiftool -json` call; writing is
  one call driven by a temporary `-@` argfile with `-execute` per photo. Don't
  introduce per-photo subprocess calls.
- **Pass `-charset filename=utf8`** on every exiftool call so non-ASCII paths
  work.
- **Safe defaults:** photos that already have GPS are skipped unless
  `--overwrite`; mtimes are preserved (`-P`) unless `--no-preserve-mtime`.
  Don't change these defaults.
- **Exit codes** (documented in README): 0 success, 1 exiftool/fatal errors,
  2 usage error or no timed GPX points, 130 on Ctrl+C. Fatal errors use
  `sys.exit("error: ...")`.

## Style conventions

- **Docstrings** use Google style (`Args:` / `Returns:` / `Raises:`) and are
  written in plain, simplified technical English: short declarative sentences,
  no contractions, phrasing like "Change X into Y" and "The function ...".
  Match this style for new or edited docstrings.
- **Inline comments** follow the same simplified style and explain *why* or
  name the step ("Method 2: interpolate between a and b.").
- **User-facing messages** (prints, errors, hints) are friendly and
  conversational, may use contractions, and should tell the user what to do
  next (e.g. "use --tz").
- Private helpers are prefixed with `_`. Module-level regexes are compiled
  constants named `_SOMETHING_RE`.
- Keep the `--shift`/`--tz` argv rewrite in `main()`: it is what makes negative
  values like `--tz -8` parse correctly.

## Verifying changes

There is no test suite yet. At minimum:

```sh
python3 -m py_compile gpxtag.py
python3 gpxtag.py --help
```

For behavioural changes, run against a small GPX file and **copies** of some
photos, always with `--dry-run` first:

```sh
python3 gpxtag.py sample.gpx /path/to/photo-copies --dry-run
```

Never run without `--dry-run` against a user's real photo library; without
`--backup` exiftool modifies files in place.

Pure functions (`parse_iso_time`, `parse_shift`, `resolve_tz`,
`photo_timestamp`, `photo_offset`, `Track.locate`, `fmt_dur`, `fmt_shift`) can
be exercised directly by importing the module, since `main()` only runs under
`if __name__ == "__main__"`. If you add tests, use the standard library
`unittest` and keep them out of `gpxtag.py`.

## Docs to keep in sync

When changing CLI options, defaults, output, or exit codes, update:

- the `argparse` help strings in `main()`
- the module docstring examples (they appear in `--help`)
- `README.md` (options table, behaviour, exit codes)
- `MIN_EXIFTOOL` ↔ the Requirements sections of the module docstring,
  `README.md` and `CONTRIBUTING.md`
