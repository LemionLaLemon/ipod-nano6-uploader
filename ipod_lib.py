#!/usr/bin/env python3
"""Backend for Nano6G uploader. Proven against FW 1.70 / DBVersion 5.
Music: Library.itdb + Locations.itdb + Locations.itdb.cbk (HASHAB via libhashab).
Playlist: container + item_to_container + Dynamic.itdb container_ui.
Photo: delegated to photo_add binary (libgpod photodb)."""
import os, sqlite3, hashlib, ctypes, time, shutil, subprocess, pwd
from audio_compat import RATES, stream_info, validate_audio
from pathlib import Path
from device_io import device_write, database, FileChanges, music_path

LIBHASHAB = "/usr/lib/libgpod/libhashab.so"
UUID_HEX = "000A270022AE5850"
RND = b"ABCDEFGHIJKLMNOPQRSTUVW"

class ImportCommittedError(RuntimeError):
    def __init__(self, pid, relative, phase, error):
        self.pid, self.relative, self.phase = pid, relative, phase
        super().__init__(f"Track {pid} was saved, but device {phase} failed: {error}. Repair the saved track; do not import it again.")


def device_uuid(mount):
    path = Path(mount) / 'iPod_Control/Device/SysInfo'
    if not path.is_file(): return UUID_HEX
    fields = dict(line.split(':', 1) for line in path.read_text(errors='replace').splitlines() if ':' in line)
    value = fields.get('FirewireGuid', '').strip().removeprefix('0x')
    if len(value) != 16:
        raise ValueError('The connected iPod has no valid FirewireGuid; signing stopped.')
    bytes.fromhex(value)
    return value


def _insert_row(con, table, values):
    columns = {r[1] for r in con.execute(f'pragma table_info({table})')}
    values = {key: val for key, val in values.items() if key in columns}
    names = ','.join(values)
    con.execute(f"insert into {table} ({names}) values ({','.join('?' for _ in values)})", tuple(values.values()))


def _insert_container(con, pid, name):
    order = (con.execute('select max(name_order) from container').fetchone()[0] or 0) + 100
    now = apple_now()
    _insert_row(con, 'container', dict(pid=pid, distinguished_kind=0, date_created=now,
        date_modified=now, name=name, name_order=order, parent_pid=0, media_kinds=1,
        workout_template_id=0, is_hidden=0, smart_is_folder=0, smart_is_genius=0, smart_enabled_only=0))


def device_playlists(mount):
    import plistlib
    result = []
    directory = Path(itlp(mount)).parent
    for path in sorted(directory.glob('Playlist_*.plist')):
        try:
            pid = int(path.stem.split('_', 1)[1], 16)
            if pid >= 2**63: pid -= 2**64
            with path.open('rb') as stream: data = plistlib.load(stream)
            if not isinstance(data, dict): raise ValueError('Expected a playlist dictionary')
            raw = data.get('playlistPersistentID', pid)
            if (raw & (2**64 - 1)) != (pid & (2**64 - 1)): raise ValueError('Playlist ID mismatch')
            members = data.get('trackPersistentIDs', [])
            if not isinstance(members, list) or not all(isinstance(p, int) for p in members):
                raise ValueError('Invalid playlist track IDs')
            data['trackPersistentIDs'] = [p - 2**64 if p >= 2**63 else p for p in members]
            result.append((pid, str(path), data))
        except Exception as exc:
            raise ValueError(f'Cannot read device playlist {path.name}: {exc}') from exc
    return result


def _ensure_device_container(con, mount, pid):
    if con.execute('select 1 from container where pid=?', (pid,)).fetchone(): return
    _, data = _device_playlist(mount, pid)
    if data is None or data.get('playlistDeleted'):
        raise ValueError('Playlist no longer exists. Refresh and try again.')
    _insert_container(con, pid, data.get('name') or 'Untitled playlist')


def primary_container(mount):
    from contextlib import closing
    with closing(sqlite3.connect(f'file:{itlp(mount)}/Library.itdb?mode=ro', uri=True)) as con:
        return con.execute('select primary_container_pid from db_info').fetchone()[0]


def audio_metadata(path):
    from yt_download import probe
    info = probe(path)
    audio = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if audio is None: raise ValueError('File has no audio stream.')
    tags = {str(k).lower(): v for k, v in {**info.get('format', {}).get('tags', {}), **audio.get('tags', {})}.items()}
    return dict(title=tags.get('title') or Path(path).stem, artist=tags.get('artist') or 'Unknown',
                album=tags.get('album') or None, genre=tags.get('genre'))


def _group(con, table, name, artist_pid=None):
    if table == 'album':
        row = con.execute('select pid from album where name=? and artist_pid=? order by pid limit 1', (name, artist_pid)).fetchone()
    else:
        row = con.execute(f'select pid from {table} where name=? order by pid limit 1', (name,)).fetchone()
    if row: return row[0]
    pid = (con.execute(f'select max(pid) from {table}').fetchone()[0] or 0) + 1
    values = dict(pid=pid, kind=2, artwork_status=0, artwork_album_pid=0, artwork_item_pid=0,
                  name=name, sort_name=name, name_order=0, is_unknown=0, has_songs=0,
                  has_music_videos=0, has_non_compilation_tracks=0, album_count=0,
                  artist_pid=artist_pid, user_rating=0, all_compilations=0, season_number=0,
                  sort_order=0, artist_order=0, has_any_compilations=0, artist_count_calc=0,
                  has_movies=0, item_count=0)
    _insert_row(con, table, values)
    return pid


def _genre(con, name):
    name = (name or 'Music').strip() or 'Music'
    row = con.execute('select id from genre_map where genre=?', (name,)).fetchone()
    if row: return row[0]
    gid = (con.execute('select max(id) from genre_map').fetchone()[0] or 0) + 1
    _insert_row(con, 'genre_map', dict(id=gid, genre=name, genre_order=0, is_unknown=0,
                                    has_music=0, artist_count_calc=0, album_count_calc=0, compilation_count_calc=0))
    return gid


def _refresh_browse(con):
    """Recompute browse state after imports, edits, or deletion; keep artwork links."""
    item_columns = {r[1] for r in con.execute('pragma table_info(item)')}
    group_orders={}
    for field,order,table in [('sort_title','title_order',None),('sort_artist','artist_order','track_artist'),
                             ('sort_album','album_order','album'),('sort_album_artist','album_artist_order','artist')]:
        keys={r[0] for r in con.execute(f'select distinct {field} from item where {field} is not null')}
        if table:
            keys.update(r[0] for r in con.execute(f'select coalesce(sort_name,name) from {table}') if r[0] is not None)
        ranks={value:rank*100 for rank,value in enumerate(sorted(keys,key=lambda v:(str(v).casefold(),str(v))),1)}
        for value,rank in ranks.items():con.execute(f'update item set {order}=? where {field}=?',(rank,value))
        if table:group_orders[table]=ranks
    pairs=con.execute("select distinct coalesce(sort_album_artist,''),coalesce(sort_album,'') from item").fetchall()
    for rank,(artist,album) in enumerate(sorted(pairs,key=lambda r:(r[0].casefold(),r[1].casefold(),r)),1):
        con.execute("update item set album_by_artist_order=? where coalesce(sort_album_artist,'')=? and coalesce(sort_album,'')=?",(rank*100,artist,album))
    for table, relation in [('artist','artist_pid'),('track_artist','track_artist_pid'),('album','album_pid')]:
        columns = {r[1] for r in con.execute(f'pragma table_info({table})')}
        for flag in ['has_songs','has_music_videos','has_movies']:
            source = {'has_songs':'is_song','has_music_videos':'is_music_video','has_movies':'is_movie'}[flag]
            if flag in columns and source in item_columns:
                con.execute(f'update {table} set {flag}=exists(select 1 from item where {relation}={table}.pid and {source}=1)')
        if 'item_count' in columns:
            con.execute(f'update {table} set item_count=(select count(*) from item where {relation}={table}.pid)')
        if 'has_non_compilation_tracks' in columns:
            con.execute(f'update {table} set has_non_compilation_tracks=exists(select 1 from item where {relation}={table}.pid and coalesce(is_compilation,0)=0 and (media_kind & 33))')
        if 'album_count' in columns:
            con.execute(f'update {table} set album_count=(select count(distinct album_pid) from item where {relation}={table}.pid)')
        rows = con.execute(f'select pid,coalesce(sort_name,name) from {table}').fetchall()
        ranks=group_orders[table]
        con.executemany(f'update {table} set name_order=? where pid=?',[(ranks.get(name,0),pid) for pid,name in rows])
    con.execute('''update album set artist_count_calc=(select count(distinct track_artist_pid) from item where album_pid=album.pid),
        has_any_compilations=exists(select 1 from item where album_pid=album.pid and is_compilation=1),
        all_compilations=case when exists(select 1 from item where album_pid=album.pid) and
            not exists(select 1 from item where album_pid=album.pid and coalesce(is_compilation,0)=0) then 1 else 0 end,
        sort_order=coalesce((select album_order from item where album_pid=album.pid limit 1),name_order),
        artist_order=coalesce((select name_order from artist where artist.pid=album.artist_pid),0)''')
    if 'artwork_cache_id' in item_columns:
        con.execute("""update album set artwork_item_pid=coalesce(
            (select pid from item where pid=album.artwork_item_pid and album_pid=album.pid and artwork_status=1 and artwork_cache_id<>0),
            (select pid from item where album_pid=album.pid and artwork_status=1 and artwork_cache_id<>0 order by pid limit 1),0)""")
        con.execute("update album set artwork_status=case when artwork_item_pid<>0 then 1 else 0 end")
        con.execute("""update artist set artwork_album_pid=coalesce(
            (select pid from album where pid=artist.artwork_album_pid and artist_pid=artist.pid and artwork_status=1),
            (select pid from album where artist_pid=artist.pid and artwork_status=1 order by pid limit 1),0)""")
        con.execute("update artist set artwork_status=case when artwork_album_pid<>0 then 1 else 0 end")
    _refresh_genres(con)


def _next_track_pid(con, mount):
    highest = max(0,con.execute('select max(pid) from item').fetchone()[0] or 0)
    art = Path(mount) / 'iPod_Control/Artwork/ArtworkDB'
    if art.exists():
        from nano_artwork import image_records
        import struct
        data = art.read_bytes()
        _, _, entries = image_records(data)
        for p, _ in entries:
            value = struct.unpack_from('<Q', data, p + 20)[0]
            if value < 2**63: highest = max(highest, value)
    import json
    from device_io import atomic_write
    state_path=Path(itlp(mount))/'.nano6g-uploader-state.json'
    if state_path.exists():
        try:
            state=json.loads(state_path.read_text())
            previous=state['last_track_pid']
            if type(previous) is not int or not 0<=previous<2**63:raise ValueError('Invalid track ID')
            highest=max(highest,previous)
        except Exception as exc:raise ValueError('Uploader ID state is damaged; restore its backup before importing.') from exc
    if highest >= 2**63 - 1: raise ValueError('Track ID limit reached.')
    pid=highest+1
    # Reserving an ID before commit intentionally leaves gaps after failed imports.
    # Deleted IDs must never be reused while the Nano may retain play-count caches.
    atomic_write(state_path,json.dumps(dict(version=1,last_track_pid=pid)).encode())
    return pid

def _mount_roots():
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    roots = [f"/run/media/{user}", f"/media/{user}", "/media", "/run/media"]
    for base in ("/run/media", "/media"):
        try:
            roots.extend(os.path.join(base, d) for d in os.listdir(base))
        except OSError:
            pass
    unique = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique

def _ipod_mount_in(roots):
    for base in roots:
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for d in entries:
            p = os.path.join(base, d)
            if os.path.isdir(os.path.join(p, "iPod_Control")):
                return p
    return None

def find_mount():
    import subprocess as sp
    found = _ipod_mount_in(_mount_roots())
    if found:
        return found
    # not mounted: find IPOD partition and mount it
    try:
        import json
        out = sp.run(["lsblk", "-J", "-o", "NAME,LABEL,TYPE"], capture_output=True, text=True, timeout=10).stdout
        devs = []
        def walk(nodes):
            for n in nodes:
                if n.get("label", "") and "IPOD" in n["label"].upper():
                    devs.append(n["name"])
                walk(n.get("children", []))
        walk(json.loads(out).get("blockdevices", []))
        for name in devs:
            dev = name if name.startswith("/dev/") else f"/dev/{name}"
            sp.run(["udisksctl", "mount", "-b", dev], capture_output=True, text=True, timeout=30)
        return _ipod_mount_in(_mount_roots())
    except Exception:
        pass
    return None

def itlp(mount):
    return os.path.join(mount, "iPod_Control", "iTunes", "iTunes Library.itlp")

def apple_now():
    return int(time.time()) - 978307200

def regen_cbk(locations_path):
    with open(locations_path, "rb") as stream: data = stream.read()
    n = (len(data) + 1023) // 1024
    sha1s = b"".join(hashlib.sha1(data[i*1024:(i+1)*1024]).digest() for i in range(n))
    final = hashlib.sha1(sha1s).digest()
    lib = ctypes.CDLL(LIBHASHAB)
    lib.calcHashAB.argtypes = [ctypes.c_char_p]*4
    lib.calcHashAB.restype = None
    location_path=Path(locations_path).resolve()
    root=location_path.parents[3] if len(location_path.parents)>3 else location_path.parent
    uuid = bytes.fromhex(device_uuid(root)) + b"\x00"*12
    out = ctypes.create_string_buffer(57)
    lib.calcHashAB(out, final, uuid, RND)
    import tempfile
    directory = os.path.dirname(locations_path)
    fd, temporary = tempfile.mkstemp(prefix=".locations-signature-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(out.raw) + final + sha1s)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, os.path.join(directory, "Locations.itdb.cbk"))
    finally:
        if os.path.exists(temporary): os.remove(temporary)
    return n

def next_ids(lib):
    con = sqlite3.connect(lib)
    mi = con.execute("select max(pid) from item").fetchone()[0] or 0
    ma = con.execute("select max(pid) from album").fetchone()[0] or 0
    mar = con.execute("select max(pid) from artist").fetchone()[0] or 0
    mt = con.execute("select max(pid) from track_artist").fetchone()[0] or 0
    mp = con.execute("select max(physical_order) from item").fetchone()[0] or 0
    mo = con.execute("select max(title_order),max(artist_order),max(album_order) from item").fetchone()
    con.close()
    return mi+1, ma+1, mar+1, mt+1, (mp or 0)+1, (mo[0] or 0)+100, (mo[1] or 0)+100, (mo[2] or 0)+100

# Nano 6G (FamilyID 17) audio support: MP3 CBR/VBR 8-320kbps, AAC/M4A 8-320kbps,
# ALAC, AIFF, WAV, Audible; sample rates 8-48kHz. Anything else -> ALAC <=48kHz.
COPY_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".aiff", ".aif", ".wav"}
ALAC_EXTS = {".flac", ".ogg", ".oga", ".opus", ".wma", ".ape", ".wv"}
EXT_FOURCC = {".mp3": 0x4D503320, ".m4a": 0x4D344120, ".mp4": 0x4D503420, ".aac": 0x4D344120}
AUDIO_FORMAT = {".mp3": 301, ".m4a": 502, ".mp4": 502, ".aac": 502}

def prepare_audio(src):
    """Probe the actual stream; return a verified MP3 or Nano-compatible M4A."""
    import tempfile
    from yt_download import probe, run_command
    info = probe(src)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio: raise ValueError("File has no audio stream.")
    codec = audio.get("codec_name")
    sr = int(audio.get("sample_rate") or 0)
    ch = int(audio.get("channels") or 0)
    bitrate = int(audio.get("bit_rate") or 0)
    bits = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0)
    if not sr or not ch: raise ValueError("Cannot determine audio sample rate/channels.")
    compatible = sr in RATES and 1 <= ch <= 2
    container = info.get("format", {}).get("format_name", "").split(",")
    if codec == "mp3" and compatible and 0 < bitrate <= 320000 and "mp3" in container:
        return src, ".mp3", False
    copy_audio = compatible and ((codec == "aac" and audio.get("profile") == "LC" and 0 < bitrate <= 320000)
                                  or (codec == "alac" and 0 < bits <= 16))
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    covers_only = all(s.get("disposition", {}).get("attached_pic") for s in video)
    if copy_audio and "mp4" in container and covers_only:
        return src, ".m4a", False
    fd, tmp = tempfile.mkstemp(suffix=".m4a"); os.close(fd)
    try:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-map_metadata", "0"]
        cover = next((s for s in video if s.get("disposition", {}).get("attached_pic")), None)
        if cover:
            cmd += ["-map", f"0:{cover['index']}", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "alac", "-sample_fmt", "s16p"]
        if not copy_audio:
            if sr not in RATES: cmd += ["-ar", "48000" if sr >= 48000 else "44100"]
            if ch > 2: cmd += ["-ac", "2"]
        tags={str(k).lower():v for k,v in {**info.get("format",{}).get("tags",{}),**audio.get("tags",{})}.items()}
        for key in ("title","artist","album","genre","date","track","disc","composer"):
            if tags.get(key):cmd += ["-metadata",f"{key}={tags[key]}"]
        album_artist=tags.get("album_artist") or tags.get("albumartist")
        if album_artist:cmd += ["-metadata",f"album_artist={album_artist}"]
        run_command(cmd + ["-movflags", "+faststart", tmp], timeout=600)
        validate_audio(probe(tmp),allow_mp3=False)
        return tmp, ".m4a", True
    except Exception:
        os.remove(tmp)
        raise

def next_location(mount, ext=".mp3"):
    mus = os.path.join(mount, "iPod_Control", "Music")
    existing = set()
    os.makedirs(mus, exist_ok=True)
    for d in os.listdir(mus):
        dd = os.path.join(mus, d)
        if os.path.isdir(dd):
            for f in os.listdir(dd):
                existing.add(f)
    i = 100000
    while True:
        name = f"W{i:07d}{ext}"
        if name not in existing:
            # use highest F dir, create F14+ as needed
            fdirs = sorted(d for d in os.listdir(mus) if d.startswith("F") and d[1:].isdigit() and os.path.isdir(os.path.join(mus, d)))
            if not fdirs: return f"F00/{name}", name
            return f"{fdirs[-1]}/{name}" if len(os.listdir(os.path.join(mus, fdirs[-1]))) < 100 else f"F{max(int(d[1:]) for d in fdirs)+1:02d}/{name}", name
        i += 1

def list_tracks(mount):
    con = sqlite3.connect(f"file:{itlp(mount)}/Library.itdb?mode=ro", uri=True)
    rows = con.execute("select pid,title,artist,album from item order by physical_order").fetchall()
    con.close()
    return rows

def list_playlists(mount):
    from contextlib import closing
    with closing(sqlite3.connect(f"file:{itlp(mount)}/Library.itdb?mode=ro", uri=True)) as con:
        rows = dict(con.execute("select pid,name from container order by name_order"))
    for pid, _, data in device_playlists(mount):
        if data.get("playlistDeleted"):
            rows.pop(pid, None)
        else:
            rows[pid] = data.get("name", rows.get(pid, "Untitled playlist"))
    return sorted(rows.items(), key=lambda pair: ((pair[1] or "").casefold(), pair[0]))

@device_write
def add_track(mount, src_mp3, title, artist, album, duration_s=None, bitrate=None, sample_rate=None):
    """Commit library, location and playback-state records together, then sign."""
    source, ext, temporary = prepare_audio(src_mp3)
    con = None
    destination = None
    committed = False
    try:
        # Fail before copying/writing if the signing library is unavailable.
        getattr(ctypes.CDLL(LIBHASHAB), "calcHashAB")
        device_uuid(mount)
        paths = {name: os.path.join(itlp(mount), name + ".itdb") for name in ("Library", "Locations", "Dynamic")}
        if not all(os.path.isfile(p) for p in paths.values()):
            raise RuntimeError("Missing iPod database. Detect the correct device before importing.")
        con = sqlite3.connect(paths["Library"])
        con.execute("ATTACH DATABASE ? AS locations", (paths["Locations"],))
        con.execute("ATTACH DATABASE ? AS dynamic", (paths["Dynamic"],))
        # Multi-file atomic commits require rollback journals, not WAL.
        for db in ("main", "locations", "dynamic"):
            mode = con.execute(f"PRAGMA {db}.journal_mode").fetchone()[0]
            if mode in ("wal", "off", "memory"):
                raise RuntimeError(f"Unsupported {db} journal mode {mode}; import stopped before writing.")
            con.execute(f"PRAGMA {db}.synchronous=FULL")
        con.execute("BEGIN IMMEDIATE")
        rel, _ = next_location(mount, ext)
        destination = os.path.join(mount, "iPod_Control", "Music", rel)
        pid = _insert_track(con, mount, source, ext, rel, title, artist, album)
        with open(destination, "rb") as copied: os.fsync(copied.fileno())
        con.commit()
        committed = True
        con.close(); con = None
        try: regen_cbk(paths["Locations"])
        except Exception as e:
            raise ImportCommittedError(pid, rel, "signing", e) from e
        try:
            from nano_artwork import import_embedded_cover
            import_embedded_cover(mount, pid, source)
        except Exception as e:
            raise ImportCommittedError(pid, rel, "artwork", e) from e
        return pid, rel
    finally:
        if con is not None:
            con.rollback(); con.close()
        if not committed and destination and os.path.isfile(destination): os.remove(destination)
        if temporary: os.remove(source)


def _insert_track(con, mount, src_mp3, ext, rel, title, artist, album):
    lib = os.path.join(itlp(mount), "Library.itdb")
    ip = _next_track_pid(con, mount)
    phys = (con.execute("select max(physical_order) from item").fetchone()[0] or 0) + 1
    tord = aord = alord = 0
    AFMT = AUDIO_FORMAT[ext]
    EXT4 = EXT_FOURCC[ext]
    try:
        fsize = os.path.getsize(src_mp3)
        now = apple_now()
        # Record the final stream, never guessed MP3 defaults for ALAC/WAV.
        from yt_download import probe
        info = probe(src_mp3)
        audio, rate, channels, duration_s, dur_samples = stream_info(info)
        validate_audio(info)
        sample_rate=float(rate)
        bitrate=int(int(audio.get("bit_rate") or info["format"].get("bit_rate") or 0)/1000)
        dur_ms=duration_s*1000
        mus = os.path.join(mount, "iPod_Control", "Music")
        os.makedirs(os.path.join(mus, os.path.dirname(rel)), exist_ok=True)
        shutil.copyfile(src_mp3, os.path.join(mus, rel))
    except Exception:
        raise
    tags = {str(k).lower():v for k,v in info.get("format", {}).get("tags", {}).items()}
    album_artist = tags.get("album_artist") or tags.get("albumartist") or artist
    arp = _group(con, "artist", album_artist)
    tp = _group(con, "track_artist", artist)
    ap = _group(con, "album", album, arp) if album else 0
    genre_id = _genre(con, tags.get("genre"))
    con.execute("""INSERT INTO item (pid,media_kind,is_song,date_modified,year,is_compilation,remember_bookmark,exclude_from_shuffle,
        artwork_status,artwork_cache_id,start_time_ms,stop_time_ms,total_time_ms,track_number,track_count,disc_number,disc_count,
        bpm,relative_volume,genre_id,album_pid,artist_pid,composer_pid,title,artist,album,album_artist,sort_title,sort_artist,
        sort_album,sort_album_artist,title_order,artist_order,album_order,genre_order,composer_order,album_artist_order,
        album_by_artist_order,series_name_order,track_artist_pid,physical_order,has_lyrics)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ip, 1, 1, now, 0, 0, 0, 0, 0, 0, 0.0, 0.0, dur_ms, 0, 0, 0, 0, 0, 0, genre_id, ap, arp, 0,
         title, artist, album, album_artist, title, artist, album, album_artist,
         tord, aord, alord, 100, 100, aord, aord, 100, tp, phys, 0))
    con.execute("INSERT INTO avformat_info VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ip, 0, AFMT, bitrate, int(audio["channels"]), float(sample_rate), dur_samples, 0, 0, 0, 0, 0, 0, 0))
    master = con.execute("select primary_container_pid from db_info").fetchone()[0]
    if not con.execute("select 1 from container where pid=?", (master,)).fetchone():
        raise RuntimeError("The primary iPod library container is missing.")
    mc = con.execute("select max(physical_order) from item_to_container where container_pid=?", (master,)).fetchone()[0]
    mc = -1 if mc is None else mc
    con.execute("INSERT INTO item_to_container VALUES(?,?,?,?)", (ip, master, mc+1, None))
    con.execute("""INSERT INTO locations.location
        (item_pid,sub_id,base_location_id,location_type,location,extension,kind_id,date_created,file_size)
        VALUES(?,?,?,?,?,?,?,?,?)""", (ip, 0, 1, 1179208773, rel, EXT4, 1, now, fsize))
    con.execute("""INSERT INTO dynamic.item_stats
        (item_pid,has_been_played,date_played,play_count_user,play_count_recent,date_skipped,
         skip_count_user,skip_count_recent,bookmark_time_ms,bookmark_time_ms_common,user_rating,user_rating_common)
        VALUES(?,0,0,0,0,0,0,0,0,0,0,0)""", (ip,))
    def number(value):
        try:return max(0,int(str(value).split("/")[0]))
        except (TypeError,ValueError):return 0
    def total(value):
        parts=str(value or "").split("/")
        return number(parts[1]) if len(parts)>1 else 0
    year=number(str(tags.get("date") or tags.get("year") or "")[:4])
    con.execute("update item set year=?,track_number=?,track_count=?,disc_number=?,disc_count=? where pid=?",
        (year,number(tags.get("track")),total(tags.get("track")),number(tags.get("disc")),total(tags.get("disc")),ip))
    _refresh_browse(con)
    return ip

@device_write
def create_playlist(mount, name):
    name = name.strip()
    if not name: raise ValueError("Playlist name cannot be blank.")
    with database(mount, ("Dynamic",)) as con:
        used = [r[0] for r in con.execute("select pid from container")]
        used += [pid for pid, _, _ in device_playlists(mount)]
        newpid = max(used + [0]) + 1
        if newpid >= 2**63: raise ValueError("Playlist ID limit reached.")
        _insert_container(con, newpid, name)
        con.execute("insert into dynamic.container_ui (container_pid,play_order,is_reversed,album_field_order,repeat_mode,shuffle_items,has_been_shuffled) values(?,0,0,1,0,0,0)", (newpid,))
    return newpid

def _device_playlist(mount, container_pid):
    for pid, path, data in device_playlists(mount):
        if pid == container_pid: return path, data
    path = os.path.join(os.path.dirname(itlp(mount)), f"Playlist_{container_pid & (2**64-1):016X}.plist")
    return path, None


def _write_device_playlist(path, data):
    import plistlib, tempfile
    fd, temporary = tempfile.mkstemp(prefix=".playlist-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(data, stream, fmt=plistlib.FMT_BINARY, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.remove(temporary)


def playlist_add(mount, container_pid, item_pid):
    return playlist_add_many(mount, container_pid, [item_pid])

@device_write
def playlist_add_many(mount, container_pid, item_pids):
    """Append to effective on-device membership; publish both lists together."""
    import plistlib
    with FileChanges() as files, database(mount,("Dynamic",)) as con:
        _ensure_device_container(con, mount, container_pid)
        con.execute("insert or ignore into dynamic.container_ui (container_pid) values(?)",(container_pid,))
        path, data = _device_playlist(mount, container_pid)
        if data is not None and data.get("playlistDeleted"):
            raise ValueError("Playlist was deleted on the iPod. Refresh and try again.")
        members = (list(data.get("trackPersistentIDs", [])) if data is not None else
                   [r[0] for r in con.execute("select item_pid from item_to_container where container_pid=? order by physical_order", (container_pid,))])
        existing = set(members)
        added = 0
        for pid in dict.fromkeys(item_pids):
            if not con.execute("select 1 from item where pid=?", (pid,)).fetchone():
                raise ValueError("A selected song no longer exists. Refresh and try again.")
            if pid not in existing:
                members.append(pid); existing.add(pid); added += 1
        con.execute("delete from item_to_container where container_pid=?", (container_pid,))
        con.executemany("insert into item_to_container values(?,?,?,?)", [(pid,container_pid,n,None) for n,pid in enumerate(members)])
        if data is not None:
            data["trackPersistentIDs"] = members
            files.write(path, plistlib.dumps(data, fmt=plistlib.FMT_BINARY, sort_keys=False))
    return added

@device_write
def playlist_delete(mount, container_pid):
    import plistlib
    with FileChanges() as files, database(mount, ("Dynamic",)) as con:
        master = con.execute("select primary_container_pid from db_info").fetchone()[0]
        if container_pid == master: raise ValueError("Cannot delete the primary iPod library.")
        _ensure_device_container(con, mount, container_pid)
        name = con.execute("select name from container where pid=?", (container_pid,)).fetchone()[0]
        path, data = _device_playlist(mount, container_pid)
        con.execute("delete from item_to_container where container_pid=?", (container_pid,))
        con.execute("delete from container where pid=?", (container_pid,))
        con.execute("delete from dynamic.container_ui where container_pid=?", (container_pid,))
        if data is not None:
            data["playlistDeleted"] = True
            files.write(path, plistlib.dumps(data, fmt=plistlib.FMT_BINARY, sort_keys=False))
    return name


def playlist_tracks(mount, container_pid):
    con = sqlite3.connect(f"file:{itlp(mount)}/Library.itdb?mode=ro", uri=True)
    rows = con.execute("""select c.item_pid,coalesce(i.title,'[Missing library entry]'),i.artist from item_to_container c
        left join item i on i.pid=c.item_pid where c.container_pid=? order by c.physical_order""",
        (container_pid,)).fetchall()
    path, data = _device_playlist(mount, container_pid)
    if data is not None:
        rows = []
        if not data.get("playlistDeleted"):
            for pid in data.get("trackPersistentIDs", []):
                track = con.execute("select pid,title,artist from item where pid=?", (pid,)).fetchone()
                rows.append(track or (pid, "[Missing library entry]", None))
    con.close()
    return rows

def _refresh_genres(con):
    """Maintain the Nano's browse flags/counts using its SysInfoExtended SQL."""
    con.execute("""UPDATE genre_map SET
        has_music = EXISTS(SELECT is_song FROM item WHERE item.genre_id=genre_map.id AND item.media_kind & 33),
        artist_count_calc = (SELECT COUNT(DISTINCT track_artist_pid) FROM item WHERE item.genre_id=genre_map.id AND item.media_kind & 33),
        album_count_calc = (SELECT COUNT(DISTINCT album_pid) FROM item WHERE item.genre_id=genre_map.id AND item.media_kind & 33),
        compilation_count_calc = (SELECT COUNT(DISTINCT album_pid) FROM item WHERE item.genre_id=genre_map.id AND item.media_kind & 33 AND item.is_compilation=1)
    """)
    genres = con.execute("select id from genre_map order by is_unknown, genre collate nocase, id").fetchall()
    con.executemany("update genre_map set genre_order=? where id=?",
                    [((order + 1) * 100, gid) for order, (gid,) in enumerate(genres)])
    con.execute("""update item set genre_order=coalesce(
        (select genre_order from genre_map where genre_map.id=item.genre_id), 0)""")


def track_genre(mount, pid):
    from contextlib import closing
    with closing(sqlite3.connect(f"file:{itlp(mount)}/Library.itdb?mode=ro", uri=True)) as con:
        row = con.execute("select g.genre from item i left join genre_map g on g.id=i.genre_id where i.pid=?", (pid,)).fetchone()
        if row is None: raise ValueError("Song no longer exists. Refresh and try again.")
        return row[0]


def list_genres(mount):
    con = sqlite3.connect(f"file:{itlp(mount)}/Library.itdb?mode=ro", uri=True)
    rows = [r[0] for r in con.execute("select genre from genre_map order by genre")]
    con.close()
    return rows

@device_write
def retitle_track(mount, pid, title, artist, album, genre):
    title = title.strip()
    if not title: raise ValueError("Song title cannot be blank.")
    artist = artist.strip() or "Unknown"
    album = album.strip() if album else None
    with database(mount) as con:
        cursor=con.execute("select * from item where pid=?",(pid,))
        original=cursor.fetchone()
        if original is None:raise ValueError("Song no longer exists. Refresh and try again.")
        old=dict(zip([c[0] for c in cursor.description],original))
        row=(old['artist'],old['album'],old['album_artist'],old['album_pid'])
        gid = con.execute("select id from genre_map where genre=?", (genre,)).fetchone()
        if not gid: raise ValueError(f"unknown genre {genre!r}")
        album_artist = (row[2] or artist) if row[0] == artist else artist
        arp = _group(con, "artist", album_artist)
        tp = _group(con, "track_artist", artist)
        ap = (row[3] if album == row[1] and album_artist == row[2] and row[3]
              else _group(con, "album", album, arp) if album else 0)
        con.execute("""update item set title=?,sort_title=?,artist=?,sort_artist=?,album=?,sort_album=?,
            album_artist=?,sort_album_artist=?,album_pid=?,artist_pid=?,track_artist_pid=?,genre_id=?,date_modified=?
            where pid=?""",
            (title,old.get('sort_title') if title==old['title'] else title,
             artist,old.get('sort_artist') if artist==old['artist'] else artist,
             album,old.get('sort_album') if album==old['album'] else album,
             album_artist,old.get('sort_album_artist') if album_artist==old['album_artist'] else album_artist,
             ap,arp,tp,gid[0],apple_now(),pid))
        _refresh_browse(con)


@device_write
def track_delete(mount, item_pid):
    """Delete library/state/location rows and effective playlist entries together."""
    import plistlib, uuid
    getattr(ctypes.CDLL(LIBHASHAB), 'calcHashAB')
    device_uuid(mount)
    moved = []
    committed = False
    try:
        with FileChanges() as files, database(mount, ('Locations','Dynamic')) as con:
            row = con.execute('select title from item where pid=?',(item_pid,)).fetchone()
            if row is None: raise ValueError('Song no longer exists. Refresh and try again.')
            title = row[0]
            locations = con.execute('''select b.path,l.location from locations.location l
                join locations.base_location b on b.id=l.base_location_id where l.item_pid=?''',(item_pid,)).fetchall()
            for base, rel in locations:
                path = music_path(mount, base, rel)
                if path.is_file() and path not in [p for p,_ in moved]:
                    temporary = path.with_name('.nano-delete-' + uuid.uuid4().hex)
                    os.replace(path, temporary); moved.append((path,temporary))
            for _, path, data in device_playlists(mount):
                old = data.get('trackPersistentIDs',[])
                new = [p for p in old if p != item_pid]
                if old != new:
                    data['trackPersistentIDs'] = new
                    files.write(path, plistlib.dumps(data,fmt=plistlib.FMT_BINARY,sort_keys=False))
            con.execute('delete from item_to_container where item_pid=?',(item_pid,))
            con.execute('delete from avformat_info where item_pid=?',(item_pid,))
            con.execute('delete from locations.location where item_pid=?',(item_pid,))
            con.execute('delete from dynamic.item_stats where item_pid=?',(item_pid,))
            con.execute('delete from item where pid=?',(item_pid,))
            artwork = Path(mount)/'iPod_Control/Artwork/ArtworkDB'
            if artwork.is_file():
                from nano_artwork import without_track
                data = artwork.read_bytes(); updated = without_track(data,item_pid)
                if updated != data: files.write(artwork,updated)
            _refresh_browse(con)
        committed = True
        try: regen_cbk(str(Path(itlp(mount))/'Locations.itdb'))
        except Exception as exc:
            raise RuntimeError(f'Track {item_pid} was deleted, but device signing failed: {exc}. Use Repair library before ejecting.') from exc
        return title
    finally:
        for path,temporary in moved:
            if committed: temporary.unlink(missing_ok=True)
            elif temporary.exists(): os.replace(temporary,path)


KANA_MAP = {
 "あ":"a","い":"i","う":"u","え":"e","お":"o",
 "か":"ka","き":"ki","く":"ku","け":"ke","こ":"ko",
 "さ":"sa","し":"shi","す":"su","せ":"se","そ":"so",
 "た":"ta","ち":"chi","つ":"tsu","て":"te","と":"to",
 "な":"na","に":"ni","ぬ":"nu","ね":"ne","の":"no",
 "は":"ha","ひ":"hi","ふ":"fu","へ":"he","ほ":"ho",
 "ま":"ma","み":"mi","む":"mu","め":"me","も":"mo",
 "や":"ya","ゆ":"yu","よ":"yo",
 "ら":"ra","り":"ri","る":"ru","れ":"re","ろ":"ro",
 "わ":"wa","を":"wo","ん":"n",
 "が":"ga","ぎ":"gi","ぐ":"gu","げ":"ge","ご":"go",
 "ざ":"za","じ":"ji","ず":"zu","ぜ":"ze","ぞ":"zo",
 "だ":"da","ぢ":"ji","づ":"zu","で":"de","ど":"do",
 "ば":"ba","び":"bi","ぶ":"bu","べ":"be","ぼ":"bo",
 "ぱ":"pa","ぴ":"pi","ぷ":"pu","ぺ":"pe","ぽ":"po",
 "きゃ":"kya","きゅ":"kyu","きょ":"kyo","しゃ":"sha","しゅ":"shu","しょ":"sho",
 "ちゃ":"cha","ちゅ":"chu","ちょ":"cho","にゃ":"nya","にゅ":"nyu","にょ":"nyo",
 "ひゃ":"hya","ひゅ":"hyu","ひょ":"hyo","みゃ":"mya","みゅ":"myu","みょ":"myo",
 "りゃ":"rya","りゅ":"ryu","りょ":"ryo","ぎゃ":"gya","ぎゅ":"gyu","ぎょ":"gyo",
 "じゃ":"ja","じゅ":"ju","じょ":"jo","びゃ":"bya","びゅ":"byu","びょ":"byo",
 "ぴゃ":"pya","ぴゅ":"pyu","ぴょ":"pyo","うぁ":"wa","ゔ":"vu",
}
def kana_fold(s):
    import unicodedata
    s = unicodedata.normalize("NFKC", s).lower()
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        # katakana -> hiragana
        o = ord(c)
        if 0x30A1 <= o <= 0x30F6:
            c = chr(o - 0x60)
        two = s[i:i+2]
        two_h = ""
        if len(two) == 2:
            a = two[0]; oa = ord(a)
            if 0x30A1 <= oa <= 0x30F6:
                a = chr(oa - 0x60)
            b = two[1]
            if 0x30A1 <= ord(b) <= 0x30F6: b = chr(ord(b) - 0x60)
            two_h = a + b
        if two_h in KANA_MAP:
            out.append(KANA_MAP[two_h]); i += 2; continue
        if c in KANA_MAP:
            out.append(KANA_MAP[c]); i += 1; continue
        out.append(s[i]); i += 1
    return "".join(out)

def match_query(text, query):
    if not query: return True
    q = query.strip().lower()
    t = (text or "").lower()
    if q in t: return True
    return q in kana_fold(t)

def _photo_helper(name, helper=None):
    path = Path(helper) if helper else Path(__file__).resolve().parent / name
    if not path.is_file(): raise RuntimeError(f'Missing photo helper: {path}')
    return str(path)


def _mutate_photos(mount, name, args, helper=None):
    """Run libgpod on a staged copy, publishing thumbnails before Photo Database."""
    import tempfile
    root = Path(mount)
    photos = root / 'Photos'
    device = root / 'iPod_Control/Device'
    if not device.is_dir(): raise ValueError('No iPod at this mount. Choose Detect first.')
    with tempfile.TemporaryDirectory(prefix='nano-photo-write-') as directory:
        stage = Path(directory)
        (stage/'iPod_Control').mkdir()
        shutil.copytree(device,stage/'iPod_Control/Device')
        if photos.exists(): shutil.copytree(photos,stage/'Photos')
        else: (stage/'Photos').mkdir()
        result = subprocess.run([_photo_helper(name,helper),str(stage),*map(str,args)],
                                capture_output=True,text=True,timeout=120)
        output = result.stdout + result.stderr
        if result.returncode: return result.returncode,output
        photo_db = stage/'Photos/Photo Database'
        if not photo_db.is_file(): raise RuntimeError('Photo helper reported success without a Photo Database.')
        files = sorted((stage/'Photos').rglob('*'))
        files = [p for p in files if p.is_file() and p != photo_db] + [photo_db]
        with FileChanges() as changes:
            for source in files:
                target = photos/source.relative_to(stage/'Photos')
                if target.exists() and source.read_bytes() == target.read_bytes(): continue
                target.parent.mkdir(parents=True,exist_ok=True)
                changes.write(target,source.read_bytes())
        return 0,output.replace('added id=0\n','')


@device_write
def add_photo(mount, image_path, helper=None):
    source = Path(image_path).resolve()
    if not source.is_file(): raise ValueError('Photo source file no longer exists.')
    return _mutate_photos(mount,'photo_add',[source],helper)


def list_photos(mount, helper=None, outdir=None):
    if outdir is None:
        key = hashlib.sha256(str(Path(mount).resolve()).encode()).hexdigest()[:16]
        outdir = str(Path.home()/'.cache/nano6g-uploader/previews'/f'{key}-{os.getpid()}')
    directory = Path(outdir);directory.mkdir(parents=True,exist_ok=True)
    for path in directory.glob('photo_*.png'): path.unlink()
    result = subprocess.run([_photo_helper('photo_list',helper),str(mount),str(directory)],
                            capture_output=True,text=True,timeout=60)
    photos=[]
    for line in result.stdout.splitlines():
        parts=line.split('\t')
        if parts and parts[0].strip().isdigit():
            pid=int(parts[0]);photos.append(dict(id=pid,album=parts[1] if len(parts)>1 else '',preview=str(directory/f'photo_{pid}.png')))
    code=result.returncode;output=result.stdout+result.stderr
    missing=[p['id'] for p in photos if not Path(p['preview']).is_file()]
    if not code and missing:
        code=1;output+='\nCannot render photo previews for IDs: '+', '.join(map(str,missing))
    return code,output,photos


@device_write
def delete_photo(mount, photo_id, helper=None):
    if not isinstance(photo_id,int) or photo_id<0:raise ValueError('Invalid photo ID.')
    return _mutate_photos(mount,'photo_del',[photo_id],helper)


@device_write
def eject_device(mount):
    root = Path(mount).resolve()
    if not (root/'iPod_Control').is_dir(): raise ValueError('No iPod at this mount.')
    result = subprocess.run(['findmnt','-n','--mountpoint',str(root),'-o','SOURCE'],capture_output=True,text=True,timeout=10)
    source = result.stdout.strip()
    if result.returncode or not source.startswith('/dev/') or '\n' in source:
        raise ValueError('This path is not a mounted iPod partition.')
    result = subprocess.run(['udisksctl','unmount','-b',source],capture_output=True,text=True,timeout=30)
    if result.returncode: raise RuntimeError(result.stderr or result.stdout)


def _merge_duplicate_groups(con):
    counts = {}
    for table, fields in [('artist',['name','sort_name','is_unknown']),
                          ('track_artist',['name','sort_name','is_unknown']),
                          ('album',['name','sort_name','artist_pid','is_unknown'])]:
        rows = con.execute(f"select pid,{','.join(fields)} from {table} order by pid").fetchall()
        canonical = {}; merged = 0
        for pid,*values in rows:
            key = tuple(values)
            if key not in canonical:
                canonical[key] = pid; continue
            target = canonical[key]
            field = {'artist':'artist_pid','track_artist':'track_artist_pid','album':'album_pid'}[table]
            con.execute(f'update item set {field}=? where {field}=?',(target,pid))
            if table=='artist':con.execute('update album set artist_pid=? where artist_pid=?',(target,pid))
            if table=='album':con.execute('update artist set artwork_album_pid=? where artwork_album_pid=?',(target,pid))
            con.execute(f'delete from {table} where pid=?',(pid,));merged+=1
        counts[table]=merged
    return counts


@device_write
def repair_library(mount, track_pids=None, merge_groups=True):
    """Repair derived state and saved artwork without importing duplicate songs."""
    import datetime, plistlib
    from contextlib import closing
    root=Path(mount);directory=Path(itlp(mount))
    for name in ('Library','Locations','Dynamic'):
        if not (directory/(name+'.itdb')).is_file():raise ValueError(f'Missing iPod database: {name}.itdb')
    device_uuid(mount)
    getattr(ctypes.CDLL(LIBHASHAB),'calcHashAB')
    backup=Path.home()/'.local/share/nano6g-uploader/backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    for name in ['Library','Locations','Dynamic']:
        path=directory/(name+'.itdb')
        with closing(sqlite3.connect(path)) as source,closing(sqlite3.connect(backup/path.name)) as target:
            source.backup(target)
    for _,path,_ in device_playlists(mount):shutil.copy2(path,backup/Path(path).name)
    state=directory/'.nano6g-uploader-state.json'
    if state.exists():shutil.copy2(state,backup/state.name)
    artwork=root/'iPod_Control/Artwork/ArtworkDB'
    if artwork.exists():shutil.copy2(artwork,backup/'ArtworkDB')
    report=dict(backup=str(backup),playlists=0,playback_records=0,file_sizes=0,covers=0,warnings=[])
    with FileChanges() as changes,database(mount,('Locations','Dynamic')) as con:
        master=con.execute('select primary_container_pid from db_info').fetchone()[0]
        tracks={r[0] for r in con.execute('select pid from item')}
        for pid,path,data in device_playlists(mount):
            if pid==master:raise ValueError('A device playlist file unexpectedly overrides the primary library.')
            if data.get('playlistDeleted'):
                con.execute('delete from item_to_container where container_pid=?',(pid,))
                con.execute('delete from container where pid=?',(pid,))
                con.execute('delete from dynamic.container_ui where container_pid=?',(pid,))
                continue
            _ensure_device_container(con,mount,pid)
            members=[p for p in data.get('trackPersistentIDs',[]) if p in tracks]
            if members != data.get('trackPersistentIDs',[]):
                data['trackPersistentIDs']=members
                changes.write(path,plistlib.dumps(data,fmt=plistlib.FMT_BINARY,sort_keys=False))
            con.execute('delete from item_to_container where container_pid=?',(pid,))
            con.executemany('insert into item_to_container values(?,?,?,?)',[(p,pid,n,None) for n,p in enumerate(members)])
            con.execute('insert or ignore into dynamic.container_ui (container_pid) values(?)',(pid,))
            report['playlists']+=1
        for (pid,) in con.execute('select pid from item where pid not in (select item_pid from dynamic.item_stats)').fetchall():
            con.execute('''insert into dynamic.item_stats(item_pid,has_been_played,date_played,play_count_user,play_count_recent,
                date_skipped,skip_count_user,skip_count_recent,bookmark_time_ms,bookmark_time_ms_common,user_rating,user_rating_common)
                values(?,0,0,0,0,0,0,0,0,0,0,0)''',(pid,));report['playback_records']+=1
        for pid,base,rel,size in con.execute('''select l.item_pid,b.path,l.location,l.file_size from locations.location l
            join locations.base_location b on b.id=l.base_location_id''').fetchall():
            path=music_path(mount,base,rel)
            if not path.is_file():report['warnings'].append(f'Missing audio file for track {pid}');continue
            if size!=path.stat().st_size:
                con.execute('update locations.location set file_size=? where item_pid=? and location=?',(path.stat().st_size,pid,rel));report['file_sizes']+=1
        if merge_groups:report['merged_groups']=_merge_duplicate_groups(con)
        _refresh_browse(con)
        targets = list(track_pids) if track_pids is not None else [r[0] for r in con.execute('select pid from item where artwork_status<>1 or artwork_cache_id=0')]
    regen_cbk(str(directory/'Locations.itdb'))
    from nano_artwork import import_embedded_cover
    with closing(sqlite3.connect(directory/'Locations.itdb')) as locations:
        for pid in targets:
            row=locations.execute('select b.path,l.location from location l join base_location b on b.id=l.base_location_id where l.item_pid=?',(pid,)).fetchone()
            if not row:continue
            path=music_path(mount,*row)
            if not path.exists():continue
            try:
                if import_embedded_cover(mount,pid,path) is not None:report['covers']+=1
            except Exception as exc:report['warnings'].append(f'Artwork for track {pid}: {exc}')
    return report
