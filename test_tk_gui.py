"""Smoke checks for the supported Tk fallback on a private virtual display."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
try:
    import tkinter as tk
    import ipod_gui as gui
except ImportError:
    gui=None
from test_import_database import create_library


@unittest.skipUnless(gui is not None and shutil.which('Xvfb'),'Tk and Xvfb required')
class TkFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous=os.environ.get('DISPLAY')
        cls.server=subprocess.Popen(['Xvfb','-displayfd','1','-screen','0','1024x768x24','-nolisten','tcp'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        display=cls.server.stdout.readline().strip()
        if not display:raise unittest.SkipTest('Virtual display could not start')
        os.environ['DISPLAY']=':'+display

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate();cls.server.wait(timeout=5);cls.server.stdout.close()
        if cls.previous is None:os.environ.pop('DISPLAY',None)
        else:os.environ['DISPLAY']=cls.previous

    def test_disconnected_launch(self):
        with patch.object(gui.lib,'find_mount',return_value=None):
            app=gui.App();app.withdraw();app.update()
            self.assertEqual(app.music_list.size(),0);app.destroy()

    def test_playlist_creation_and_eject_use_current_backend(self):
        with tempfile.TemporaryDirectory() as mount:
            create_library(Path(mount))
            with patch.object(gui.lib,'find_mount',return_value=mount):
                app=gui.App();app.withdraw();app.update()
                with patch('tkinter.simpledialog.askstring',return_value='Tk test playlist'):app.new_pl()
                self.assertIn('Tk test playlist',[name for pid,name in app.playlists])
                with patch.object(gui.lib,'eject_device') as eject:app.eject()
                eject.assert_called_once_with(mount)
                self.assertEqual(app.pl_list.size(),0);app.destroy()

    def test_eject_is_blocked_during_url_import(self):
        with patch.object(gui.lib,'find_mount',return_value=None):
            app=gui.App();app.withdraw();app._download_running=True
            with patch.object(gui.lib,'eject_device') as eject,patch.object(gui.messagebox,'showinfo'):
                app.eject();eject.assert_not_called()
            app._download_running=False;app.destroy()


if __name__=='__main__':unittest.main()
