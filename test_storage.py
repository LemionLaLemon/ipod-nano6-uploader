from collections import namedtuple
from pathlib import Path
from unittest.mock import patch
import tempfile
import threading
import time
import unittest
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from storage_usage import storage_usage,ScanCanceled,format_bytes
from storage_widget import StorageFooter
import ipod_gui_qt as gui

Usage=namedtuple('usage','total used free')
EXAMPLE=dict(total_bytes=8_000_000_000,used_bytes=4_000_000_000,free_bytes=4_000_000_000,
             music_bytes=3_200_000_000,photo_bytes=400_000_000,other_bytes=400_000_000)


class StorageAllocation(unittest.TestCase):
    def test_music_includes_covers_photos_include_thumbnails_and_other_balances(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);files=[]
            for name in ['iPod_Control/Music/F00/song.m4a','iPod_Control/Artwork/F1073_1.ithmb',
                         'Photos/Thumbs/F1093_1.ithmb','Photos/Full Resolution/original.jpg']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'hello');files.append(path)
            # Symlinks outside the media directories must not consume a media category.
            outside=root/'outside';outside.write_bytes(b'x'*10000)
            (root/'Photos/link.jpg').symlink_to(outside)
            music=sum(p.stat().st_blocks*512 for p in files[:2]);photos=sum(p.stat().st_blocks*512 for p in files[2:])
            other=32768;free=1_000_000;total=music+photos+other+free
            with patch('storage_usage.shutil.disk_usage',return_value=Usage(total,total-free,free)):
                usage=storage_usage(root)
            self.assertEqual(usage['music_bytes'],music);self.assertEqual(usage['photo_bytes'],photos)
            self.assertEqual(usage['other_bytes'],other)
            self.assertEqual(sum(usage[k] for k in ('music_bytes','photo_bytes','other_bytes','free_bytes')),total)

    def test_disconnected_path_does_not_read_host_disk_capacity(self):
        with tempfile.TemporaryDirectory() as directory,patch('storage_usage.shutil.disk_usage') as disk:
            with self.assertRaises(FileNotFoundError):storage_usage(directory)
            disk.assert_not_called()

    def test_cancel_stops_scan_before_disk_query(self):
        with tempfile.TemporaryDirectory() as directory,patch('storage_usage.shutil.disk_usage') as disk:
            (Path(directory)/'iPod_Control').mkdir()
            with self.assertRaises(ScanCanceled):storage_usage(directory,lambda:True)
            disk.assert_not_called()

    def test_sizes_use_decimal_gb(self):
        self.assertEqual(format_bytes(8_000_000_000),'8.00 GB')
        self.assertEqual(format_bytes(4_500_000),'4.5 MB')


class StorageDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def pump(self,condition):
        deadline=time.monotonic()+3
        while not condition() and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.005)
        self.assertTrue(condition())

    def test_breakdown_and_used_percentage_match_capacity(self):
        footer=StorageFooter();footer.set_usage(EXAMPLE)
        self.assertEqual(footer.bar.value(),50)
        self.assertIn('4.00 GB free',footer.summary.text())
        self.assertIn('Music 3.20 GB (40.0%)',footer.labels['music_bytes'].text())
        self.assertIn('Photos 400.0 MB (5.0%)',footer.labels['photo_bytes'].text())
        footer.stop();footer.close()

    def test_ejected_footer_cannot_be_repopulated_by_stale_scan(self):
        entered=threading.Event();release=threading.Event()
        def scan(*args):entered.set();release.wait(3);return EXAMPLE
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'iPod_Control').mkdir();footer=StorageFooter()
            with patch('storage_widget.storage_usage',side_effect=scan):
                footer.refresh(directory);self.assertTrue(entered.wait(3))
                footer.clear('iPod ejected.');release.set()
                self.pump(lambda:footer._active is None)
            self.assertIsNone(footer.usage);self.assertEqual(footer.summary.text(),'iPod ejected.')
            footer.stop();footer.close()

    def test_scan_errors_clear_previous_free_space(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'iPod_Control').mkdir();footer=StorageFooter()
            footer.set_usage(EXAMPLE)
            with patch('storage_widget.storage_usage',side_effect=OSError('unplugged')):
                footer.refresh(directory);self.pump(lambda:footer._active is None)
            self.assertIsNone(footer.usage);self.assertIn('unavailable',footer.summary.text())
            footer.stop();footer.close()

    def test_main_window_eject_clears_storage(self):
        with patch.object(gui.lib,'find_mount',return_value=None):window=gui.Main()
        window.storage.set_usage(EXAMPLE)
        with patch.object(gui.lib,'eject_device'):window.eject()
        self.assertIsNone(window.storage.usage)
        self.assertEqual(window.storage.summary.text(),'iPod ejected.')
        window.close()


if __name__=='__main__':unittest.main()
