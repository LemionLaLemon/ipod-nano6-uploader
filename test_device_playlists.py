"""Regression for Nano playlist edits overriding SQLite membership."""
from contextlib import closing
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

import ipod_lib as lib
from test_import_database import create_library


class DevicePlaylistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        self.directory = create_library(self.mount)
        with closing(sqlite3.connect(self.directory / 'Library.itdb')) as con:
            for pid in (1, 2, 3, 4):
                con.execute('insert into item (pid,title,artist) values (?,?,?)', (pid, str(pid), 'Artist'))
            con.execute('insert into item_to_container values (4,10,0,NULL)')
            con.commit()
        self.path = self.directory.parent / 'Playlist_000000000000000A.plist'
        self.data = {'playlistPersistentID': 10, 'name': 'Favorites', 'version': 0,
                     'DBTimestampMacOSDate': 0, 'trackPersistentIDs': [2, 1, 2]}
        self.path.write_bytes(plistlib.dumps(self.data))

    def test_read_uses_device_order_and_repeats(self):
        before = self.path.read_bytes()
        self.assertEqual([r[0] for r in lib.playlist_tracks(str(self.mount), 10)], [2, 1, 2])
        self.assertEqual(self.path.read_bytes(), before)

    def test_append_preserves_device_edits_and_syncs_both_lists(self):
        self.assertEqual(lib.playlist_add_many(str(self.mount), 10, [3, 2, 3]), 1)
        self.assertEqual(lib.playlist_add_many(str(self.mount), 10, [3]), 0)
        data = plistlib.loads(self.path.read_bytes())
        self.assertEqual(data['trackPersistentIDs'], [2, 1, 2, 3])
        self.assertEqual(data['name'], 'Favorites')
        with closing(sqlite3.connect(self.directory / 'Library.itdb')) as con:
            self.assertEqual([r[0] for r in con.execute('select item_pid from item_to_container order by physical_order')], [2, 1, 2, 3])

    def test_invalid_import_rolls_back_sync_without_touching_device_list(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            lib.playlist_add_many(str(self.mount), 10, [3, 999])
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.directory / 'Library.itdb')) as con:
            self.assertEqual(con.execute('select item_pid from item_to_container').fetchall(), [(4,)])

    def test_deleted_device_list_cannot_be_resurrected_by_append(self):
        self.data['playlistDeleted'] = True
        self.path.write_bytes(plistlib.dumps(self.data))
        self.assertEqual(lib.playlist_tracks(str(self.mount), 10), [])
        with self.assertRaisesRegex(ValueError, 'deleted'):
            lib.playlist_add_many(str(self.mount), 10, [3])


if __name__ == '__main__':
    unittest.main()
