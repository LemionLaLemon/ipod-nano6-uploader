"""Best-source URL audio imports, finalized as Nano-compatible M4A with JPEG cover."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib.parse import urlparse, parse_qs
from audio_compat import RATES, validate_audio


class DownloadCanceled(Exception):
    pass


def check_dependencies():
    missing = [name for name in ("yt-dlp", "ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Install these tools and make sure they are on PATH: " + ", ".join(missing))


def validate_urls(text):
    urls = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
    if not urls:
        raise ValueError("Paste at least one video or audio URL, one per line.")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError(f"Expected an http(s) URL: {url}")
    return urls


def expand_url(url, canceled=lambda: False):
    """Resolve playlist links in order; ordinary video links remain single imports."""
    parsed = urlparse(url)
    if not parse_qs(parsed.query).get("list") and "/playlist" not in parsed.path:
        return [url]
    data = json.loads(run_command([
        "yt-dlp", "--ignore-config", "--yes-playlist", "--flat-playlist",
        "--dump-single-json", "--skip-download", "--", url], canceled, 300))
    entries = data.get("entries")
    if entries is None:
        return [data.get("webpage_url") or url]
    result = []
    for entry in entries:
        if not entry:
            continue
        link = entry.get("webpage_url") or entry.get("url")
        if link and link.startswith(("https://", "http://")):
            result.append(link)
        elif entry.get("id") and "youtube" in (entry.get("ie_key") or data.get("extractor") or "").lower():
            result.append("https://www.youtube.com/watch?v=" + entry["id"])
    if not result:
        raise RuntimeError("Playlist contains no available tracks.")
    return result


def run_command(command, canceled=lambda: False, timeout=1800):
    """Drain both pipes while allowing cancellation during downloads/conversion."""
    if canceled(): raise DownloadCanceled()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    started = time.monotonic()
    try:
        while True:
            if canceled(): raise DownloadCanceled()
            if time.monotonic() - started > timeout:
                raise RuntimeError(f"{command[0]} timed out")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError((stderr or stdout or f"{command[0]} failed")[-3000:])
        return stdout
    finally:
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.communicate()


def probe(path, canceled=lambda: False):
    return json.loads(run_command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], canceled, 30))


def finalize_audio(source, thumbnail, output, metadata, canceled=lambda: False):
    """Keep compatible AAC bit-for-bit; otherwise use 16-bit stereo ALAC <=48kHz."""
    streams = probe(source, canceled).get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio: raise RuntimeError("Downloaded file has no audio stream.")
    sample_rate = int(audio.get("sample_rate") or 48000)
    channels = int(audio.get("channels") or 2)
    bitrate = int(audio.get("bit_rate") or 0)
    copy = (audio.get("codec_name") == "aac" and audio.get("profile") == "LC"
            and 0 < bitrate <= 320000 and sample_rate in RATES and 1 <= channels <= 2)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-i", str(thumbnail),
           "-map", "0:a:0", "-map", "1:v:0", "-map_metadata", "-1"]
    if copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "alac", "-sample_fmt", "s16p"]
        if sample_rate not in RATES: cmd += ["-ar", "48000" if sample_rate >= 48000 else "44100"]
        if channels > 2: cmd += ["-ac", "2"]
    # A real JPEG attached picture becomes MP4's covr tag, not a video track.
    cmd += ["-c:v", "mjpeg", "-disposition:v:0", "attached_pic",
            "-metadata:s:v:0", "title=Album cover", "-metadata:s:v:0", "comment=Cover (front)"]
    for key in ("title", "artist", "album"):
        if metadata.get(key): cmd += ["-metadata", f"{key}={metadata[key]}"]
    cmd += ["-movflags", "+faststart", str(output)]
    run_command(cmd, canceled, 600)
    result = probe(output, canceled)
    if not any(s.get("codec_name") == "mjpeg" and s.get("disposition", {}).get("attached_pic")
               for s in result.get("streams", [])):
        raise RuntimeError("Cover verification failed: the M4A has no embedded JPEG cover.")
    validate_audio(result,allow_mp3=False)
    return "AAC (original audio)" if copy else "Apple Lossless (ALAC)"


def download_audio(url, directory, progress=lambda message: None, canceled=lambda: False):
    """Download one URL into a caller-owned temporary directory; return path + tags."""
    check_dependencies()
    validate_urls(url)
    directory = Path(directory)
    progress("Downloading best available audio and thumbnail…")
    run_command(["yt-dlp", "--ignore-config", "--no-playlist", "--playlist-items", "1", "--no-progress", "--no-simulate",
                 "--format", "bestaudio/best", "--write-info-json", "--no-write-playlist-metafiles", "--write-thumbnail",
                 "--convert-thumbnails", "jpg", "--output", str(directory / "source.%(ext)s"),
                 "--", url], canceled)
    info_path = directory / "source.info.json"
    if not info_path.is_file(): raise RuntimeError("yt-dlp did not produce track metadata.")
    info = json.loads(info_path.read_text())
    source = directory / f"source.{info['ext']}"
    if not source.is_file(): raise RuntimeError("yt-dlp did not produce the expected audio file.")
    thumbnail = directory / "source.jpg"
    if not thumbnail.is_file():
        raise RuntimeError("No usable thumbnail was supplied. Nothing was imported.")
    tags = {"title": info.get("track") or info.get("title") or "Untitled",
            "artist": info.get("artist") or info.get("creator") or info.get("uploader") or "Unknown",
            "album": info.get("album") or None}
    output = directory / "import.m4a"
    progress(f"Preparing audio and embedding cover: {tags['title']}")
    encoding = finalize_audio(source, thumbnail, output, tags, canceled)
    progress(f"Cover verified · {encoding} · {tags['title']}")
    return str(output), tags
