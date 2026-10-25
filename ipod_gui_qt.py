#!/usr/bin/env python3
"""Qt6 (PySide6) Nano6G uploader. Native dialogs, responsive layout."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipod_lib as lib
import yt_download
from storage_widget import StorageFooter
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem,
    QListWidget, QTextEdit, QFileDialog, QMessageBox, QLineEdit, QLabel,
    QAbstractItemView, QSplitter, QInputDialog, QDialog, QDialogButtonBox,
    QFormLayout, QComboBox, QHeaderView)
from PySide6.QtCore import Qt

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nano6G Uploader")
        self.resize(960, 700)
        self.tracks = []; self.playlists = []; self.photos = []
        self.master_pid = None
        self.setAcceptDrops(True)
        cw = QWidget(); self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Mount:"))
        self.mount_edit = QLineEdit(lib.find_mount() or "")
        bar.addWidget(self.mount_edit, 1)
        b = QPushButton("Detect"); b.clicked.connect(self.detect); bar.addWidget(b)
        b = QPushButton("Refresh"); b.clicked.connect(self.refresh_all); bar.addWidget(b)
        b = QPushButton("Check library"); b.clicked.connect(self.check_library); bar.addWidget(b)
        b = QPushButton("Repair library"); b.clicked.connect(self.repair_library); bar.addWidget(b)
        b = QPushButton("Eject"); b.clicked.connect(self.eject); bar.addWidget(b)
        root.addLayout(bar)
        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        self.build_music(); self.build_pl(); self.build_photo()
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("Activity and transfer results appear here.")
        root.addWidget(self.log)
        self.storage=StorageFooter();root.addWidget(self.storage)
        from PySide6.QtCore import QTimer
        self.storage_timer=QTimer(self);self.storage_timer.setInterval(30000)
        self.storage_timer.timeout.connect(self.refresh_storage);self.storage_timer.start()
        self.mount_edit.editingFinished.connect(self.refresh_all)
        self.statusBar().showMessage("Connect your iPod, then choose Detect.")
        self.refresh_all()

    def msg(self, s):
        self.log.append(s)
        self.statusBar().showMessage(s)
    def mount(self): return self.mount_edit.text().strip()

    def refresh_storage(self):
        if getattr(self,"_worker",None) and self._worker.isRunning():return
        self.storage.refresh(self.mount())

    def detect(self):
        m = lib.find_mount()
        if m: self.mount_edit.setText(m); self.msg(f"found {m}"); self.refresh_all()
        else: self.msg("not found - replug iPod and Detect again")

    def eject(self):
        if getattr(self,"_worker",None) and self._worker.isRunning():
            self.msg("Wait for the current transfer before ejecting.");return
        try:lib.eject_device(self.mount())
        except Exception as e:
            QMessageBox.warning(self,"Eject failed",str(e));return
        self.storage.clear("iPod ejected.")
        self.msg("iPod ejected. You can unplug it now.")
        self.tracks = []; self.playlists = []; self.photos = []
        self.music_table.setRowCount(0)
        self.pl_list.clear(); self.pl_tracks.clear()
        self.photo_list.clear()
        self.tabs.setEnabled(False)

    def refresh_all(self):
        m = self.mount()
        self.refresh_storage()
        connected = bool(m and os.path.isdir(os.path.join(m, "iPod_Control")))
        self.tabs.setEnabled(connected)
        if not connected:
            self.tracks = []; self.playlists = []; self.photos = []
            self.music_table.setRowCount(0); self.pl_list.clear(); self.pl_tracks.clear()
            self.pl_combo.clear(); self.photo_list.clear()
            self.msg("No iPod at this mount. Connect it and choose Detect."); return
        current = self.pl_list.currentRow()
        selected_pid = self.playlists[current][0] if 0 <= current < len(self.playlists) else None
        try:
            try: self.master_pid = lib.primary_container(m)
            except Exception: self.master_pid = None
            self.tracks = lib.list_tracks(m)
            self.music_table.setSortingEnabled(False)
            self.music_table.setRowCount(len(self.tracks))
            for i, (pid, t, a, al) in enumerate(self.tracks):
                self.music_table.setItem(i, 0, QTableWidgetItem(str(pid)))
                self.music_table.setItem(i, 1, QTableWidgetItem(t or ""))
                self.music_table.setItem(i, 2, QTableWidgetItem(a or ""))
                self.music_table.setItem(i, 3, QTableWidgetItem(al or ""))
            pls = lib.list_playlists(m)
            self.playlists = pls
            self.pl_list.clear()
            for pid, n in pls: self.pl_list.addItem(n)
            self.pl_list.setCurrentRow(next((i for i, (p, _) in enumerate(pls) if p == selected_pid), 0 if pls else -1))
            if hasattr(self, "pl_combo"):
                cur = self.pl_combo.currentData()
                self.pl_combo.clear()
                for pid, name in pls:
                    if pid != self.master_pid: self.pl_combo.addItem(name, pid)
                idx = self.pl_combo.findData(cur)
                if idx >= 0: self.pl_combo.setCurrentIndex(idx)
            self.music_table.setSortingEnabled(True)
            self.filter_music(self.music_search.text() if hasattr(self, "music_search") else "")
            try:
                if hasattr(self, "photo_list"): self.refresh_photos()
            except Exception as e: self.msg(f"photos: {e}")
        except Exception as e:
            self.tracks=[];self.playlists=[];self.photos=[]
            self.music_table.setRowCount(0);self.pl_list.clear();self.pl_tracks.clear();self.pl_combo.clear();self.photo_list.clear()
            self.tabs.setEnabled(False);self.msg(f"Cannot load this iPod: {e}")

    def check_library(self):
        try:
            from diagnose_ipod import diagnose
            report=diagnose(self.mount())
            warnings=report.get("issues",[])
            text=f"{report['library_tracks']} tracks checked.\n" + ("\n".join(warnings) if warnings else "No consistency problems found.")
            QMessageBox.information(self,"Library check",text)
        except Exception as e:QMessageBox.warning(self,"Library check failed",str(e))

    def repair_library(self):
        if getattr(self,"_worker",None) and self._worker.isRunning():return
        from PySide6.QtCore import QThread,Signal
        from PySide6.QtWidgets import QProgressDialog
        mount=self.mount()
        class Repair(QThread):
            result=Signal(object)
            def run(self):
                try:self.result.emit(lib.repair_library(mount))
                except Exception as e:self.result.emit(e)
        self._worker=Repair()
        dialog=QProgressDialog("Repairing saved metadata and artwork…",None,0,0,self)
        dialog.setWindowTitle("Repair library");dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setAutoClose(False);dialog.setAutoReset(False)
        self.centralWidget().setEnabled(False)
        results=[];self._worker.result.connect(results.append)
        def done():
            dialog.close();self.centralWidget().setEnabled(True);self.refresh_all()
            result=results[0] if results else RuntimeError("Repair returned no result")
            if isinstance(result,Exception):QMessageBox.warning(self,"Repair failed",str(result))
            else:
                self.msg(f"Library repaired; {result['covers']} covers restored. Backup: {result['backup']}")
                if result['warnings']:QMessageBox.warning(self,"Repair needs attention","\n".join(result['warnings']))
        self._worker.finished.connect(done);self._worker.start();dialog.exec()

    def build_music(self):
        w = QWidget(); v = QVBoxLayout(w)
        h = QHBoxLayout()
        h.addWidget(QLabel("Search:"))
        self.music_search = QLineEdit(); self.music_search.setPlaceholderText("filter title/artist/album")
        self.music_search.setClearButtonEnabled(True)
        self.music_search.textChanged.connect(self.filter_music)
        h.addWidget(self.music_search, 1)
        v.addLayout(h)
        self.music_table = QTableWidget(0, 4)
        self.music_table.setHorizontalHeaderLabels(["pid", "title", "artist", "album"])
        self.music_table.setColumnHidden(0, True)
        self.music_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.music_table.setAlternatingRowColors(True)
        self.music_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.music_table.horizontalHeader().setStretchLastSection(True)
        self.music_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.music_table.setSortingEnabled(True)
        self.music_table.sortItems(1,Qt.AscendingOrder)
        self.music_table.verticalHeader().setVisible(False)
        v.addWidget(self.music_table, 1)
        self.music_count = QLabel()
        v.addWidget(self.music_count)
        self.music_table.itemSelectionChanged.connect(lambda: self.filter_music(self.music_search.text()))
        from PySide6.QtWidgets import QComboBox
        h = QHBoxLayout()
        b = QPushButton("Import audio…"); b.clicked.connect(lambda: self.add_music()); h.addWidget(b)
        b = QPushButton("Download URL…"); b.clicked.connect(self.add_urls); h.addWidget(b)
        b = QPushButton("Edit..."); b.clicked.connect(self.edit_music); h.addWidget(b)
        b = QPushButton("Delete track"); b.clicked.connect(self.del_music); h.addWidget(b)
        h.addWidget(QLabel("to playlist:"))
        self.pl_combo = QComboBox(); self.pl_combo.setMinimumWidth(160); h.addWidget(self.pl_combo)
        b = QPushButton("Add selected songs"); b.clicked.connect(self.pl_add_here); h.addWidget(b)
        h.addStretch(1); v.addLayout(h)
        self.tabs.addTab(w, "Music")
        self.music_table.itemDoubleClicked.connect(lambda *a: self.edit_music())

    def filter_music(self, text):
        for i in range(self.music_table.rowCount()):
            cells = [self.music_table.item(i, c).text() if self.music_table.item(i, c) else "" for c in (1, 2, 3)]
            show = (not text.strip()) or any(lib.match_query(c, text) for c in cells)
            self.music_table.setRowHidden(i, not show)
        visible = sum(not self.music_table.isRowHidden(i) for i in range(self.music_table.rowCount()))
        self.music_count.setText(f"{visible} of {self.music_table.rowCount()} songs · {len(self.selected_track_pids())} selected · Drop audio files to import")

    def selected_track_pids(self):
        return [int(self.music_table.item(i.row(), 0).text())
                for i in sorted(self.music_table.selectionModel().selectedRows(), key=lambda i: i.row())
                if not self.music_table.isRowHidden(i.row()) and self.music_table.item(i.row(), 0)]

    def edit_music(self):
        from PySide6.QtWidgets import QDialog, QFormLayout, QComboBox, QDialogButtonBox
        if len(self.selected_track_pids()) != 1:
            QMessageBox.information(self, "Edit song", "Select exactly one visible song to edit."); return
        r = next(i.row() for i in self.music_table.selectionModel().selectedRows() if not self.music_table.isRowHidden(i.row()))
        if r is None or r < 0: return
        try: ipid = int(self.music_table.item(r, 0).text())
        except (ValueError, AttributeError): return
        cur = [self.music_table.item(r, c).text() if self.music_table.item(r, c) else "" for c in (1, 2, 3)]
        try:
            genres = lib.list_genres(self.mount())
            current_genre = lib.track_genre(self.mount(), ipid)
        except Exception as e:
            QMessageBox.critical(self, "Cannot read genre", str(e)); return
        dlg = QDialog(self); dlg.setWindowTitle(f"Edit pid {ipid}")
        f = QFormLayout(dlg)
        e_title = QLineEdit(cur[0]); e_artist = QLineEdit(cur[1]); e_album = QLineEdit(cur[2])
        c_genre = QComboBox(); c_genre.addItems(genres)
        if current_genre in genres: c_genre.setCurrentText(current_genre)
        else: c_genre.setCurrentIndex(-1)
        f.addRow("Title:", e_title); f.addRow("Artist:", e_artist)
        f.addRow("Album (blank = none):", e_album); f.addRow("Genre:", c_genre)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.Accepted: return
        try:
            lib.retitle_track(self.mount(), ipid, e_title.text().strip(), e_artist.text().strip() or "Unknown",
                              e_album.text().strip() or None, c_genre.currentText())
            self.msg(f"updated pid {ipid}. Eject + check."); self.refresh_all()
        except Exception as e: QMessageBox.critical(self, "edit failed", str(e))

    def pl_add_here(self):
        pids = self.selected_track_pids()
        cpid = self.pl_combo.currentData()
        if not pids or cpid is None:
            QMessageBox.information(self, "Add songs", "Select songs and a destination playlist first. Create a playlist in the Playlists tab if needed."); return
        self.add_to_playlist(cpid, pids)

    def add_to_playlist(self, cpid, pids):
        try:
            count = lib.playlist_add_many(self.mount(), cpid, pids)
            self.show_pl_contents(self.pl_list.currentRow())
            self.msg(f"Added {count} songs · {len(pids)-count} already in playlist.")
        except Exception as e: QMessageBox.critical(self, "failed", str(e))

    def del_music(self):
        selected = self.selected_track_pids()
        if len(selected) != 1:
            QMessageBox.information(self,"Delete song","Select exactly one visible song to delete."); return
        ipid=selected[0]
        r=next(i.row() for i in self.music_table.selectionModel().selectedRows() if not self.music_table.isRowHidden(i.row()))
        title = self.music_table.item(r, 1).text() if self.music_table.item(r, 1) else str(ipid)
        if QMessageBox.question(self, "delete", f"Delete '{title}' from iPod? File + DB rows removed.") != QMessageBox.Yes: return
        try:
            lib.track_delete(self.mount(), ipid)
            self.msg(f"deleted track {title}. Eject + check."); self.refresh_all()
        except Exception as e: QMessageBox.critical(self, "delete failed", str(e))

    def add_music(self, playlist_pid=None):
        from PySide6.QtWidgets import QInputDialog
        srcs, _ = QFileDialog.getOpenFileNames(self, "Pick audio", os.path.expanduser("~/Downloads"), "Audio (*.mp3 *.flac *.m4a *.aac *.wav *.aiff *.aif *.ogg *.oga *.opus *.wma *.mp4 *.ape *.wv);;MP3 (*.mp3);;FLAC (*.flac)")
        if not srcs: return
        dlg = QDialog(self); dlg.setWindowTitle("Import audio")
        form = QFormLayout(dlg)
        form.addRow(QLabel(f"{len(srcs)} files selected. Titles, artist, album and genre use embedded tags; filenames are the title fallback."))
        artist = QLineEdit(); artist.setPlaceholderText("Keep each file's artist tag")
        album = QLineEdit(); album.setPlaceholderText("Keep each file's album tag")
        form.addRow("Artist for these files", artist); form.addRow("Album (optional)", album)
        destination = QComboBox(); destination.addItem("Library only", None)
        for pid, name in self.playlists:
            if pid != self.master_pid: destination.addItem(name, pid)
        destination.setCurrentIndex(max(0, destination.findData(playlist_pid)))
        form.addRow("Add to playlist", destination)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Import")
        buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject); form.addRow(buttons)
        if dlg.exec() != QDialog.Accepted: return
        self.run_upload_jobs([("mp3", s, artist.text().strip() or None, album.text().strip() or None) for s in srcs], destination.currentData())

    AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".mp4", ".aac", ".wav", ".aiff", ".aif", ".ogg", ".oga", ".opus", ".wma", ".ape", ".wv")

    def add_urls(self):
        try: yt_download.check_dependencies()
        except Exception as e:
            QMessageBox.information(self, "Download tools needed", str(e)); return
        dlg = QDialog(self); dlg.setWindowTitle("Download audio with yt-dlp"); dlg.resize(600, 420)
        layout = QVBoxLayout(dlg)
        help_text = QLabel("Paste URLs, one per line. Playlist links import every available song.\n"
                           "Downloads the best available audio. Compatible AAC is preserved; other\n"
                           "formats become Apple Lossless (up to 48 kHz, 16-bit stereo).\n"
                           "Thumbnails are embedded as JPEG covers and verified before import.")
        help_text.setWordWrap(True); layout.addWidget(help_text)
        urls = QTextEdit(); urls.setAcceptRichText(False)
        urls.setPlaceholderText("https://www.youtube.com/watch?v=…"); layout.addWidget(urls)
        destination = QComboBox(); destination.addItem("Library only", None)
        for pid, name in self.playlists:
            if pid != self.master_pid: destination.addItem(name, pid)
        if self.tabs.currentIndex() == 1 and self.pl_list.currentRow() >= 0:
            destination.setCurrentIndex(max(0, destination.findData(self.playlists[self.pl_list.currentRow()][0])))
        layout.addWidget(QLabel("Destination playlist")); layout.addWidget(destination)
        note = QLabel("Embedded thumbnails also populate the Nano artwork database.\n"
                      "Temporary downloads are removed after import. Cancel stops the download; an active device write finishes first.")
        note.setWordWrap(True); layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Download & import")
        layout.addWidget(buttons)
        def accept():
            try: yt_download.validate_urls(urls.toPlainText())
            except ValueError as e:
                QMessageBox.information(dlg, "Check URLs", str(e)); return
            dlg.accept()
        buttons.accepted.connect(accept); buttons.rejected.connect(dlg.reject)
        urls.setFocus()
        if dlg.exec() == QDialog.Accepted:
            self.run_upload_jobs([("url", url) for url in yt_download.validate_urls(urls.toPlainText())], destination.currentData())

    def upload_paths(self, paths):
        jobs = []
        for p in paths:
            if p.lower().endswith(self.AUDIO_EXTS):
                jobs.append(("mp3", p, None, None))
            elif p.lower().endswith((".png", ".jpg", ".jpeg")):
                jobs.append(("img", p))
        if jobs:self.run_upload_jobs(jobs)
        else:self.msg("No supported audio or image files were dropped.")

    def run_upload_jobs(self, jobs, playlist_pid=None):
        if not jobs:return
        if getattr(self, "_worker", None) and self._worker.isRunning(): return
        if not os.path.isdir(os.path.join(self.mount(), "iPod_Control")):
            QMessageBox.information(self, "Connect iPod", "Connect your iPod and choose Detect before importing."); return
        from PySide6.QtCore import QThread, Signal
        from PySide6.QtWidgets import QProgressDialog
        import subprocess as sp
        mount = self.mount()
        class Worker(QThread):
            prog = Signal(int, str)
            fin = Signal()
            def __init__(self, jobs):
                super().__init__()
                self.jobs = jobs
                self.cancel = False
                self.succeeded = 0
                self.failed = 0
                self.warnings = 0
            def import_one(self, mount, src, title, artist, album, progress):
                try: return lib.add_track(mount,src,title,artist,album)
                except lib.ImportCommittedError as exc:
                    self.warnings += 1
                    progress(f"SAVED with warning: {exc}")
                    return exc.pid,exc.relative
            def run(self):
                expanded = []
                for job in self.jobs:
                    if self.cancel: break
                    if job[0] != "url":
                        expanded.append(job)
                        continue
                    try:
                        self.prog.emit(0, "Reading URL / playlist…")
                        links = yt_download.expand_url(job[1], lambda: self.cancel)
                        expanded.extend(("url", link) for link in links)
                        self.prog.emit(0, f"Queued {len(links)} song(s)")
                    except yt_download.DownloadCanceled:
                        break
                    except Exception as exc:
                        expanded.append(("error", job[1], str(exc)))
                self.jobs = expanded
                n = len(self.jobs)
                for i, job in enumerate(self.jobs):
                    if self.cancel: break
                    try:
                        if job[0] == "error":
                            raise RuntimeError(job[2])
                        if job[0] == "url":
                            import tempfile
                            with tempfile.TemporaryDirectory(prefix="nano6g-download-") as directory:
                                src, tags = yt_download.download_audio(job[1], directory,
                                    lambda text: self.prog.emit(int(i*100/n), text), lambda: self.cancel)
                                if self.cancel: raise yt_download.DownloadCanceled()
                                pid, rel = self.import_one(mount,src,tags["title"],tags["artist"],tags["album"],lambda text: self.prog.emit(int(i*100/n),text))
                                if playlist_pid is not None:
                                    try: lib.playlist_add(mount, playlist_pid, pid)
                                    except Exception as e:
                                        self.warnings += 1
                                        self.prog.emit(int((i+1)*100/n),f"SAVED track {pid}; playlist needs repair: {e}")
                                self.prog.emit(int((i+1)*100/n), f"Imported {tags['title']}")
                        elif job[0] == "mp3":
                            _, src, artist, album = job
                            metadata = lib.audio_metadata(src)
                            title = metadata["title"]
                            pid, rel = self.import_one(mount, src, title, artist or metadata["artist"], album or metadata["album"],
                                                       lambda text: self.prog.emit(int(i*100/n),text))
                            if playlist_pid is not None:
                                try: lib.playlist_add(mount, playlist_pid, pid)
                                except Exception as e:
                                    self.warnings += 1
                                    self.prog.emit(int((i+1)*100/n),f"SAVED track {pid}; playlist needs repair: {e}")
                            self.prog.emit(int((i+1)*100/n), f"added {title} pid={pid}")
                        else:
                            code, out = lib.add_photo(mount, job[1])
                            if code: raise RuntimeError(out or f"Photo import exited with {code}")
                            self.prog.emit(int((i+1)*100/n), out.strip() or f"photo exit {code}")
                        self.succeeded += 1
                    except yt_download.DownloadCanceled:
                        break
                    except Exception as e:
                        self.failed += 1
                        self.prog.emit(int((i+1)*100/n), f"FAILED {job[1]}: {e}")
                self.fin.emit()
        self._worker = Worker(jobs)
        dlg = QProgressDialog("Uploading...", "Cancel", 0, 100, self)
        dlg.setWindowTitle("Importing files"); dlg.setMinimumDuration(0)
        dlg.setAutoClose(False); dlg.setAutoReset(False)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setValue(0)
        self.centralWidget().setEnabled(False)
        self._worker.prog.connect(lambda v, s: (self.msg(s), dlg.setLabelText(s), dlg.setValue(v)))
        def finished():
            dlg.canceled.disconnect(cancel)
            dlg.close()
            self.centralWidget().setEnabled(True)
            self.refresh_all()
            worker = self._worker
            remaining = len(worker.jobs) - worker.succeeded - worker.failed
            self.msg(f"Import {'canceled' if worker.cancel else 'finished'}: {worker.succeeded} imported, {worker.failed} failed, {remaining} skipped. {worker.warnings} warnings. Eject before unplugging.")
        self._worker.finished.connect(finished)
        def cancel():
            self._worker.cancel = True
            self.msg("Canceling download; any active device write will finish first…")
        dlg.canceled.connect(cancel)
        self._worker.start()
        dlg.exec()

    def closeEvent(self, event):
        if getattr(self, "_worker", None) and self._worker.isRunning():
            self.msg("Wait for the current import to finish before closing.")
            event.ignore()
        else:
            self.storage_timer.stop();self.storage.stop();event.accept()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths: self.upload_paths(paths)

    def build_pl(self):
        from PySide6.QtWidgets import QSplitter
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(QLabel("Choose a playlist, then add songs from your library or import new audio files."))
        sp = QSplitter()
        self.pl_list = QListWidget(); sp.addWidget(self.pl_list)
        self.pl_tracks = QListWidget(); sp.addWidget(self.pl_tracks)
        sp.setSizes([300, 400])
        v.addWidget(sp, 1)
        self.pl_list.currentRowChanged.connect(self.show_pl_contents)
        self.music_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pl_summary = QLabel("Create a playlist to start organizing your music.")
        v.addWidget(self.pl_summary)
        h = QHBoxLayout()
        b = QPushButton("New..."); b.clicked.connect(self.new_pl); h.addWidget(b)
        self.pl_add_button = QPushButton("Add songs…"); self.pl_add_button.clicked.connect(self.pl_add); h.addWidget(self.pl_add_button)
        self.pl_import_button = QPushButton("Import audio into playlist…"); self.pl_import_button.clicked.connect(self.pl_import); h.addWidget(self.pl_import_button)
        b = QPushButton("Download URL…"); b.clicked.connect(self.add_urls); h.addWidget(b)
        b = QPushButton("Delete playlist"); b.clicked.connect(self.pl_del); h.addWidget(b)
        h.addStretch(1); v.addLayout(h)
        self.tabs.addTab(w, "Playlists")

    def show_pl_contents(self, row):
        self.pl_tracks.clear()
        valid = row is not None and 0 <= row < len(self.playlists)
        editable = valid and self.playlists[row][0] != self.master_pid
        self.pl_add_button.setEnabled(editable); self.pl_import_button.setEnabled(editable)
        self.pl_summary.setText("Choose or create a playlist.")
        if not valid: return
        try:
            tracks = lib.playlist_tracks(self.mount(), self.playlists[row][0])
            for pid, t, a in tracks:
                self.pl_tracks.addItem(f"{t} - {a}")
            self.pl_summary.setText(f"{self.playlists[row][1]} · {len(tracks)} songs" + (" · Add songs to get started." if not tracks else ""))
        except Exception as e: self.msg(f"playlist view: {e}")

    def new_pl(self):
        name, ok = QInputDialog.getText(self, "playlist", "name:")
        name = name.strip()
        if not ok or not name: return
        try:
            pid = lib.create_playlist(self.mount(), name)
            self.msg(f"playlist {name} pid={pid}. Eject + check."); self.refresh_all()
            self.pl_list.setCurrentRow(next(i for i, (p, _) in enumerate(self.playlists) if p == pid))
        except Exception as e: QMessageBox.critical(self, "failed", str(e))

    def sel_track_pid(self):
        r = self.music_table.currentRow()
        if r is None or r < 0: return None
        it = self.music_table.item(r, 0)
        try: return int(it.text()) if it else None
        except ValueError: return None

    def pl_add(self):
        r = self.pl_list.currentRow()
        if r < 0: return
        cpid, name = self.playlists[r]
        try:
            existing = {p for p, _, _ in lib.playlist_tracks(self.mount(), cpid)}
        except Exception as e:
            QMessageBox.critical(self, "Cannot load playlist", str(e)); return
        dlg = QDialog(self); dlg.setWindowTitle(f"Add songs to {name}"); dlg.resize(720, 480)
        layout = QVBoxLayout(dlg)
        search = QLineEdit(); search.setPlaceholderText("Search title, artist or album…"); search.setClearButtonEnabled(True)
        layout.addWidget(search)
        table = QTableWidget(0, 3); table.setHorizontalHeaderLabels(["Title", "Artist", "Album"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setAlternatingRowColors(True); table.verticalHeader().hide()
        available = [t for t in self.tracks if t[0] not in existing]
        table.setRowCount(len(available))
        for row, (pid, title, artist, album) in enumerate(available):
            for col, value in enumerate((title, artist, album)):
                item = QTableWidgetItem(value or ""); item.setData(Qt.UserRole, pid)
                table.setItem(row, col, item)
        table.setSortingEnabled(True); layout.addWidget(table)
        summary = QLabel(); layout.addWidget(summary)
        actions = QHBoxLayout(); layout.addLayout(actions)
        select = QPushButton("Select visible"); actions.addWidget(select)
        clear = QPushButton("Clear selection"); clear.clicked.connect(table.clearSelection); actions.addWidget(clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); actions.addWidget(buttons)
        add = buttons.button(QDialogButtonBox.Ok)
        def selected():
            return [table.item(i.row(), 0).data(Qt.UserRole) for i in table.selectionModel().selectedRows() if not table.isRowHidden(i.row())]
        def update():
            for row in range(table.rowCount()):
                text = " ".join(table.item(row, c).text() for c in range(3))
                table.setRowHidden(row, not lib.match_query(text, search.text()) if search.text().strip() else False)
            n = len(selected()); add.setText(f"Add {n} songs"); add.setEnabled(n > 0)
            summary.setText(f"{n} selected · Songs already in this playlist are excluded." if available else "All library songs are already in this playlist. Import audio to add new songs.")
        select.clicked.connect(table.selectAll)
        search.textChanged.connect(update); table.itemSelectionChanged.connect(update)
        buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
        update(); search.setFocus()
        if dlg.exec() == QDialog.Accepted: self.add_to_playlist(cpid, selected())

    def pl_import(self):
        row = self.pl_list.currentRow()
        if row >= 0: self.add_music(self.playlists[row][0])

    def pl_del(self):
        r = self.pl_list.currentRow()
        if r < 0: return
        pid, name = self.playlists[r]
        if pid == self.master_pid:
            QMessageBox.warning(self, "refuse", "cannot delete master iPod container"); return
        if QMessageBox.question(self, "delete", f"Delete playlist '{name}'? Tracks stay on iPod.") != QMessageBox.Yes: return
        try:
            lib.playlist_delete(self.mount(), pid)
            self.msg(f"deleted playlist {name}. Eject + check."); self.refresh_all()
        except Exception as e: QMessageBox.critical(self, "failed", str(e))

    def build_photo(self):
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        from PySide6.QtGui import QIcon
        w = QWidget(); v = QVBoxLayout(w)
        v.addWidget(QLabel("Original file is only read. Eject + check Photos after."))
        self.photo_list = QListWidget()
        self.photo_list.setViewMode(QListWidget.IconMode)
        self.photo_list.setIconSize(__import__("PySide6.QtCore", fromlist=["QSize"]).QSize(96, 96))
        self.photo_list.setResizeMode(QListWidget.Adjust)
        v.addWidget(self.photo_list, 1)
        h = QHBoxLayout()
        b = QPushButton("Refresh"); b.clicked.connect(lambda: (self.refresh_photos(),self.refresh_storage())); h.addWidget(b)
        b = QPushButton("Add image..."); b.clicked.connect(self.add_photo); h.addWidget(b)
        b = QPushButton("Delete photo"); b.clicked.connect(self.del_photo); h.addWidget(b)
        h.addStretch(1); v.addLayout(h)
        self.tabs.addTab(w, "Photos")
        self.photos = []

    def refresh_photos(self):
        from PySide6.QtWidgets import QListWidgetItem
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtCore import QSize, Qt
        self.photo_list.clear()
        code, out, photos = lib.list_photos(self.mount())
        self.photos = photos
        if code != 0:
            self.msg(out or "photo list failed - replug iPod?"); return
        for p in photos:
            it = QListWidgetItem(f"id {p['id']}\n{p['album']}")
            if os.path.isfile(p["preview"]):
                pm = QPixmap(p["preview"])
                if not pm.isNull():
                    pm = pm.scaled(QSize(96, 96), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    it.setIcon(QIcon(pm))
            it.setData(Qt.UserRole,p["id"])
            self.photo_list.addItem(it)
        self.msg(f"{len(photos)} photos")

    def del_photo(self):
        it = self.photo_list.currentItem()
        if not it: return
        pid = it.data(Qt.UserRole)
        if QMessageBox.question(self, "delete", f"Delete photo id {pid}?") != QMessageBox.Yes: return
        code, out = lib.delete_photo(self.mount(), pid)
        self.msg(out or f"photo_del exit {code}")
        if code == 0:
            self.refresh_photos();self.refresh_storage()
        else: QMessageBox.critical(self, "delete failed", out or f"photo_del exit {code} with no message - see log")

    def add_photo(self):
        src, _ = QFileDialog.getOpenFileName(self, "Pick image", os.path.expanduser("~/Downloads"), "Images (*.png *.jpg *.jpeg)")
        if not src: return
        self.run_upload_jobs([("img", src)])

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = Main()
    win.show()
    sys.exit(app.exec())
