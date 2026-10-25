# Nano6G Uploader

A Linux desktop uploader for the **iPod Nano 6th generation** (firmware 1.70):
music, playlists, artwork, photos, URL downloads, and library checks. It writes
directly to the device's SQLite databases, native artwork stores, and the
Nano-compatible signing library.

## Install

Clone into `~/ipod-nano6g-fix` (the install scripts use that path) and run the
one-time installer:

```sh
git clone https://github.com/LemionLaLemon/ipod-nano6-uploader.git ~/ipod-nano6g-fix
sudo bash ~/ipod-nano6g-fix/install.sh
```

Then launch `python3 ipod_gui_qt.py` for the Qt interface (the rofi entry
**Nano Gen 6 Uploader** runs this), or `python3 ipod_gui.py` for the smaller
Tk fallback.

Requires Python, PySide6 (Qt interface) or Tk, Pillow, ffmpeg/ffprobe, yt-dlp,
udisksctl, the bundled photo helpers, and the installed Nano-compatible
`/usr/lib/libgpod/libhashab.so`. `install.sh` installs the signing libraries from
`nano6g-libs/` and re-applies them through a pacman hook after libgpod upgrades;
`restore-stock.sh` reverts the change. The setup was verified against a connected
Nano 6G running firmware 1.70.

## Music

- Import MP3, M4A, MP4 audio, AAC, WAV, AIFF/AIF, FLAC, Ogg/OGA, Opus, WMA,
  APE, and WavPack/WV. Drag and drop also accepts audio and JPEG/PNG photos.
- Local files retain available title, artist, album, album artist, genre, year,
  track/disc numbers, and embedded covers. Filenames supply missing titles.
  Optional batch artist/album fields override those tags; blank fields retain them.
- Compatible MP3 and AAC-LC audio is preserved. Compatible 16-bit ALAC is
  preserved. Unsupported audio is converted to 16-bit ALAC, stereo or mono,
  at a supported sample rate up to 48 kHz. MP4 imports extract the audio stream.
- Playback sample rate, channel count, sample count, and duration come from the
  final device file. Conversion output is validated before registration.
- Embedded covers populate the Nano's native ArtworkDB and its six thumbnail
  caches. The initialized ArtworkDB layout and Nano RGB565 thumbnail formats
  are required. Existing artwork records and pixels are preserved.
- Edit title, artist, album, and an existing genre. The editor retains the
  current genre and unchanged custom sort tags. Artist, album, and genre browse
  flags, counts, representatives, and sort orders are refreshed after changes.
- Delete track removes its audio, database/state rows, native artwork record,
  and membership in both SQLite and on-device playlist files.

## URL downloads

Paste one HTTP(S) URL per line in **Download URL…**, then choose a playlist or
**Library only**. Each URL imports one item; playlist-only URLs use their first
item. yt-dlp downloads the best available source audio and its video thumbnail.
User yt-dlp configuration is ignored to keep output paths and conversion stable.

Compatible AAC-LC is preserved; other downloaded audio becomes 16-bit ALAC.
The thumbnail is converted to JPEG and verified in the resulting M4A, then
registered in native device artwork. Missing thumbnails fail before import.
Downloads and conversion run in the background. Temporary sources are removed.
Cancellation stops downloading/conversion and skips remaining jobs; a device
write already in progress finishes. Close and eject are blocked during writes.

## Playlists and photos

Create playlists, add library songs, import into a chosen playlist, or delete a
playlist while keeping its songs. Playlists created/edited on the Nano are read
from their `Playlist_<ID>.plist` files. Edits update SQLite and these files while
preserving existing order and intentional repeated songs. Protection of the
primary library uses its persistent ID, including after a rename.

The Qt Photos tab lists thumbnails and supports image import and photo deletion.
Photo helpers run against a staged copy; successful thumbnail/database changes
are published together. Failed helpers leave device photo data unchanged.
Helpers resolve beside the running application, and previews use a private cache.

Per-playlist song removal/reordering is not exposed in this UI; device-side
playlist edits are respected. Music registration uses the Nano's SQLite databases;
this application does not regenerate the legacy iTunesCDB for syncing with iTunes.

## Checks, repair, and backups

**Check library** reports database integrity, file/state inconsistencies,
playlist synchronization, genre flags, redundant browse groups, native artwork
references, and the Locations checksum. A checksum match does not independently
authenticate the device-specific HASHAB signature or prove firmware display.

**Repair library** backs up metadata, synchronizes device playlist edits,
restores missing playback-state rows, updates file sizes and browse indexes,
merges redundant artist/album groups, regenerates signing, and recovers native
covers from saved files. Existing play counts and song tags are preserved.
Backups go to `~/.local/share/nano6g-uploader/backups/`.

A song already committed when artwork, signing, or playlist publication fails
is reported as saved with a warning. Use Repair library rather than importing
that song again. Persistent ID reservations prevent deleted track IDs from
being assigned to future imports. Writes are coordinated across app instances.

Choose **Eject** before unplugging. Unmount errors are reported without claiming
success. An eject target must be the actual iPod mount rather than a parent disk.

For the read-only report from a terminal:

```sh
python3 diagnose_ipod.py "/run/media/lemion/IPOD NANOGE"
```

For regression tests:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Qt tests run offscreen; Tk smoke tests use a private Xvfb display when available.
`audit_device_copy.py` performs destructive integration checks only within a
local audit backup under `~/ipod-repair-backups/`; it refuses a physical mount.
Hardware cover/photo appearance and physical playback still need a device check.

## Storage footer

The Qt interface shows a segmented storage bar at the bottom of every tab, with
allocated sizes and capacity percentages for Music, Photos, Other, and Free.
Music includes native album-art caches; Photos includes thumbnails and stored
originals. Other includes databases, system files, trash, and allocation overhead.
GB uses decimal units and capacity comes from the actual mounted volume.
Scans run in the background, refresh after imports/deletions and every 30 seconds,
and clear on eject/disconnect. Stale scan results are discarded.
