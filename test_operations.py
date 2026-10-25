"""Failures and cross-feature invariants for real user operations."""
from contextlib import closing,ExitStack
from pathlib import Path
import hashlib
import os
import plistlib
import shutil
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import ipod_lib as lib
import device_io
from test_import_database import create_library
from yt_download import run_command,probe
import test_playlist_ui as ui


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg required')
class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.mount=Path(self.tmp.name)/'device';self.directory=create_library(self.mount)
        (self.mount/'iPod_Control/Device').mkdir()
        self.source=Path(self.tmp.name)/'source.wav'
        run_command(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.12:sample_rate=48000',str(self.source)])
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(lib.ctypes,'CDLL'))
        self.stack.enter_context(patch.object(lib,'regen_cbk'))
        self.pid,self.relative=lib.add_track(str(self.mount),str(self.source),'Song','Artist','Album')

    def connect(self):return sqlite3.connect(self.directory/'Library.itdb')

    def playlist(self):
        pid=lib.create_playlist(str(self.mount),'Test list')
        path=self.directory.parent/f'Playlist_{pid:016X}.plist'
        path.write_bytes(plistlib.dumps(dict(playlistPersistentID=pid,name='Test list',trackPersistentIDs=[self.pid,self.pid])))
        lib.playlist_add_many(str(self.mount),pid,[])
        return pid,path

    def test_deleted_highest_track_id_is_not_reused(self):
        lib.track_delete(str(self.mount),self.pid)
        pid,_=lib.add_track(str(self.mount),str(self.source),'New song','Artist','Album')
        self.assertGreater(pid,self.pid)

    def test_repeated_imports_reuse_groups(self):
        lib.add_track(str(self.mount),str(self.source),'Another','Artist','Album')
        with closing(self.connect()) as con:
            for table in ['artist','track_artist','album']:
                self.assertEqual(con.execute('select count(*) from '+table).fetchone()[0],1)
            self.assertEqual(con.execute('select item_count from album').fetchone()[0],2)

    def test_delete_updates_device_playlist_and_playback_state(self):
        playlist,path=self.playlist()
        lib.track_delete(str(self.mount),self.pid)
        self.assertEqual(plistlib.loads(path.read_bytes())['trackPersistentIDs'],[])
        self.assertFalse((self.mount/'iPod_Control/Music'/self.relative).exists())
        with closing(sqlite3.connect(self.directory/'Dynamic.itdb')) as con:
            self.assertEqual(con.execute('select count(*) from item_stats').fetchone()[0],0)
        with closing(self.connect()) as con:
            self.assertEqual(con.execute('select has_songs,item_count from album').fetchone(),(0,0))

    def test_failed_delete_restores_audio_playlist_and_all_databases(self):
        playlist,path=self.playlist();before=path.read_bytes()
        with closing(sqlite3.connect(self.directory/'Dynamic.itdb')) as con:
            con.execute("create trigger reject_delete before delete on item_stats begin select raise(abort,'rejected');end")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'rejected'):
            lib.track_delete(str(self.mount),self.pid)
        self.assertTrue((self.mount/'iPod_Control/Music'/self.relative).is_file())
        self.assertEqual(path.read_bytes(),before)
        with closing(self.connect()) as con:self.assertIsNotNone(con.execute('select 1 from item where pid=?',(self.pid,)).fetchone())
        with closing(sqlite3.connect(self.directory/'Locations.itdb')) as con:
            self.assertIsNotNone(con.execute('select 1 from location where item_pid=?',(self.pid,)).fetchone())

    def test_deleted_playlist_id_is_not_reused(self):
        playlist,path=self.playlist();lib.playlist_delete(str(self.mount),playlist)
        new=lib.create_playlist(str(self.mount),'Replacement')
        self.assertGreater(new,playlist)
        self.assertIn(new,dict(lib.list_playlists(str(self.mount))))
        self.assertNotIn(playlist,dict(lib.list_playlists(str(self.mount))))

    def test_device_only_playlist_becomes_editable(self):
        pid=999;path=self.directory.parent/f'Playlist_{pid:016X}.plist'
        path.write_bytes(plistlib.dumps(dict(playlistPersistentID=pid,name='From Nano',trackPersistentIDs=[])))
        self.assertIn(pid,dict(lib.list_playlists(str(self.mount))))
        self.assertEqual(lib.playlist_add_many(str(self.mount),pid,[self.pid]),1)
        with closing(sqlite3.connect(self.directory/'Dynamic.itdb')) as con:
            self.assertEqual(con.execute('select container_pid from container_ui where container_pid=999').fetchone(),(999,))

    def test_renamed_master_cannot_be_deleted(self):
        with self.assertRaisesRegex(ValueError,'primary'):
            lib.playlist_delete(str(self.mount),10)

    def test_failed_playlist_publication_rolls_back_membership(self):
        playlist,path=self.playlist()
        other,_=lib.add_track(str(self.mount),str(self.source),'Other','Artist','Album')
        with patch('device_io.atomic_write',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):lib.playlist_add_many(str(self.mount),playlist,[other])
        with closing(self.connect()) as con:
            self.assertEqual(con.execute('select item_pid from item_to_container where container_pid=? order by physical_order',(playlist,)).fetchall(),[(self.pid,),(self.pid,)])

    def test_path_outside_music_is_never_deleted(self):
        outside=Path(self.tmp.name)/'outside';outside.write_text('keep')
        with closing(sqlite3.connect(self.directory/'Locations.itdb')) as con:
            con.execute("update location set location='../../../../outside'");con.commit()
        with self.assertRaisesRegex(ValueError,'outside'):
            lib.track_delete(str(self.mount),self.pid)
        self.assertEqual(outside.read_text(),'keep')

    def test_stream_metadata_survives_opus_conversion(self):
        source=Path(self.tmp.name)/'tags.opus'
        run_command(['ffmpeg','-v','error','-i',str(self.source),'-c:a','libopus',
            '-metadata','genre=Vocaloid','-metadata','album_artist=Album artist','-metadata','track=3/9',str(source)])
        pid,_=lib.add_track(str(self.mount),str(source),'Opus song','Singer','Album')
        with closing(self.connect()) as con:
            self.assertEqual(con.execute('select album_artist,track_number,track_count from item where pid=?',(pid,)).fetchone(),('Album artist',3,9))
        self.assertEqual(lib.track_genre(str(self.mount),pid),'Vocaloid')

    def test_photo_helper_failure_cannot_modify_device_files(self):
        photos=self.mount/'Photos';photos.mkdir();database=photos/'Photo Database';database.write_bytes(b'original')
        image=Path(self.tmp.name)/'photo.png';image.write_bytes(b'image')
        def fail(command,**kwargs):
            stage=Path(command[1]);(stage/'Photos/Photo Database').write_bytes(b'corrupted')
            return SimpleNamespace(returncode=1,stdout='',stderr='write rejected')
        with patch.object(lib.subprocess,'run',side_effect=fail):
            code,message=lib.add_photo(str(self.mount),image)
        self.assertEqual(code,1);self.assertIn('rejected',message)
        self.assertEqual(database.read_bytes(),b'original')

    def test_repair_rejects_incomplete_device_without_creating_empty_databases(self):
        invalid=Path(self.tmp.name)/'incomplete'
        directory=Path(lib.itlp(str(invalid)));directory.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'Missing iPod database'):
            lib.repair_library(str(invalid))
        self.assertEqual(list(directory.iterdir()),[])

    def test_repair_preserves_existing_play_counts(self):
        with closing(sqlite3.connect(self.directory/'Dynamic.itdb')) as con:
            con.execute('update item_stats set play_count_user=7');con.commit()
        # A stale byte count is repaired without replacing the user's audio.
        audio=self.mount/'iPod_Control/Music'/self.relative;before=audio.read_bytes()
        with closing(sqlite3.connect(self.directory/'Locations.itdb')) as con:
            con.execute('update location set file_size=1');con.commit()
        with patch.object(lib.Path,'home',return_value=Path(self.tmp.name)):
            report=lib.repair_library(str(self.mount))
        self.assertEqual(report['file_sizes'],1);self.assertEqual(audio.read_bytes(),before)
        with closing(sqlite3.connect(self.directory/'Dynamic.itdb')) as con:
            self.assertEqual(con.execute('select play_count_user from item_stats').fetchone()[0],7)


class CoordinationAndSearch(unittest.TestCase):
    def test_competing_write_is_rejected_but_nested_write_is_allowed(self):
        entered=threading.Event();release=threading.Event();errors=[]
        @device_io.device_write
        def inner(mount):return 'ok'
        @device_io.device_write
        def outer(mount):
            try:
                self.assertEqual(inner(mount),'ok');entered.set();release.wait(5)
            except Exception as e:errors.append(e)
        with tempfile.TemporaryDirectory() as mount:
            worker=threading.Thread(target=outer,args=(mount,));worker.start()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(RuntimeError,'Another transfer'):inner(mount)
            finally:release.set();worker.join(5)
        self.assertFalse(errors)

    def test_katakana_and_hiragana_digraph_search(self):
        for value in ['キャ','きゃ','ｷｬ']:
            self.assertTrue(lib.match_query(value,'kya'))
        self.assertTrue(lib.match_query('シャ','sha'))
        self.assertTrue(lib.match_query('Vocaloid','  vocaloid  '))

    def test_eject_requires_exact_mount_and_reports_unmount_failure(self):
        with tempfile.TemporaryDirectory() as mount:
            (Path(mount)/'iPod_Control').mkdir()
            with patch.object(lib.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='')) as run:
                with self.assertRaisesRegex(ValueError,'not a mounted'):lib.eject_device(mount)
                self.assertEqual(run.call_count,1)
                self.assertIn('--mountpoint',run.call_args.args[0])
            results=[SimpleNamespace(returncode=0,stdout='/dev/sda1\n'),SimpleNamespace(returncode=1,stdout='',stderr='busy')]
            with patch.object(lib.subprocess,'run',side_effect=results):
                with self.assertRaisesRegex(RuntimeError,'busy'):lib.eject_device(mount)

    def test_signing_hashes_partial_last_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'Locations.itdb';data=b'x'*1500;path.write_bytes(data)
            with patch.object(lib.ctypes,'CDLL'):
                self.assertEqual(lib.regen_cbk(str(path)),2)
            chunks=hashlib.sha1(data[:1024]).digest()+hashlib.sha1(data[1024:]).digest()
            self.assertEqual(path.with_suffix('.itdb.cbk').read_bytes()[57:],hashlib.sha1(chunks).digest()+chunks)


@unittest.skipIf(ui.gui is None,'PySide6 required')
class UIRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=ui.QApplication.instance() or ui.QApplication([])

    def window(self,mount,stack):
        for name,value in [('find_mount',mount),('list_tracks',[(1,'Song','Artist','Album')]),
             ('list_playlists',[(10,'Renamed primary'),(20,'iPod')]),('primary_container',10),('list_photos',(0,'',[])),('playlist_tracks',[])]:
            stack.enter_context(patch.object(lib,name,return_value=value))
        return ui.gui.Main()

    def test_custom_playlist_named_ipod_is_editable_and_renamed_master_is_excluded(self):
        with tempfile.TemporaryDirectory() as mount,ExitStack() as stack:
            (Path(mount)/'iPod_Control').mkdir();window=self.window(mount,stack)
            self.assertEqual(window.pl_combo.count(),1)
            self.assertEqual(window.pl_combo.currentData(),20)
            window.pl_list.setCurrentRow(0);self.assertFalse(window.pl_add_button.isEnabled())
            window.pl_list.setCurrentRow(1);self.assertTrue(window.pl_add_button.isEnabled());window.close()

    def test_committed_import_warning_counts_as_saved_and_adds_playlist(self):
        with tempfile.TemporaryDirectory() as mount,ExitStack() as stack:
            (Path(mount)/'iPod_Control').mkdir();window=self.window(mount,stack)
            stack.enter_context(patch.object(lib,'audio_metadata',return_value=dict(title='Tagged title',artist='Artist',album='Album')))
            add=stack.enter_context(patch.object(lib,'add_track',side_effect=lib.ImportCommittedError(5,'song.m4a','artwork','fixture')))
            playlist=stack.enter_context(patch.object(lib,'playlist_add'))
            window.run_upload_jobs([('mp3','song.flac',None,None)],20)
            self.assertIn('1 imported, 0 failed',window.statusBar().currentMessage())
            self.assertIn('1 warnings',window.statusBar().currentMessage())
            playlist.assert_called_once_with(mount,20,5);self.assertEqual(add.call_count,1);window.close()

    def test_cancel_finishes_current_write_skips_next_job_and_blocks_close_eject(self):
        import time
        from PySide6.QtWidgets import QProgressDialog,QPushButton
        from PySide6.QtGui import QCloseEvent
        entered=threading.Event();release=threading.Event();errors=[]
        with tempfile.TemporaryDirectory() as mount,ExitStack() as stack:
            (Path(mount)/'iPod_Control').mkdir();window=self.window(mount,stack)
            stack.enter_context(patch.object(lib,'audio_metadata',return_value=dict(title='Tagged title',artist='Artist',album='Album')))
            def write(*args):
                entered.set();release.wait(5);return 5,'song.m4a'
            add=stack.enter_context(patch.object(lib,'add_track',side_effect=write))
            eject=stack.enter_context(patch.object(lib,'eject_device'))
            def cancel_when_writing():
                if not entered.is_set():ui.QTimer.singleShot(5,cancel_when_writing);return
                try:
                    event=QCloseEvent();window.closeEvent(event);self.assertFalse(event.isAccepted())
                    window.eject();eject.assert_not_called()
                    dialog=next(d for d in window.findChildren(QProgressDialog) if d.isVisible())
                    next(b for b in dialog.findChildren(QPushButton) if b.text()=='Cancel').click()
                except Exception as exc:errors.append(exc)
                finally:release.set()
            ui.QTimer.singleShot(5,cancel_when_writing)
            window.run_upload_jobs([('mp3','one.flac',None,None),('mp3','two.flac',None,None)])
            deadline=time.monotonic()+5
            while (window._worker.isRunning() or not window.centralWidget().isEnabled()) and time.monotonic()<deadline:
                self.app.processEvents();time.sleep(.005)
            if errors:raise errors[0]
            self.assertEqual(add.call_count,1)
            self.assertIn('Import canceled: 1 imported, 0 failed, 1 skipped',window.statusBar().currentMessage())
            self.assertTrue(window.centralWidget().isEnabled());window.close()

    def test_failed_refresh_clears_stale_edit_targets(self):
        with tempfile.TemporaryDirectory() as mount,ExitStack() as stack:
            (Path(mount)/'iPod_Control').mkdir();window=self.window(mount,stack)
            with patch.object(lib,'list_tracks',side_effect=RuntimeError('unplugged')):window.refresh_all()
            self.assertEqual(window.music_table.rowCount(),0);self.assertEqual(window.tracks,[])
            self.assertFalse(window.tabs.isEnabled());window.close()


if __name__=='__main__':unittest.main()
