#!/usr/bin/env python3
"""Minimal Nano6G uploader: music + playlists + photos. No libgpod music parse."""
import os, tkinter as tk
from tkinter import filedialog, messagebox, ttk
import ipod_lib as lib
import subprocess
import yt_download

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Nano6G Uploader")
        self.geometry("720x520")
        self.minsize(640, 440)
        self.tracks = []; self.playlists = []
        self.master_pid=None
        self._download_running = False
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.mount = tk.StringVar(value=lib.find_mount() or "")
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Mount:").pack(side="left")
        ttk.Entry(top, textvariable=self.mount, width=50).pack(side="left", padx=4)
        ttk.Button(top, text="Detect", command=self.detect).pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_all).pack(side="left", padx=4)
        ttk.Button(top, text="Eject", command=self.eject).pack(side="left", padx=4)
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.log = tk.Text(self, height=8); self.log.pack(fill="x", padx=8, pady=4)
        self.build_music(nb); self.build_pl(nb); self.build_photo(nb)
        self.refresh_all()

    def msg(self, s):
        self.log.insert("end", s+"\n"); self.log.see("end")

    def close_app(self):
        if self._download_running:
            messagebox.showinfo("Import running", "Cancel the download and wait for any active device write to finish before closing.")
        else: self.destroy()

    def detect(self):
        m = lib.find_mount()
        if m: self.mount.set(m); self.msg(f"found {m}"); self.refresh_all()
        else: self.msg("not found - replug iPod")

    def eject(self):
        if self._download_running:
            messagebox.showinfo("Import running","Wait for the current import before ejecting.");return
        try:
            lib.eject_device(self.mount.get())
            self.music_list.delete(0,"end");self.pl_list.delete(0,"end")
            self.tracks=[];self.playlists=[]
            self.msg("iPod ejected. You can unplug it now.")
        except Exception as e:messagebox.showerror("Eject failed",str(e))

    def refresh_all(self):
        m = self.mount.get()
        if not m or not os.path.isdir(os.path.join(m, "iPod_Control")):
            self.music_list.delete(0, "end"); self.pl_list.delete(0, "end")
            self.tracks = []; self.playlists = []
            self.msg("No iPod at this mount. Connect it and choose Detect.")
            return
        try:
            self.master_pid=lib.primary_container(m)
            tracks = lib.list_tracks(m)
            self.tracks = tracks
            self.music_list.delete(0, "end")
            for pid, t, a, al in tracks:
                self.music_list.insert("end", f"{pid} | {t} - {a}")
            pls = lib.list_playlists(m)
            self.playlists = pls
            self.pl_list.delete(0, "end")
            for pid, n in pls:
                self.pl_list.insert("end", f"{n}")
        except Exception as e:
            self.msg(f"refresh failed: {e}")

    def build_music(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="Music")
        self.music_list = tk.Listbox(f, height=14, exportselection=False); self.music_list.pack(fill="both", expand=True, padx=6, pady=4)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="Import audio…", command=self.add_music).pack(side="left")
        ttk.Button(row, text="Download URL…", command=self.add_urls).pack(side="left", padx=4)

    def add_urls(self):
        import queue
        import tempfile
        import threading
        if not os.path.isdir(os.path.join(self.mount.get(), "iPod_Control")):
            messagebox.showinfo("Connect iPod", "Connect your iPod and choose Detect first."); return
        try: yt_download.check_dependencies()
        except Exception as e:
            messagebox.showinfo("Download tools needed", str(e)); return
        dlg = tk.Toplevel(self); dlg.title("Download audio with yt-dlp"); dlg.geometry("650x460")
        dlg.transient(self); dlg.grab_set()
        ttk.Label(dlg, text="Paste URLs, one per line. Playlist-only URLs import their first item.\n"
                  "Best available audio → original compatible AAC or Apple Lossless\n"
                  "(up to 48 kHz, 16-bit stereo). JPEG covers are embedded and verified.",
                  wraplength=610).pack(anchor="w", padx=12, pady=12)
        urls = tk.Text(dlg, height=7); urls.pack(fill="both", expand=True, padx=12)
        choices = [(None, "Library only")] + [(p, n) for p, n in self.playlists if p != self.master_pid]
        destination = ttk.Combobox(dlg, values=[name for _, name in choices], state="readonly")
        destination.current(0); destination.pack(fill="x", padx=12, pady=8)
        if self.pl_list.curselection():
            pid = self.playlists[self.pl_list.curselection()[0]][0]
            destination.current(next((i for i, (p, _) in enumerate(choices) if p == pid), 0))
        ttk.Label(dlg, text="Embedded covers also populate the Nano artwork database.\nTemporary downloads are removed after import.").pack(anchor="w", padx=12)
        status = ttk.Label(dlg, text="Ready", wraplength=610); status.pack(fill="x", padx=12, pady=8)
        bar = ttk.Progressbar(dlg, mode="indeterminate"); bar.pack(fill="x", padx=12)
        actions = ttk.Frame(dlg); actions.pack(fill="x", padx=12, pady=12)
        events = queue.Queue(); cancel = threading.Event(); running = False
        def close():
            if running:
                cancel.set(); status.configure(text="Canceling download; any active device write will finish first…")
            else: dlg.destroy()
        def start():
            nonlocal running
            try: links = yt_download.validate_urls(urls.get("1.0", "end"))
            except ValueError as e:
                messagebox.showinfo("Check URLs", str(e), parent=dlg); return
            mount = self.mount.get(); playlist_pid = choices[destination.current()][0]
            running = True; start_button.configure(state="disabled")
            self._download_running = True
            urls.configure(state="disabled"); destination.configure(state="disabled"); bar.start()
            def worker():
                succeeded = failed = 0
                for url in links:
                    if cancel.is_set(): break
                    try:
                        with tempfile.TemporaryDirectory(prefix="nano6g-download-") as directory:
                            src, tags = yt_download.download_audio(url, directory, lambda text: events.put(("progress", text)), cancel.is_set)
                            if cancel.is_set(): raise yt_download.DownloadCanceled()
                            try:pid,_=lib.add_track(mount,src,tags["title"],tags["artist"],tags["album"])
                            except lib.ImportCommittedError as e:
                                pid=e.pid;events.put(("progress",f"SAVED with warning: {e}"))
                            if playlist_pid is not None:
                                try: lib.playlist_add(mount, playlist_pid, pid)
                                except Exception as e:
                                    events.put(("progress",f"SAVED track {pid}; playlist needs repair: {e}"))
                        succeeded += 1
                    except yt_download.DownloadCanceled: break
                    except Exception as e:
                        failed += 1; events.put(("progress", f"FAILED {url}: {e}"))
                events.put(("done", f"{'Canceled' if cancel.is_set() else 'Finished'}: {succeeded} imported, {failed} failed, {len(links)-succeeded-failed} skipped."))
            threading.Thread(target=worker, daemon=True).start()
            poll()
        def poll():
            nonlocal running
            while not events.empty():
                kind, text = events.get_nowait(); status.configure(text=text); self.msg(text)
                if kind == "done":
                    running = False; bar.stop(); cancel_button.configure(text="Close")
                    self._download_running = False
                    self.refresh_all(); return
            dlg.after(100, poll)
        start_button = ttk.Button(actions, text="Download & import", command=start); start_button.pack(side="right")
        cancel_button = ttk.Button(actions, text="Cancel", command=close); cancel_button.pack(side="right", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", close); dlg.bind("<Escape>", lambda _: close()); urls.focus_set()

    def add_music(self):
        src=filedialog.askopenfilename(filetypes=[("Audio","*.mp3 *.m4a *.aac *.wav *.aiff *.aif *.flac *.opus *.ogg *.wma *.ape *.wv")])
        if not src:return
        try:
            tags=lib.audio_metadata(src)
            try:pid,rel=lib.add_track(self.mount.get(),src,tags["title"],tags["artist"],tags["album"])
            except lib.ImportCommittedError as e:
                self.msg(str(e));self.refresh_all();return
            self.msg(f"Added {tags['title']} pid={pid}. Eject before unplugging.");self.refresh_all()
        except Exception as e:messagebox.showerror("Import failed",str(e))

    def build_pl(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="Playlists")
        ttk.Label(f, text="Choose a playlist, then add songs from your library.").pack(anchor="w", padx=6, pady=6)
        self.pl_list = tk.Listbox(f, height=14, exportselection=False); self.pl_list.pack(fill="both", expand=True, padx=6, pady=4)
        row = ttk.Frame(f); row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="New...", command=self.new_pl).pack(side="left")
        ttk.Button(row, text="Add songs…", command=self.pl_add).pack(side="left", padx=4)
        ttk.Button(row, text="Download URL…", command=self.add_urls).pack(side="left", padx=4)

    def new_pl(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("playlist", "name:")
        if not name: return
        try:
            pid = lib.create_playlist(self.mount.get(), name)
            self.msg(f"playlist {name} pid={pid}. Eject + check.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("failed", str(e))

    def pl_add(self):
        if not self.pl_list.curselection():
            messagebox.showinfo("Add songs", "Choose a playlist first."); return
        cpid, name = self.playlists[self.pl_list.curselection()[0]]
        if cpid == self.master_pid:
            messagebox.showinfo("Library", "Choose a custom playlist. Imported songs are already in the iPod library."); return
        try: existing = {p for p, _, _ in lib.playlist_tracks(self.mount.get(), cpid)}
        except Exception as e:
            messagebox.showerror("Cannot load playlist", str(e)); return
        dlg = tk.Toplevel(self); dlg.title(f"Add songs to {name}"); dlg.geometry("640x460")
        dlg.transient(self); dlg.grab_set()
        query = tk.StringVar()
        ttk.Label(dlg, text="Search title, artist or album. Select multiple songs with Ctrl / Shift.").pack(anchor="w", padx=12, pady=8)
        entry = ttk.Entry(dlg, textvariable=query); entry.pack(fill="x", padx=12)
        frame = ttk.Frame(dlg); frame.pack(fill="both", expand=True, padx=12, pady=8)
        songs = tk.Listbox(frame, selectmode="extended", exportselection=False)
        scroll = ttk.Scrollbar(frame, command=songs.yview); scroll.pack(side="right", fill="y")
        songs.configure(yscrollcommand=scroll.set); songs.pack(fill="both", expand=True)
        available = [t for t in self.tracks if t[0] not in existing]
        visible = []
        summary = ttk.Label(dlg); summary.pack(anchor="w", padx=12)
        actions = ttk.Frame(dlg); actions.pack(fill="x", padx=12, pady=12)
        def update(*_):
            n = len(songs.curselection())
            summary.configure(text=f"{len(visible)} available · {n} selected · Existing playlist songs excluded")
            add.configure(text=f"Add {n} songs", state="normal" if n else "disabled")
        def filter_songs(*_):
            selected = {visible[i][0] for i in songs.curselection()}
            visible[:] = [t for t in available if not query.get().strip() or lib.match_query(" ".join(str(v or "") for v in t[1:]), query.get())]
            songs.delete(0, "end")
            for i, (pid, title, artist, album) in enumerate(visible):
                songs.insert("end", f"{title or 'Untitled'} — {artist or 'Unknown'} · {album or ''}")
                if pid in selected: songs.selection_set(i)
            update()
        def commit():
            try:
                count = lib.playlist_add_many(self.mount.get(), cpid, [visible[i][0] for i in songs.curselection()])
                self.msg(f"Added {count} songs to {name}."); dlg.destroy()
            except Exception as e: messagebox.showerror("Could not add songs", str(e), parent=dlg)
        ttk.Button(actions, text="Select visible", command=lambda: (songs.selection_set(0, "end"), update())).pack(side="left")
        ttk.Button(actions, text="Cancel", command=dlg.destroy).pack(side="right")
        add = ttk.Button(actions, command=commit); add.pack(side="right", padx=8)
        songs.bind("<<ListboxSelect>>", update); query.trace_add("write", filter_songs)
        dlg.bind("<Escape>", lambda _: dlg.destroy())
        filter_songs(); entry.focus_set()

    def build_photo(self, nb):
        f = ttk.Frame(nb); nb.add(f, text="Photos")
        ttk.Label(f, text="Uses photo_add (libgpod photodb). Original file is only read.").pack(padx=6, pady=6)
        ttk.Button(f, text="Add image...", command=self.add_photo).pack(padx=6, pady=4)

    def add_photo(self):
        src = filedialog.askopenfilename(filetypes=[("images", "*.png *.jpg *.jpeg")])
        if not src: return
        code, out = lib.add_photo(self.mount.get(), src)
        self.msg(out)
        if code == 0: self.msg("photo added. Eject + check Photos.")
        else: messagebox.showerror("photo failed", out)

if __name__ == "__main__":
    App().mainloop()
