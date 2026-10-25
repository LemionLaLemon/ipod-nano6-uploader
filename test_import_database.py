from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ipod_lib as lib
from diagnose_ipod import diagnose
from yt_download import probe, run_command


def create_library(mount):
    directory = Path(lib.itlp(str(mount)))
    directory.mkdir(parents=True)
    tables = {
        "item": "pid media_kind is_song date_modified year is_compilation remember_bookmark exclude_from_shuffle artwork_status artwork_cache_id start_time_ms stop_time_ms total_time_ms track_number track_count disc_number disc_count bpm relative_volume genre_id album_pid artist_pid composer_pid title artist album album_artist sort_title sort_artist sort_album sort_album_artist title_order artist_order album_order genre_order composer_order album_artist_order album_by_artist_order series_name_order track_artist_pid physical_order has_lyrics",
        "album": "pid kind artwork_status artwork_item_pid artist_pid user_rating name name_order all_compilations feed_url season_number is_unknown has_songs has_music_videos sort_order artist_order has_any_compilations sort_name artist_count_calc has_movies item_count",
        "artist": "pid kind artwork_status artwork_album_pid name name_order sort_name is_unknown has_songs has_music_videos",
        "track_artist": "pid name name_order sort_name has_songs has_music_videos has_non_compilation_tracks is_unknown album_count",
        "avformat_info": "item_pid sub_id audio_format bit_rate channels sample_rate duration gapless_heuristic_info gapless_encoding_delay gapless_encoding_drain gapless_last_frame_resynch analysis_inhibit_flags audio_fingerprint volume_normalization_energy",
        "genre_map": "id genre genre_order is_unknown has_music artist_count_calc album_count_calc compilation_count_calc",
        "container": "pid name name_order",
        "db_info": "primary_container_pid",
        "item_to_container": "item_pid container_pid physical_order shuffle_order",
    }
    with closing(sqlite3.connect(directory / "Library.itdb")) as con:
        for name, columns in tables.items():
            con.execute(f"CREATE TABLE {name} ({','.join(columns.split())})")
        # A renamed iPod must use db_info, not a hard-coded container name.
        con.execute("insert into container (pid,name,name_order) values(10,'My Nano',100)")
        con.execute("insert into db_info values(10)")
        con.execute("insert into genre_map values(1,'Music',100,0,0,0,0,0)")
        con.commit()
    with closing(sqlite3.connect(directory / "Locations.itdb")) as con:
        con.execute("CREATE TABLE location (item_pid,sub_id,base_location_id,location_type,location,extension,kind_id,date_created,file_size)")
        con.execute("CREATE TABLE base_location (id,path)")
        con.execute("insert into base_location values(1,'iPod_Control/Music')")
        con.commit()
    with closing(sqlite3.connect(directory / "Dynamic.itdb")) as con:
        con.execute("CREATE TABLE container_ui (container_pid PRIMARY KEY,play_order,is_reversed,album_field_order,repeat_mode,shuffle_items,has_been_shuffled)")
        con.execute("""CREATE TABLE item_stats (item_pid,has_been_played,date_played,play_count_user,
            play_count_recent,date_skipped,skip_count_user,skip_count_recent,bookmark_time_ms,
            bookmark_time_ms_common,user_rating,user_rating_common)""")
    return directory


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools required")
class ImportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name) / "ipod"
        self.directory = create_library(self.mount)
        self.source = Path(self.tmp.name) / "high-resolution.wav"
        run_command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1:sample_rate=96000",
                     "-ac", "2", "-c:a", "pcm_s24le", str(self.source)])

    def test_wav_conversion_and_complete_database_import(self):
        with patch.object(lib.ctypes, "CDLL"), patch.object(lib, "regen_cbk") as sign:
            pid, rel = lib.add_track(str(self.mount), str(self.source), "WAV song", "Artist", None)
        audio = next(s for s in probe(self.mount / "iPod_Control" / "Music" / rel)["streams"] if s["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "alac")
        self.assertEqual(audio["sample_rate"], "48000")
        self.assertEqual(int(audio["bits_per_raw_sample"]), 16)
        self.assertTrue(rel.endswith(".m4a"))
        with closing(sqlite3.connect(self.directory / "Library.itdb")) as con:
            self.assertEqual(con.execute("select title from item where pid=?", (pid,)).fetchone(), ("WAV song",))
            self.assertEqual(con.execute("select container_pid,physical_order from item_to_container where item_pid=?", (pid,)).fetchone(), (10, 0))
            self.assertEqual(con.execute("select audio_format from avformat_info where item_pid=?", (pid,)).fetchone(), (502,))
            self.assertEqual(con.execute("select channels from avformat_info where item_pid=?", (pid,)).fetchone(), (2,))
        with closing(sqlite3.connect(self.directory / "Locations.itdb")) as con:
            self.assertEqual(con.execute("select location,extension from location where item_pid=?", (pid,)).fetchone(), (rel, lib.EXT_FOURCC[".m4a"]))
        with closing(sqlite3.connect(self.directory / "Dynamic.itdb")) as con:
            self.assertEqual(con.execute("select item_pid from item_stats").fetchall(), [(pid,)])
        sign.assert_called_once()

    def test_database_failure_rolls_back_all_records_and_file(self):
        with closing(sqlite3.connect(self.directory / "Dynamic.itdb")) as con:
            con.execute("CREATE TRIGGER reject_import BEFORE INSERT ON item_stats BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with patch.object(lib.ctypes, "CDLL"), patch.object(lib, "regen_cbk") as sign:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture failure"):
                lib.add_track(str(self.mount), str(self.source), "Failed song", "Artist", None)
        for database, table in (("Library", "item"), ("Locations", "location"), ("Dynamic", "item_stats")):
            with closing(sqlite3.connect(self.directory / f"{database}.itdb")) as con:
                self.assertEqual(con.execute(f"select count(*) from {table}").fetchone()[0], 0)
        self.assertFalse(list((self.mount / "iPod_Control" / "Music").rglob("*.m4a")))
        sign.assert_not_called()

    def test_raw_aac_is_remuxed_not_renamed(self):
        source = Path(self.tmp.name) / "raw.aac"
        run_command(["ffmpeg", "-v", "error", "-i", str(self.source), "-ar", "44100", "-c:a", "aac", str(source)])
        converted, extension, temporary = lib.prepare_audio(str(source))
        try:
            self.assertTrue(temporary)
            self.assertEqual(extension, ".m4a")
            self.assertIn("mp4", probe(converted)["format"]["format_name"])
        finally:
            if temporary: os.remove(converted)

    def test_playlist_does_not_hide_dangling_membership(self):
        with closing(sqlite3.connect(self.directory / "Library.itdb")) as con:
            con.execute("insert into item_to_container values(99,10,0,NULL)"); con.commit()
        self.assertEqual(lib.playlist_tracks(str(self.mount), 10), [(99, "[Missing library entry]", None)])

    def test_signing_failure_identifies_committed_import(self):
        with patch.object(lib.ctypes, "CDLL"), patch.object(lib, "regen_cbk", side_effect=RuntimeError("signer unavailable")):
            with self.assertRaisesRegex(RuntimeError, "was saved, but device signing failed"):
                lib.add_track(str(self.mount), str(self.source), "Saved song", "Artist", None)
        with closing(sqlite3.connect(self.directory / "Library.itdb")) as con:
            self.assertEqual(con.execute("select count(*) from item").fetchone()[0], 1)

    def test_read_only_report_exposes_count_mismatch(self):
        with closing(sqlite3.connect(self.directory / "Library.itdb")) as con:
            con.execute("insert into item_to_container values(99,10,0,NULL)"); con.commit()
        before = {p: p.read_bytes() for p in self.directory.glob("*.itdb")}
        report = diagnose(self.mount)
        playlist = report["playlists"][0]
        self.assertEqual(playlist["membership_rows"], 1)
        self.assertEqual(playlist["existing_library_tracks"], 0)
        self.assertEqual(playlist["missing_track_ids"], [99])
        self.assertFalse(report["locations_checksum_matches_file"])
        for path, data in before.items(): self.assertEqual(path.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
