# gpxtag

Geotag photos from one or more GPX track logs.

`gpxtag` reads the capture time of every photo, finds where you were at that
moment in your GPX track, and writes the GPS position into the photo's EXIF
data. Photos taken outside the track's time span, or during a long gap in the
track, are left alone.

It is a single Python script with no Python dependencies. All metadata reading
and writing is done by [exiftool](https://exiftool.org).

## Requirements

- Python 3.9 or newer (3.9+ is needed for place names in `--tz`; fixed offsets
  work on older versions)
- exiftool 10.28 or newer on your `PATH` (check with `exiftool -ver`). gpxtag
  checks this at startup.
  - macOS: `brew install exiftool`
  - Debian/Ubuntu: `apt install libimage-exiftool-perl`
- A time zone database for place names in `--tz`. Most systems have one; if
  not, `pip install tzdata`.

## Installation

Copy or symlink the script somewhere on your `PATH`:

```sh
chmod +x gpxtag.py
ln -s "$PWD/gpxtag.py" ~/.local/bin/gpxtag
```

Or run it directly with `python3 gpxtag.py ...`.

## Usage

```
gpxtag [options] TRACK.gpx [TRACK2.gpx ...] [PHOTO_DIR_OR_FILE ...]
```

Give one or more `.gpx` files, then the photo folders or files to tag. If you
give no photo paths, the current folder is used.

```sh
gpxtag track.gpx                              # photos in the current folder
gpxtag track.gpx ~/Photos/trip -r --dry-run   # preview, include subfolders
gpxtag day1.gpx day2.gpx ~/Photos/trip        # several tracks at once
gpxtag track.gpx --tz "los angeles"           # camera's stored offset is wrong/missing
gpxtag track.gpx --tz tokyo                   # any city in the time zone database
gpxtag track.gpx --tz -8                      # or a fixed UTC offset
gpxtag track.gpx --shift -0:01:30             # camera clock was 90 s fast
```

**Always start with `--dry-run`.** It prints what would be tagged, and where,
without writing anything.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-r`, `--recursive` | off | Include subfolders. |
| `--tz PLACE\|OFFSET` | from photo | Time zone the camera clock showed. Overrides the offset stored in every photo. A place (`"los angeles"`, `tokyo`, `America/Denver`) or an offset (`-8`, `+09:00`, `UTC-7`). Daylight saving is worked out per photo for places. |
| `--shift [-]H:MM[:SS]` | `0` | Amount to add to every photo time, to correct a camera clock that was simply wrong. Plain seconds (`90`) also work. |
| `--max-gap SEC` | `300` | Largest gap between two track points that will be interpolated across. |
| `--tolerance SEC` | `60` | Snap to the nearest track point if it is within this many seconds. Also lets photos slightly before or after the track match. |
| `--overwrite` | off | Replace GPS data on photos that already have it. All existing EXIF GPS tags are removed first, so nothing stale (e.g. an old altitude) is left behind. By default those photos are skipped. |
| `--ext EXT` | still images | Only process this extension. Repeatable (`--ext jpg --ext arw`). Without it, every file exiftool identifies as `image/*` is processed. |
| `-n`, `--dry-run` | off | Show what would be done; write nothing. |
| `--backup` | off | Keep exiftool's `FILE_original` backup copies. By default files are modified in place. |
| `--no-preserve-mtime` | off | Let file modification times change. By default they are preserved. |
| `-q`, `--quiet` | off | Only print the summary line (plus any errors). |

## How it works

### Time zones

GPX tracks are always recorded in UTC. Camera clocks usually show local time.
To compare the two, `gpxtag` needs to know each photo's UTC offset.

Modern cameras and phones store this offset in the photo (EXIF
`OffsetTimeOriginal`, `OffsetTimeDigitized` or `OffsetTime`), so normally
nothing needs to be configured. For each photo, the offset is chosen in this
order:

1. `--tz`, if given (applies to every photo in the run)
2. The offset stored in the photo, starting with the one that belongs to the
   date tag used (`OffsetTimeOriginal` for `DateTimeOriginal`,
   `OffsetTimeDigitized` for `CreateDate`)
3. A time zone written inside the date/time value itself

Fixed `--tz` offsets must be real ones, from `-12:00` to `+14:00`.

With a `--tz` place, a clock time can be ambiguous around a daylight-saving
change: the hour that repeats when clocks go back, or the hour that is skipped
when they go forward. `gpxtag` tries both readings. If only one fits the track
it uses that one; if both fit, it uses the first and flags the photo so you
can check it.

Photos with no offset from any of these are skipped, and `gpxtag` tells you to
use `--tz`.

The offset tags were added in **EXIF 2.31** (2016). Photos from older cameras,
or files saved by software that writes an older EXIF version, have no stored
offset and need `--tz`.

Use `--tz` when the stored offset is wrong or missing — for example, when you
forgot to change the camera's time zone after travelling. Use `--shift` when
the camera clock itself was wrong by some amount. The two can be combined.

### Finding a position

For each photo time, `gpxtag` tries, in order:

1. **exact** – a track point with exactly the same time.
2. **interp** – linear interpolation between the points just before and after,
   if both are in the same track segment and no more than `--max-gap` seconds
   apart. Tracks that cross the 180° meridian are handled.
3. **nearest** – the closest track point, if it is within `--tolerance`
   seconds.

Otherwise the photo is skipped and the reason is shown (before track start,
after track end, or in a track gap).

Only recorded track points (`<trkpt>` with a `<time>`) are used. Routes
(`<rtept>`), waypoints (`<wpt>`) and untimed points are counted and reported
but ignored, since they can't say when you were somewhere. Several GPX files
can be given at once; their points are merged, and interpolation never crosses
from one segment or file to another.

### What gets written

For each matched photo, `gpxtag` writes (after clearing old GPS tags, with
`--overwrite`) `GPSLatitude`/`Ref`,
`GPSLongitude`/`Ref`, `GPSDateStamp`, `GPSTimeStamp` (UTC), `GPSMapDatum`
(`WGS-84`), and — if the track has elevation — `GPSAltitude`/`Ref`. All photos
are written in a single exiftool run.

### Hints

If photos fall outside the track, `gpxtag` tries every offset from −14 h to
+14 h in 30-minute steps. If one of them would place at least half of the
skipped photos, it works out what the suggested `--tz` (or `--shift`) would do
to *every* photo, including those already matched:

- If more photos would match in total, it suggests that option.
- If not (for example, a few photos from a second camera with a wrong clock),
  it suggests tagging the skipped photos in a separate run instead.

This only detects photos that land *outside* the track. On a multi-day track,
a wrong time zone can place a photo *inside* the track at the wrong position,
so check the `--dry-run` output.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (including when some photos were skipped) |
| `1` | exiftool reported errors, did not update every photo, or a fatal error (e.g. unreadable GPX, unknown `--tz` place, exiftool missing or older than 10.28) |
| `2` | Bad command-line arguments, or none of the GPX files contained timestamped track points |
| `130` | Interrupted with Ctrl+C |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
