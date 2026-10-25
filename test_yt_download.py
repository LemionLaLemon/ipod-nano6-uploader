import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import yt_download as yt


class DownloadTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(yt.validate_urls("https://example.com/a\nhttps://example.com/a"), ["https://example.com/a"])
        for value in ("", "file:///etc/passwd", "--exec command"):
            with self.assertRaises(ValueError): yt.validate_urls(value)

    def test_best_source_command_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.info.json").write_text(json.dumps({"ext": "webm", "track": "Song", "artist": "Artist", "album": "Album"}))
            (root / "source.webm").touch(); (root / "source.jpg").touch()
            with patch.object(yt, "check_dependencies"), patch.object(yt, "run_command") as run, \
                 patch.object(yt, "finalize_audio", return_value="ALAC") as finalize:
                path, tags = yt.download_audio("https://example.com/a", directory)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--format") + 1], "bestaudio/best")
                self.assertIn("--ignore-config", command)
                self.assertIn("--no-playlist", command)
                self.assertIn("--write-thumbnail", command)
                self.assertEqual(command[command.index("--convert-thumbnails") + 1], "jpg")
                self.assertEqual(tags, {"title": "Song", "artist": "Artist", "album": "Album"})
                self.assertEqual(path, str(root / "import.m4a"))
                self.assertEqual(finalize.call_args.args[1], root / "source.jpg")
            (root / "source.jpg").unlink()
            with patch.object(yt, "check_dependencies"), patch.object(yt, "run_command"):
                with self.assertRaisesRegex(RuntimeError, "thumbnail"):
                    yt.download_audio("https://example.com/a", directory)

    def test_playlist_expansion(self):
        data = {"extractor": "youtube:tab", "entries": [
            {"url": "https://www.youtube.com/watch?v=one"}, None,
            {"id": "two", "ie_key": "Youtube"}]}
        with patch.object(yt, "run_command", return_value=json.dumps(data)) as run:
            self.assertEqual(yt.expand_url("https://youtube.com/playlist?list=abc"), [
                "https://www.youtube.com/watch?v=one", "https://www.youtube.com/watch?v=two"])
            self.assertIn("--flat-playlist", run.call_args.args[0])
        with patch.object(yt, "run_command") as run:
            self.assertEqual(yt.expand_url("https://youtube.com/watch?v=one"), ["https://youtube.com/watch?v=one"])
            run.assert_not_called()
        with patch.object(yt, "run_command", return_value='{"entries": []}'):
            with self.assertRaisesRegex(RuntimeError, "no available tracks"):
                yt.expand_url("https://youtube.com/playlist?list=empty")

    def test_cancel_running_process(self):
        calls = 0
        def canceled():
            nonlocal calls
            calls += 1
            return calls > 2
        with self.assertRaises(yt.DownloadCanceled):
            yt.run_command([sys.executable, "-c", "import time; time.sleep(60)"], canceled)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools required")
class RealEncodingTests(unittest.TestCase):
    def test_aac_preserved_and_alac_cover_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cover = root / "cover.png"
            yt.run_command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180", "-frames:v", "1", str(cover)])
            for codec, ext in (("aac", "m4a"), ("pcm_s24le", "wav")):
                source = root / f"source.{ext}"
                output = root / f"output-{codec}.m4a"
                yt.run_command(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2:sample_rate=48000", "-c:a", codec, str(source)])
                yt.finalize_audio(source, cover, output, {"title": "Test title", "artist": "Test artist", "album": "Test album"})
                result = yt.probe(output)
                audio = next(s for s in result["streams"] if s["codec_type"] == "audio")
                self.assertEqual(audio["codec_name"], "aac" if codec == "aac" else "alac")
                picture = next(s for s in result["streams"] if s["codec_type"] == "video")
                self.assertEqual(picture["codec_name"], "mjpeg")
                self.assertEqual(picture["disposition"]["attached_pic"], 1)
                self.assertEqual(result["format"]["tags"]["title"], "Test title")
                self.assertEqual(result["format"]["tags"]["artist"], "Test artist")
                if codec == "aac":
                    def hashes(path):
                        data = json.loads(yt.run_command(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets", "-show_data_hash", "sha256", "-of", "json", str(path)]))
                        return [p["data_hash"] for p in data["packets"]]
                    self.assertEqual(hashes(source), hashes(output))


if __name__ == "__main__":
    unittest.main()
