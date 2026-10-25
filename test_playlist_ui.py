"""Run with: python3 -m unittest discover -s ipod-nano6g-fix -p 'test_*.py'."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import ipod_lib as lib


class PlaylistTransactions(unittest.TestCase):
    def test_order_duplicates_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            from test_import_database import create_library
            from pathlib import Path
            database_dir=create_library(Path(directory))
            path=str(database_dir/"Library.itdb")
            with closing(sqlite3.connect(path)) as con:
                con.execute("insert into item (pid) values (1),(2),(3),(4)")
                con.execute("insert into item_to_container values (1,10,0,NULL)")
                con.commit()
            with patch.object(lib, "itlp", return_value=str(database_dir)):
                self.assertEqual(lib.playlist_add_many("mount", 10, [3, 1, 2, 3]), 2)
                self.assertEqual(lib.playlist_add_many("mount", 10, [1, 2]), 0)
                with self.assertRaises(ValueError):
                    lib.playlist_add_many("mount", 10, [4, 999])
                with self.assertRaises(ValueError):
                    lib.playlist_add_many("mount", 999, [1])
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute("SELECT item_pid, physical_order FROM item_to_container ORDER BY physical_order").fetchall(), [(1, 0), (3, 1), (2, 2)])


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QTableWidget, QDialogButtonBox
    from PySide6.QtCore import QTimer
    import ipod_gui_qt as gui
except ImportError:
    gui = None


@unittest.skipIf(gui is None, "PySide6 is not installed")
class PlaylistUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_picker_filters_and_adds_visible_selection(self):
        with tempfile.TemporaryDirectory() as mount:
            os.mkdir(os.path.join(mount, "iPod_Control"))
            with patch.object(lib, "find_mount", return_value=mount), \
                 patch.object(lib, "list_tracks", return_value=[(1, "Existing", "A", ""), (2, "Alpha", "B", ""), (3, "Beta", "C", "")]), \
                 patch.object(lib, "list_playlists", return_value=[(10, "Favorites")]), \
                 patch.object(lib, "playlist_tracks", return_value=[(1, "Existing", "A")]), \
                 patch.object(lib, "list_photos", return_value=(0, "", [])), \
                 patch.object(lib, "playlist_add_many", return_value=1) as add:
                window = gui.Main()
                errors = []
                def interact():
                    dialog = self.app.activeModalWidget()
                    try:
                        table = dialog.findChild(QTableWidget)
                        self.assertEqual(table.rowCount(), 2)
                        table.selectAll()
                        dialog.findChild(QLineEdit).setText("Beta")
                        button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
                        self.assertEqual(button.text(), "Add 1 songs")
                        button.click()
                    except Exception as exc:
                        errors.append(exc)
                        dialog.reject()
                QTimer.singleShot(0, interact)
                window.pl_add()
                if errors: raise errors[0]
                add.assert_called_once_with(mount, 10, [3])
                window.refresh_all()
                self.assertEqual(window.pl_list.currentRow(), 0)
                window.close()

    def test_disconnected_startup(self):
        with patch.object(lib, "find_mount", return_value=None):
            window = gui.Main()
            self.assertFalse(window.tabs.isEnabled())
            self.assertEqual(window.music_table.rowCount(), 0)
            window.close()

    def test_import_destination_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as mount:
            os.mkdir(os.path.join(mount, "iPod_Control"))
            with patch.object(lib, "find_mount", return_value=mount), \
                 patch.object(lib, "list_tracks", return_value=[]), \
                 patch.object(lib, "list_playlists", return_value=[(10, "Favorites")]), \
                 patch.object(lib, "playlist_tracks", return_value=[]), \
                 patch.object(lib, "list_photos", return_value=(0, "", [])), \
                 patch.object(lib, "audio_metadata", side_effect=lambda src: dict(title=src.rsplit(".",1)[0],artist="Artist",album=None)), \
                 patch.object(lib, "add_track", side_effect=[(5, "song.mp3"), RuntimeError("Conversion failed")]), \
                 patch.object(lib, "playlist_add") as add:
                window = gui.Main()
                window.run_upload_jobs([("mp3", "one.mp3", "A", None), ("mp3", "two.mp3", "A", None)], 10)
                self.assertFalse(window._worker.isRunning())
                self.assertTrue(window.centralWidget().isEnabled())
                add.assert_called_once_with(mount, 10, 5)
                self.assertIn("1 imported, 1 failed, 0 skipped", window.statusBar().currentMessage())
                window.close()

    def test_url_import_metadata_destination_and_cleanup(self):
        temporary_paths = []
        def download(url, directory, progress, canceled):
            self.assertEqual(url, "https://example.com/song")
            self.assertFalse(canceled())
            source = os.path.join(directory, "import.m4a")
            with open(source, "wb") as stream: stream.write(b"fixture")
            temporary_paths.append(source)
            progress("Cover verified")
            return source, {"title": "Song", "artist": "Artist", "album": "Album"}
        def import_track(mount, source, title, artist, album):
            self.assertTrue(os.path.isfile(source))
            self.assertEqual((title, artist, album), ("Song", "Artist", "Album"))
            return 5, "song.m4a"
        with tempfile.TemporaryDirectory() as mount:
            os.mkdir(os.path.join(mount, "iPod_Control"))
            with patch.object(lib, "find_mount", return_value=mount), \
                 patch.object(lib, "list_tracks", return_value=[]), \
                 patch.object(lib, "list_playlists", return_value=[(10, "Favorites")]), \
                 patch.object(lib, "playlist_tracks", return_value=[]), \
                 patch.object(lib, "list_photos", return_value=(0, "", [])), \
                 patch.object(gui.yt_download, "download_audio", side_effect=download), \
                 patch.object(lib, "add_track", side_effect=import_track), \
                 patch.object(lib, "playlist_add") as add:
                window = gui.Main()
                window.run_upload_jobs([("url", "https://example.com/song")], 10)
                add.assert_called_once_with(mount, 10, 5)
                self.assertIn("1 imported, 0 failed", window.statusBar().currentMessage())
                self.assertTrue(temporary_paths)
                self.assertTrue(all(not os.path.exists(p) for p in temporary_paths))
                window.close()


if __name__ == "__main__":
    unittest.main()
