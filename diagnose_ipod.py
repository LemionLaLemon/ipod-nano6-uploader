#!/usr/bin/env python3
"""Read-only consistency report. Usage: python3 diagnose_ipod.py /path/to/iPod"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import ipod_lib as lib


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def diagnose(mount):
    mount = Path(mount).resolve()
    directory = Path(lib.itlp(str(mount)))
    report = {"mount": str(mount), "signing_uuid_configured_in_code": lib.UUID_HEX,
              "note": "SQLite visibility is not proof of firmware compatibility. No files were modified.",
              "files": {}, "playlists": [], "incomplete_tracks": []}
    for path in sorted(directory.parent.iterdir()):
        if path.is_file(): report["files"][path.name] = {"size": path.stat().st_size, "modified": path.stat().st_mtime}
    for path in sorted(directory.iterdir()):
        if path.is_file(): report["files"]["iTunes Library.itlp/" + path.name] = {"size": path.stat().st_size, "modified": path.stat().st_mtime}
    with closing(connect(directory / "Library.itdb")) as library, \
         closing(connect(directory / "Locations.itdb")) as locations, \
         closing(connect(directory / "Dynamic.itdb")) as dynamic:
        report["database_checks"] = {}
        for name, db in (("Library", library), ("Locations", locations), ("Dynamic", dynamic)):
            report["database_checks"][name] = {
                "quick_check": db.execute("pragma quick_check").fetchall(),
                "journal_mode": db.execute("pragma journal_mode").fetchone()[0],
                "tables": [r[0] for r in db.execute("select name from sqlite_master where type='table'")],
            }
        tracks = dict(library.execute("select pid,title from item"))
        report["library_tracks"] = len(tracks)
        stats = {r[0] for r in dynamic.execute("select item_pid from item_stats")}
        bases = dict(locations.execute("select id,path from base_location"))
        locs = {}
        for pid, base, rel, size in locations.execute("select item_pid,base_location_id,location,file_size from location"):
            locs.setdefault(pid, []).append((base, rel, size))
        for pid, title in tracks.items():
            issues = []
            if pid not in stats: issues.append("Missing Dynamic.item_stats row")
            if pid not in locs: issues.append("Missing Locations.location row")
            for base, rel, size in locs.get(pid, []):
                if base not in bases:
                    issues.append(f"Missing base_location {base}"); continue
                path = mount / str(bases[base]).lstrip("/") / str(rel)
                if not path.is_file(): issues.append(f"Missing audio file: {path}")
                elif size != path.stat().st_size: issues.append(f"File size mismatch: {path}")
            if issues: report["incomplete_tracks"].append({"pid": pid, "title": title, "issues": issues})
        for pid, name in library.execute("select pid,name from container"):
            members = [r[0] for r in library.execute("select item_pid from item_to_container where container_pid=? order by physical_order", (pid,))]
            device_path, device_data = lib._device_playlist(str(mount), pid)
            effective = (device_data.get("trackPersistentIDs", []) if device_data is not None else members)
            report["playlists"].append({"pid": pid, "name": name,
                "device_playlist_file": str(device_path) if device_data is not None else None,
                "device_playlist_deleted": bool(device_data and device_data.get("playlistDeleted")),
                "effective_membership_rows": len(effective),
                "device_membership_matches_database": effective == members,
                "membership_rows": len(members),
                "unique_tracks": len(set(members)), "existing_library_tracks": sum(p in tracks for p in members),
                "missing_track_ids": [p for p in members if p not in tracks]})
        report["duplicate_playlist_names"] = library.execute("select name,count(*) from container group by name having count(*)>1").fetchall()
        report["audio_format_counts"] = library.execute("select audio_format,count(*) from avformat_info group by audio_format").fetchall()
    data = (directory / "Locations.itdb").read_bytes()
    chunks = b"".join(hashlib.sha1(data[i:i+1024]).digest() for i in range(0, len(data), 1024))
    cbk_path = directory / "Locations.itdb.cbk"
    cbk = cbk_path.read_bytes() if cbk_path.is_file() else b""
    report["locations_checksum_matches_file"] = bool(cbk) and cbk[57:] == hashlib.sha1(chunks).digest() + chunks
    report["signature_note"] = "Checksum match does not verify the device-specific HASHAB signature or configured UUID."
    report['issues'] = []
    report['device_uuid'] = lib.device_uuid(mount)
    if any(v['quick_check'] != [('ok',)] for v in report['database_checks'].values()):
        report['issues'].append('A SQLite database failed its integrity check.')
    if report['incomplete_tracks']:
        report['issues'].append(f"{len(report['incomplete_tracks'])} tracks have missing state, audio, or stale file-size records.")
    if not report['locations_checksum_matches_file']:
        report['issues'].append('Locations checksum does not match; repair signing before ejecting.')
    pending = [p for p in report['playlists'] if not p['device_membership_matches_database'] and not p['device_playlist_deleted']]
    if pending:report['issues'].append(f'{len(pending)} on-device playlist changes need database synchronization.')
    dangling = sum(len(p['missing_track_ids']) for p in report['playlists'])
    if dangling:report['issues'].append(f'{dangling} playlist entries refer to missing tracks.')
    with closing(connect(directory/'Library.itdb')) as library:
        columns={r[1] for r in library.execute('pragma table_info(genre_map)')}
        if 'has_music' in columns:
            bad=library.execute('''select g.genre from genre_map g where coalesce(g.has_music,0) !=
                exists(select 1 from item i where i.genre_id=g.id and i.media_kind & 33)''').fetchall()
            report['hidden_or_stale_genres']=[r[0] for r in bad]
            if bad:report['issues'].append(f'{len(bad)} genre visibility flags are stale.')
        duplicate_groups={}
        for table,fields in [('artist','name,sort_name,is_unknown'),('track_artist','name,sort_name,is_unknown'),
                             ('album','name,sort_name,artist_pid,is_unknown')]:
            available={r[1] for r in library.execute(f'pragma table_info({table})')}
            if set(fields.split(',')) <= available:
                count=library.execute(f'select coalesce(sum(n-1),0) from (select count(*) n from {table} group by {fields} having count(*)>1)').fetchone()[0]
                duplicate_groups[table]=count
        report['duplicate_browse_groups']=duplicate_groups
        if sum(duplicate_groups.values()):report['issues'].append(f"{sum(duplicate_groups.values())} redundant artist/album records can be merged without changing tags.")
        item_columns={r[1] for r in library.execute('pragma table_info(item)')}
        if {'artwork_status','artwork_cache_id'} <= item_columns:
            artwork_path=mount/'iPod_Control/Artwork/ArtworkDB'
            links=library.execute('select pid,artwork_cache_id from item where artwork_status=1 and artwork_cache_id<>0').fetchall()
            report['artwork_records']=0
            try:
                from nano_artwork import image_records,thumbnails,u32
                import struct
                if artwork_path.exists():
                    data=artwork_path.read_bytes();_,_,entries=image_records(data)
                    report['artwork_records']=len(entries)
                    image_ids={u32(data,p+16) for p,n in entries}
                    invalid=[pid for pid,cache_id in links if cache_id not in image_ids]
                    if invalid:report['issues'].append(f'{len(invalid)} tracks refer to missing native artwork.')
                    sizes={p.name:p.stat().st_size for p in artwork_path.parent.glob('*.ithmb')}
                    broken=0
                    for p,n in entries:
                        image=data[p:p+n]
                        for node,fmt,name in thumbnails(image):
                            if u32(image,node+20)+u32(image,node+24)>sizes.get(name,0):broken+=1
                    if broken:report['issues'].append(f'{broken} native artwork thumbnail references are out of bounds.')
                elif links:report['issues'].append('Native ArtworkDB is missing.')
            except Exception as exc:report['issues'].append(f'Native artwork could not be checked: {exc}')
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount")
    args = parser.parse_args()
    print(json.dumps(diagnose(args.mount), indent=2))
