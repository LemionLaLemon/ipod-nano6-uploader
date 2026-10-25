"""Keep Nano genre browse flags and the metadata editor consistent."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ipod_lib as lib
from test_import_database import create_library
import test_playlist_ui as ui


class GenreMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        self.directory = create_library(self.mount)
        self.db = self.directory / 'Library.itdb'
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("insert into genre_map values(33,'Vocaloid',100,0,0,0,0,0)")
            con.execute("insert into artist (pid,name) values(1,'Artist')")
            con.execute("insert into track_artist (pid,name) values(1,'Artist')")
            con.execute("insert into album (pid,name) values(1,'Album')")
            for pid, genre, media, album, compilation in [(1,33,1,1,1),(2,1,1,2,0),(3,33,2,3,0)]:
                con.execute('''insert into item (pid,title,artist,genre_id,genre_order,media_kind,is_song,
                    artist_pid,track_artist_pid,album_pid,is_compilation) values(?,?,?,?,?,?,1,1,1,?,?)''',
                    (pid,str(pid),'Artist',genre,999,media,album,compilation))
            con.commit()

    def test_refresh_fixes_flags_counts_and_order_without_retagging_songs(self):
        with closing(sqlite3.connect(self.db)) as con:
            lib._refresh_genres(con)
            self.assertEqual(con.execute('select has_music,artist_count_calc,album_count_calc,compilation_count_calc from genre_map where id=33').fetchone(), (1,1,1,1))
            self.assertEqual(con.execute('select genre_id from item order by pid').fetchall(), [(33,),(1,),(33,)])
            self.assertEqual(con.execute('select count(*) from item i join genre_map g on i.genre_id=g.id where i.genre_order<>g.genre_order').fetchone(), (0,))

    def test_moving_last_music_track_hides_empty_genre_and_preserves_current_genre_lookup(self):
        self.assertEqual(lib.track_genre(str(self.mount),1),'Vocaloid')
        lib.retitle_track(str(self.mount),1,'Edited title','Artist','Album','Music')
        self.assertEqual(lib.track_genre(str(self.mount),1),'Music')
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('select has_music,artist_count_calc,album_count_calc from genre_map where id=33').fetchone(), (0,0,0))
            self.assertEqual(con.execute('select title from item where pid=1').fetchone(), ('Edited title',))

    def test_invalid_genre_leaves_metadata_unchanged(self):
        with self.assertRaises(ValueError):
            lib.retitle_track(str(self.mount),1,'Should not save','Artist',None,'Missing')
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('select title,genre_id from item where pid=1').fetchone(), ('1',33))
            con.execute('begin immediate')  # failed edit released its connection/lock


@unittest.skipIf(ui.gui is None, 'PySide6 required')
class GenreEditor(unittest.TestCase):
    def test_title_edit_starts_with_current_genre(self):
        app = ui.QApplication.instance() or ui.QApplication([])
        with tempfile.TemporaryDirectory() as mount:
            os.mkdir(os.path.join(mount,'iPod_Control'))
            with patch.object(lib,'find_mount',return_value=mount), \
                 patch.object(lib,'list_tracks',return_value=[(1,'Song','Artist','Album')]), \
                 patch.object(lib,'list_playlists',return_value=[]), \
                 patch.object(lib,'list_photos',return_value=(0,'',[])), \
                 patch.object(lib,'list_genres',return_value=['Jazz','Music','Vocaloid']), \
                 patch.object(lib,'track_genre',return_value='Vocaloid'), \
                 patch.object(lib,'retitle_track') as edit:
                window = ui.gui.Main();window.music_table.selectRow(0)
                errors=[]
                def interact():
                    dialog=app.activeModalWidget()
                    try:
                        combo=dialog.findChild(ui.gui.QComboBox)
                        self.assertEqual(combo.currentText(),'Vocaloid')
                        dialog.accept()
                    except Exception as exc:
                        errors.append(exc);dialog.reject()
                ui.QTimer.singleShot(0,interact);window.edit_music()
                if errors:raise errors[0]
                self.assertEqual(edit.call_args.args[-1],'Vocaloid')
                window.close()


if __name__=='__main__':unittest.main()
