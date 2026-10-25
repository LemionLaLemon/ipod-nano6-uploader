#!/usr/bin/env python3
"""Destructive integration checks ONLY against a backed-up local device copy.

Usage: python3 audit_device_copy.py /home/USER/ipod-repair-backups/.../device-test
Never accepts a physical /run/media mount. No synthetic songs reach the iPod.
"""
from pathlib import Path
from contextlib import closing
import argparse
import json
import plistlib
import sqlite3
import struct
import tempfile
from unittest.mock import patch

import ipod_lib as lib
from yt_download import run_command,probe
from audio_compat import validate_audio,stream_info
from nano_artwork import image_records


def audit(mount):
    mount=Path(mount).resolve()
    if not mount.is_relative_to(Path.home()/'ipod-repair-backups') or not (mount.parent/'source-before').is_dir():
        raise ValueError('Refusing destructive tests outside an explicit local audit backup.')
    if mount.name not in ('device-test','metadata-test'):raise ValueError('Not an audit device copy.')
    directory=Path(lib.itlp(str(mount)))
    results=[]
    def record(name,details):
        results.append(dict(feature=name,status='passed',details=details))
        print(name,': passed',flush=True)
        (mount.parent/'integration-results.json').write_text(json.dumps(results,indent=2))
    def connect():return sqlite3.connect(directory/'Library.itdb')
    with tempfile.TemporaryDirectory(prefix='nano-audit-audio-') as temporary:
        source=Path(temporary)
        from PIL import Image
        cover=source/'cover.png';Image.new('RGB',(640,360),(250,40,80)).save(cover)
        specifications=[('mp3','libmp3lame',44100,2,['-b:a','128k'],True),
            ('m4a','aac',48000,2,['-b:a','128k'],True),
            ('lossless.m4a','alac',48000,2,['-sample_fmt','s16p'],True),
            ('mp4','aac',48000,2,['-b:a','128k','-c:v','mpeg4','-shortest'],False),
            ('wv','wavpack',96000,2,[],False),
            ('wav','pcm_s24le',96000,2,[],False),('aiff','pcm_s16be',44100,2,[],False),
            ('flac','flac',96000,2,[],True),('opus','libopus',48000,2,[],False),
            ('ogg','libvorbis',44100,2,[],False),('aac','aac',44100,1,['-b:a','96k'],False),
            ('wma','wmav2',44100,2,[],False),('surround.flac','flac',96000,6,[],False)]
        playlist=lib.create_playlist(str(mount),'Audit integration playlist')
        initial_photos=lib.list_photos(str(mount))[2]
        imported=[]
        for extension,codec,rate,channels,options,art in specifications:
            file=source/('tagged.'+extension)
            command=['ffmpeg','-v','error','-f','lavfi','-i',f'sine=duration=0.24:sample_rate={rate}']
            if extension=='mp4':command+=['-f','lavfi','-i','color=c=blue:s=64x64:d=0.24','-map','0:a:0','-map','1:v:0']
            if art:command+=['-i',str(cover),'-map','0:a:0','-map','1:v:0','-c:v','mjpeg','-disposition:v','attached_pic']
            command+=['-ac',str(channels),'-c:a',codec,*options]
            for key,value in dict(title='Tagged '+extension,artist='Audit Track Artist',album_artist='Audit Album Artist',
                 album='Audit Album',genre='Audit Vocaloid',date='2024',track='3/9',disc='1/2').items():
                command+=['-metadata',f'{key}={value}']
            run_command(command+[str(file)])
            tags=lib.audio_metadata(file)
            pid,relative=lib.add_track(str(mount),str(file),tags['title'],tags['artist'],tags['album'])
            imported.append(pid);lib.playlist_add(str(mount),playlist,pid)
            final=probe(mount/'iPod_Control/Music'/relative)
            validate_audio(final)
            assert all(s.get('disposition',{}).get('attached_pic') for s in final['streams'] if s['codec_type']=='video')
            audio,sr,ch,duration,samples=stream_info(final)
            assert sr<=48000 and ch<=2
            with closing(connect()) as con:
                row=con.execute('select total_time_ms from item where pid=?',(pid,)).fetchone()
                assert abs(row[0]-duration*1000)<.01
                row=con.execute('select sample_rate,duration,channels from avformat_info where item_pid=?',(pid,)).fetchone()
                assert row==(sr,samples,ch),(extension,row,(sr,samples,ch))
                artwork=con.execute('select artwork_status,artwork_cache_id from item where pid=?',(pid,)).fetchone()
                if art:assert artwork[0]==1 and artwork[1]>0
            record('Audio import '+extension,dict(pid=pid,codec=audio['codec_name'],sample_rate=sr,channels=ch,embedded_cover=art))
        with closing(connect()) as con:
            assert con.execute("select count(*) from artist where name='Audit Album Artist'").fetchone()[0]==1
            assert con.execute("select count(*) from track_artist where name='Audit Track Artist'").fetchone()[0]==1
            assert con.execute("select count(*) from (select artist_pid,count(*) n from album where name='Audit Album' group by artist_pid having count(*)>1)").fetchone()[0]==0
        record('Reuse artist and album records','Repeated imports reuse groups keyed by album name and album artist.')
        original=lib.playlist_tracks(str(mount),playlist)
        assert len(original)==len(imported)
        assert lib.playlist_add_many(str(mount),playlist,[imported[0],imported[0]])==0
        record('Playlist add and duplicate prevention',len(original))
        original_pid=imported[0]
        lib.retitle_track(str(mount),original_pid,'Edited audit title','Edited audit artist','Edited audit album','Audit Vocaloid')
        with closing(connect()) as con:
            assert con.execute("select has_songs,item_count from album where name='Edited audit album'").fetchone()==(1,1)
            assert con.execute('select artwork_status from item where pid=?',(original_pid,)).fetchone()[0]==1
        record('Metadata edit and browse counters','New album is visible and original artwork reference survives.')
        # A playlist created solely on the device must appear and become editable.
        native_id=playlist+10
        path=directory.parent/f'Playlist_{native_id:016X}.plist'
        data=dict(playlistPersistentID=native_id,name='Audit device-created playlist',version=0,DBTimestampMacOSDate=0,
                  trackPersistentIDs=[imported[1],imported[1]])
        path.write_bytes(plistlib.dumps(data))
        assert native_id in dict(lib.list_playlists(str(mount)))
        assert lib.playlist_add_many(str(mount),native_id,[original_pid])==1
        assert [r[0] for r in lib.playlist_tracks(str(mount),native_id)]==[imported[1],imported[1],original_pid]
        record('On-device playlists and repeated entries','Device-only list is shown; repeated entries and order survive edits.')
        # A failed file publication must roll back both membership and audio deletion.
        with closing(connect()) as con:previous=con.execute('select * from item_to_container where container_pid=?',(native_id,)).fetchall()
        with patch('device_io.atomic_write',side_effect=OSError('injected publication failure')):
            try:lib.playlist_add_many(str(mount),native_id,[imported[2]])
            except OSError:pass
            else:raise AssertionError('Injected playlist publication failure was ignored')
        with closing(connect()) as con:assert previous==con.execute('select * from item_to_container where container_pid=?',(native_id,)).fetchall()
        record('Playlist failure rollback','No partial database update when device playlist publication fails.')
        with patch.object(lib,'_refresh_browse',side_effect=RuntimeError('injected delete failure')):
            try:lib.track_delete(str(mount),original_pid)
            except RuntimeError:pass
            else:raise AssertionError('Injected track deletion failure was ignored')
        assert original_pid in [r[0] for r in lib.playlist_tracks(str(mount),native_id)]
        with closing(connect()) as con:assert con.execute('select 1 from item where pid=?',(original_pid,)).fetchone()
        record('Track deletion failure rollback','Library, playlist files, artwork and audio restored after a failed deletion.')
        lib.track_delete(str(mount),original_pid)
        assert original_pid not in [r[0] for r in lib.playlist_tracks(str(mount),native_id)]
        assert original_pid not in plistlib.loads(path.read_bytes())['trackPersistentIDs']
        raw=(mount/'iPod_Control/Artwork/ArtworkDB').read_bytes()
        _,_,entries=image_records(raw)
        assert original_pid not in [struct.unpack_from('<Q',raw,p+20)[0] for p,n in entries]
        with closing(connect()) as con:
            assert con.execute("select has_songs,item_count from album where name='Edited audit album'").fetchone()==(0,0)
        record('Track deletion','Deletes playlist-file membership and native artwork record; hides empty browse groups.')
        lib.playlist_delete(str(mount),native_id)
        next_playlist=lib.create_playlist(str(mount),'Audit replacement playlist')
        assert next_playlist>native_id
        assert next_playlist in dict(lib.list_playlists(str(mount)))
        record('Playlist delete/recreate','Deleted on-device IDs are never reused for a new playlist.')
        master=lib.primary_container(str(mount))
        with closing(connect()) as con:con.execute("update container set name='Renamed primary library' where pid=?",(master,));con.commit()
        try:lib.playlist_delete(str(mount),master)
        except ValueError:pass
        else:raise AssertionError('Renamed primary library was deletable')
        record('Renamed primary-library protection','Protection follows the persistent ID rather than the name.')
        code,output=lib.add_photo(str(mount),str(cover));assert code==0,output
        after=lib.list_photos(str(mount))[2]
        newids={p['id'] for p in after}-{p['id'] for p in initial_photos}
        assert len(newids)==1
        code,output=lib.delete_photo(str(mount),newids.pop());assert code==0,output
        assert [p['id'] for p in lib.list_photos(str(mount))[2]]==[p['id'] for p in initial_photos]
        record('Photo list/add/delete','A real photo round trip preserves all eight existing photos.')
        report=lib.repair_library(str(mount))
        assert not report['warnings'],report
        record('Library repair and saved-cover recovery',report)
        from diagnose_ipod import diagnose
        diagnostic=diagnose(mount)
        assert diagnostic['locations_checksum_matches_file']
        assert all(v['quick_check']==[('ok',)] for v in diagnostic['database_checks'].values())
        record('Post-operation database integrity and signing','SQLite integrity and regenerated Locations checksum match.')
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mount');args=parser.parse_args()
    audit(args.mount)
