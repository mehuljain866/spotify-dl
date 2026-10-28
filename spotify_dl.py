#!/usr/bin/env python3
"""
spotify_dl.py — Spotify Playlist Downloader CLI
Downloads songs from a Spotify playlist/album/track link, embeds full ID3 metadata
(title, artist, album, track number, release date, cover art) into audio files.

Key Features:
- Handles playlists of ANY length (100, 200, 500+ tracks) with automatic pagination.
- Smart Incremental Sync: remembers downloaded songs and scans the folder,
  skipping previously downloaded songs so only new additions are downloaded.
- Embedded high-res (640x640) album art and complete ID3v2.3 tags.
- No Spotify developer account / API keys required.

Usage:
    python spotify_dl.py <spotify_url> [options]
    python spotify_dl.py --help
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Ensure stdout/stderr are safe on Windows even under pythonw.exe or CP1252 consoles
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

if sys.platform == "win32":
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def safe_print(*args, **kwargs):
    """Print safely without crashing on Windows cp1252 consoles or pythonw."""
    if sys.stdout is None:
        return
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            msg = " ".join(str(a) for a in args)
            clean = msg.encode("ascii", errors="replace").decode("ascii")
            print(clean, **kwargs)
        except Exception:
            pass

import requests
from bs4 import BeautifulSoup
from mutagen.id3 import (
    APIC, COMM, ID3, ID3NoHeaderError,
    TALB, TCMP, TDRC, TIT2, TPE1, TPE2, TRCK, TPOS
)
from mutagen.mp3 import MP3

try:
    from mutagen.flac import FLAC, Picture
except ImportError:
    FLAC, Picture = None, None

try:
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:
    MP4, MP4Cover = None, None

try:
    from mutagen.oggopus import OggOpus
except ImportError:
    OggOpus = None

try:
    import yt_dlp
except ImportError:
    print("[ERROR] yt-dlp is not installed. Run: pip install yt-dlp")
    sys.exit(1)

# Spotapi enables scraping Spotify's internal GraphQL API with full pagination
try:
    import spotapi
    _SPOTAPI_AVAILABLE = True
except ImportError:
    _SPOTAPI_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# ANSI colour helpers
# ──────────────────────────────────────────────────────────────────────────────

def _c(text: str, code: str) -> str:
    """Wrap text in an ANSI colour code if stdout is a TTY."""
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")


# ──────────────────────────────────────────────────────────────────────────────
# URL and HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_CACHE_FILENAME = ".spotify_dl_cache.json"


def extract_spotify_id(url: str) -> Tuple[str, str]:
    """
    Parse a Spotify URL or URI and return (content_type, spotify_id).
    Supports playlist, album, and track URLs, with or without https:// protocol.
    """
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("spotify:"):
        url = "https://" + url

    # Check spotify URI format: spotify:track:xxxx
    uri_match = re.search(r"spotify:(playlist|album|track):([A-Za-z0-9]+)", url)
    if uri_match:
        return uri_match.group(1), uri_match.group(2)

    # Check open.spotify.com URL format
    pattern = r"open\.spotify\.com/(?:intl-[a-z]+/)?([a-z]+)/([A-Za-z0-9]+)"
    m = re.search(pattern, url)
    if not m:
        raise ValueError(f"Could not parse Spotify URL: {url!r}")
    return m.group(1), m.group(2)


def _get_embed_data(content_type: str, spotify_id: str) -> dict:
    """Fetch the __NEXT_DATA__ JSON from a Spotify embed page."""
    embed_url = f"https://open.spotify.com/embed/{content_type}/{spotify_id}"
    resp = requests.get(embed_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag:
        raise RuntimeError("Could not find __NEXT_DATA__ on embed page")
    return json.loads(script_tag.string)


# ──────────────────────────────────────────────────────────────────────────────
# Spotify Track & Playlist Extractors
# ──────────────────────────────────────────────────────────────────────────────

def fetch_playlist_tracks(playlist_url: str) -> Tuple[dict, List[dict]]:
    """
    Fetch playlist/album/track metadata and all tracks.
    Supports arbitrarily long playlists (>100, >500 tracks) via spotapi pagination.
    Falls back gracefully to embed scraping.
    """
    content_type, spotify_id = extract_spotify_id(playlist_url)

    if content_type == "track":
        return _fetch_single_track(spotify_id)
    if content_type == "album":
        if _SPOTAPI_AVAILABLE:
            try:
                return _fetch_album_spotapi(spotify_id)
            except Exception as exc:
                print(f"  {yellow('Note: Extended album scraper notice, trying fallback:')} {exc}")
        return _fetch_album_tracks(spotify_id)

    # 1. Primary method: spotapi with pagination (handles 100+ songs without API keys)
    if _SPOTAPI_AVAILABLE:
        try:
            return _fetch_playlist_spotapi(spotify_id)
        except Exception as exc:
            print(f"  {yellow('Note: Extended scraper encountered notice, trying fallback:')} {exc}")

    # 2. Fallback: Spotify Embed page scraper
    return _fetch_playlist_embed(spotify_id)


def _fetch_playlist_spotapi(playlist_id: str) -> Tuple[dict, List[dict]]:
    """
    Fetches all tracks using spotapi's GraphQL pagination.
    Can paginate 100, 200, 500+ tracks reliably.
    """
    p = spotapi.PublicPlaylist(playlist_id)
    info = p.get_playlist_info(limit=25)
    pl_data = info.get("data", {}).get("playlistV2", {})
    pl_name = pl_data.get("name", "Unknown Playlist")
    pl_desc = pl_data.get("description", "")

    # Extract playlist cover
    cover_url = None
    images_val = pl_data.get("images")
    if isinstance(images_val, dict):
        items = images_val.get("items", [])
        if items and "sources" in items[0] and items[0]["sources"]:
            cover_url = items[0]["sources"][0].get("url")
    elif isinstance(images_val, list) and images_val:
        cover_url = images_val[0].get("url")

    tracks: List[dict] = []
    seen_ids: Set[str] = set()
    track_num = 1

    for chunk in p.paginate_playlist():
        for item in chunk.get("items", []):
            data = item.get("itemV2", {}).get("data", {})
            if not data or data.get("__typename") != "Track":
                continue

            uri = data.get("uri", "")
            t_id = uri.split(":")[-1] if uri else ""
            if t_id and t_id in seen_ids:
                continue
            if t_id:
                seen_ids.add(t_id)

            title = data.get("name", "Unknown")
            artists_list = [
                a["profile"]["name"]
                for a in data.get("artists", {}).get("items", [])
                if "profile" in a
            ]
            artist = ", ".join(artists_list) if artists_list else "Unknown"

            album = data.get("albumOfTrack", {})
            album_name = album.get("name", "")
            album_artists = [
                a["profile"]["name"]
                for a in album.get("artists", {}).get("items", [])
                if "profile" in a
            ]
            album_artist = ", ".join(album_artists) if album_artists else (artists_list[0] if artists_list else "Unknown")
            release_date = album.get("date", {}).get("isoString", "")
            covers = album.get("coverArt", {}).get("sources", [])
            track_cover = None
            if covers:
                best = max(covers, key=lambda c: c.get("width") or 0)
                track_cover = best.get("url")
                # Upgrade Spotify 300px thumbnail hash to 640px high-res
                if track_cover and "00001e02" in track_cover:
                    track_cover = track_cover.replace("00001e02", "0000b273")

            t_no = data.get("trackNumber") or track_num
            disc_no = data.get("discNumber") or 1
            dur = data.get("trackDuration", {}).get("totalMilliseconds", 0)

            tracks.append({
                "title": title,
                "artist": artist,
                "album_artist": album_artist,
                "album_name": album_name,
                "release_date": release_date[:10] if release_date else "",
                "cover_url": track_cover or cover_url,
                "track_number": t_no,
                "disc_number": disc_no,
                "duration_ms": dur,
                "track_id": t_id,
                "playlist_pos": track_num,
            })
            track_num += 1

    playlist_info = {
        "name": pl_name,
        "description": pl_desc,
        "cover_url": cover_url,
    }
    return playlist_info, tracks


def _fetch_playlist_embed(playlist_id: str) -> Tuple[dict, List[dict]]:
    """Embed-page fallback for playlists (first 50 tracks)."""
    data = _get_embed_data("playlist", playlist_id)
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]

    playlist_info = {
        "name": entity.get("title") or entity.get("name", "Unknown Playlist"),
        "description": entity.get("subtitle", ""),
        "cover_url": (
            entity.get("coverArt", {}).get("sources", [{}])[-1].get("url")
        ),
    }

    raw_tracks = entity.get("trackList", [])
    tracks: List[dict] = []
    for idx, t in enumerate(raw_tracks, start=1):
        if not t.get("title"):
            continue
        uri = t.get("uri", "")
        track_id = uri.split(":")[-1] if uri else ""
        t_artist = t.get("subtitle", "Unknown")
        tracks.append({
            "title": t.get("title", "Unknown"),
            "artist": t_artist,
            "album_artist": t_artist.split(",")[0].strip() if t_artist else "Unknown",
            "cover_url": None,
            "track_number": idx,
            "disc_number": 1,
            "album_name": None,
            "release_date": None,
            "duration_ms": t.get("duration", 0),
            "track_id": track_id,
            "playlist_pos": idx,
        })

    return playlist_info, tracks


def _fetch_single_track(track_id: str) -> Tuple[dict, List[dict]]:
    """Fetch metadata for a single Spotify track."""
    data = _get_embed_data("track", track_id)
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]

    artists = [a["name"] for a in entity.get("artists", [])]
    artist_str = ", ".join(artists) if artists else "Unknown"

    vi = entity.get("visualIdentity", {})
    images = vi.get("image", [])
    cover_url = None
    if images:
        best = max(images, key=lambda x: x.get("maxWidth", 0))
        cover_url = best.get("url")

    track_info = {
        "title": entity.get("title", "Unknown"),
        "artist": artist_str,
        "album_artist": artists[0] if artists else "Unknown",
        "cover_url": cover_url,
        "track_number": 1,
        "disc_number": 1,
        "album_name": entity.get("title", "Single"),
        "release_date": (entity.get("releaseDate") or {}).get("isoString", "")[:10],
        "duration_ms": entity.get("duration", 0),
        "track_id": track_id,
        "playlist_pos": 1,
    }
    playlist_info = {
        "name": track_info["title"],
        "description": "",
        "cover_url": cover_url,
    }
    return playlist_info, [track_info]


def _fetch_album_tracks(album_id: str) -> Tuple[dict, List[dict]]:
    """Fetch track list for a Spotify album."""
    data = _get_embed_data("album", album_id)
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]

    album_name = entity.get("title") or entity.get("name", "Unknown Album")
    album_artists_raw = entity.get("artists") or []
    if album_artists_raw:
        album_artist = ", ".join(a.get("name", "") for a in album_artists_raw if a.get("name"))
    else:
        album_artist = entity.get("subtitle") or "Unknown"

    rd_val = entity.get("releaseDate")
    if isinstance(rd_val, dict):
        release_date = rd_val.get("isoString", "")[:10]
    elif isinstance(rd_val, str):
        release_date = rd_val[:10]
    else:
        release_date = ""

    cover_url = None
    vi_images = entity.get("visualIdentity", {}).get("image", [])
    if vi_images:
        best_img = max(vi_images, key=lambda x: x.get("maxWidth", 0))
        cover_url = best_img.get("url")
    elif entity.get("coverArt", {}).get("sources"):
        cover_url = entity["coverArt"]["sources"][-1]["url"]

    if cover_url and "00001e02" in cover_url:
        cover_url = cover_url.replace("00001e02", "0000b273")

    raw_tracks = entity.get("trackList", [])
    tracks: List[dict] = []
    for idx, t in enumerate(raw_tracks, start=1):
        if not t.get("title"):
            continue
        uri = t.get("uri", "")
        track_id = uri.split(":")[-1] if uri else ""
        t_artist = t.get("subtitle") or album_artist
        tracks.append({
            "title": t.get("title", "Unknown"),
            "artist": t_artist,
            "album_artist": album_artist,
            "cover_url": cover_url,
            "track_number": idx,
            "disc_number": 1,
            "album_name": album_name,
            "release_date": release_date,
            "duration_ms": t.get("duration", 0),
            "track_id": track_id,
            "playlist_pos": idx,
        })

    playlist_info = {
        "name": album_name,
        "description": f"By {album_artist}",
        "cover_url": cover_url,
    }
    return playlist_info, tracks


def _fetch_album_spotapi(album_id: str) -> Tuple[dict, List[dict]]:
    """
    Fetches all tracks for an album using spotapi's GraphQL endpoint.
    Guarantees album_artist is set to the primary creator across all tracks.
    """
    a = spotapi.PublicAlbum(album_id)
    info = a.get_album_info(limit=343)
    alb = info.get("data", {}).get("albumUnion", {})
    if not alb or alb.get("__typename") != "Album":
        raise RuntimeError(f"Album not found or unavailable in this region (type: {alb.get('__typename') if alb else 'None'})")

    album_name = alb.get("name", "Unknown Album")
    album_artists = [
        x["profile"]["name"]
        for x in alb.get("artists", {}).get("items", [])
        if "profile" in x and "name" in x["profile"]
    ]
    album_artist = ", ".join(album_artists) if album_artists else "Unknown"

    rd_val = alb.get("date")
    if isinstance(rd_val, dict):
        release_date = rd_val.get("isoString", "")[:10]
    elif isinstance(rd_val, str):
        release_date = rd_val[:10]
    else:
        release_date = ""

    cover_url = None
    covers = alb.get("coverArt", {}).get("sources", [])
    if covers:
        best_cov = max(covers, key=lambda c: c.get("width") or 0)
        cover_url = best_cov.get("url")
        if cover_url and "00001e02" in cover_url:
            cover_url = cover_url.replace("00001e02", "0000b273")

    raw_items = alb.get("tracksV2", {}).get("items", [])
    tracks: List[dict] = []
    for idx, item in enumerate(raw_items, start=1):
        tr = item.get("track", {})
        if not tr or not tr.get("name"):
            continue

        tr_artists = [
            x["profile"]["name"]
            for x in tr.get("artists", {}).get("items", [])
            if "profile" in x and "name" in x["profile"]
        ]
        track_artist = ", ".join(tr_artists) if tr_artists else album_artist

        uri = tr.get("uri", "")
        track_id = uri.split(":")[-1] if uri else ""
        t_no = tr.get("trackNumber") or idx
        disc_no = tr.get("discNumber") or 1
        dur = tr.get("duration", {}).get("totalMilliseconds", 0) if isinstance(tr.get("duration"), dict) else tr.get("duration", 0)

        tracks.append({
            "title": tr.get("name", "Unknown"),
            "artist": track_artist,
            "album_artist": album_artist,
            "album_name": album_name,
            "release_date": release_date,
            "cover_url": cover_url,
            "track_number": t_no,
            "disc_number": disc_no,
            "duration_ms": dur,
            "track_id": track_id,
            "playlist_pos": idx,
        })

    playlist_info = {
        "name": album_name,
        "description": f"By {album_artist}",
        "cover_url": cover_url,
    }
    return playlist_info, tracks


def enrich_track_metadata(track: dict) -> dict:
    """
    Enriches track metadata with album art and release date if missing.
    """
    track_id = track.get("track_id")
    if not track_id:
        return track

    try:
        data = _get_embed_data("track", track_id)
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]

        if not track.get("release_date"):
            rd = (entity.get("releaseDate") or {}).get("isoString", "")
            if rd:
                track["release_date"] = rd[:10]

        if not track.get("cover_url"):
            vi = entity.get("visualIdentity", {})
            images = vi.get("image", [])
            if images:
                best = max(images, key=lambda x: x.get("maxWidth", 0))
                track["cover_url"] = best["url"]
    except Exception:
        pass

    return track


# ──────────────────────────────────────────────────────────────────────────────
# Filesystem & Smart Duplicate Detection
# ──────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str, max_len: int = 180) -> str:
    """Sanitize strings for Windows / POSIX file paths."""
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len]


def normalize_match_key(text: str) -> str:
    """Normalize text for fuzzy title / artist matching."""
    text = unicodedata.normalize("NFKD", text).lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_output_path(output_dir: Path, track: dict, fmt: str = "mp3") -> Path:
    """Generate canonical output path: '{Artist} - {Title}.{ext}'."""
    artist = sanitize_filename(track.get("artist", "Unknown"))
    title = sanitize_filename(track.get("title", "Unknown"))
    return output_dir / f"{artist} - {title}.{fmt}"


class DownloadTracker:
    """
    Tracks downloaded files using two layers:
    1. Persistent JSON cache (.spotify_dl_cache.json) mapping track_id -> file info.
    2. Real-time folder scan index matching filenames and audio tags on disk.
    Thread-safe for concurrent downloads.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.cache_file = output_dir / _CACHE_FILENAME
        self.lock = threading.Lock()
        self.cache: Dict[str, dict] = self._load_cache()
        self.disk_files_by_name: Dict[str, Path] = {}
        self.disk_files_by_match: Dict[Tuple[str, str], Path] = {}
        self._scan_folder()

    def _load_cache(self) -> Dict[str, dict]:
        if self.cache_file.exists():
            try:
                with self.cache_file.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_cache(self) -> None:
        with self.lock:
            try:
                tmp = self.cache_file.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(self.cache, f, indent=2, ensure_ascii=False)
                tmp.replace(self.cache_file)
            except Exception:
                pass

    def _scan_folder(self) -> None:
        """Scan folder for any already-existing audio files."""
        if not self.output_dir.exists():
            return
        audio_exts = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".ogg"}
        try:
            for item in self.output_dir.iterdir():
                if item.is_file() and item.suffix.lower() in audio_exts:
                    # Ignore 0-byte or corrupt downloads (< 50KB)
                    if item.stat().st_size > 50000:
                        self.disk_files_by_name[item.name.lower()] = item
                        stem = item.stem
                        if " - " in stem:
                            parts = stem.split(" - ", 1)
                            a_norm = normalize_match_key(parts[0])
                            t_norm = normalize_match_key(parts[1])
                            self.disk_files_by_match[(t_norm, a_norm)] = item
                            self.disk_files_by_match[(a_norm, t_norm)] = item
        except Exception:
            pass

    def is_already_downloaded(self, track: dict, fmt: str = "mp3") -> Tuple[bool, Optional[Path]]:
        """
        Check if a track is already present in this folder.
        Checks:
        1. Cache record (by Spotify track_id)
        2. Expected output file on disk
        3. Fuzzy match of artist and title in the folder
        """
        t_id = track.get("track_id", "")
        expected_path = make_output_path(self.output_dir, track, fmt)

        # 1. Direct cache check
        if t_id and t_id in self.cache:
            entry_file = self.output_dir / self.cache[t_id].get("file", "")
            if entry_file.exists() and entry_file.stat().st_size > 50000:
                return True, entry_file

        # 2. Direct expected filename check
        if expected_path.exists() and expected_path.stat().st_size > 50000:
            self.mark_downloaded(track, expected_path)
            return True, expected_path

        # 3. Check lowercase filename in folder
        low_name = expected_path.name.lower()
        if low_name in self.disk_files_by_name:
            match_file = self.disk_files_by_name[low_name]
            if match_file.exists() and match_file.stat().st_size > 50000:
                self.mark_downloaded(track, match_file)
                return True, match_file

        # 4. Fuzzy title + artist match
        t_norm = normalize_match_key(track.get("title", ""))
        a_norm = normalize_match_key(track.get("artist", ""))
        # Compare first artist or full artist string
        for a_candidate in [a_norm, a_norm.split(",")[0].strip()]:
            if (t_norm, a_candidate) in self.disk_files_by_match:
                match_file = self.disk_files_by_match[(t_norm, a_candidate)]
                if match_file.exists() and match_file.stat().st_size > 50000:
                    self.mark_downloaded(track, match_file)
                    return True, match_file

        return False, None

    def mark_downloaded(self, track: dict, file_path: Path) -> None:
        """Register a track as downloaded in cache and memory index."""
        t_id = track.get("track_id", "")
        with self.lock:
            if t_id:
                self.cache[t_id] = {
                    "file": file_path.name,
                    "title": track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "downloaded_at": int(time.time()),
                }
            self.disk_files_by_name[file_path.name.lower()] = file_path


# ──────────────────────────────────────────────────────────────────────────────
# Audio Download & ID3 Metadata Tagging
# ──────────────────────────────────────────────────────────────────────────────

def build_search_query(track: dict) -> str:
    """Build a search query for yt-dlp to find the best audio stream."""
    artist = track.get("artist", "")
    title = track.get("title", "")
    return f"{artist} - {title} audio"


def download_track_audio(
    track: dict,
    output_path: Path,
    fmt: str = "mp3",
    bitrate: str = "320k",
    verbose: bool = False,
) -> bool:
    """
    Downloads audio using yt-dlp with FFmpeg audio conversion.
    """
    query = build_search_query(track)
    temp_tmpl = str(output_path.with_suffix(".%(ext)s"))

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": temp_tmpl,
        "quiet": not verbose,
        "no_warnings": not verbose,
        "default_search": "ytsearch1",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": bitrate.replace("k", ""),
            }
        ],
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([query])
        return output_path.exists() and output_path.stat().st_size > 50000
    except Exception as e:
        if verbose:
            print(f"    {red('yt-dlp error:')} {e}")
        return False


def download_image_bytes(url: Optional[str]) -> Optional[bytes]:
    """Download cover art image from URL."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def embed_id3_metadata(audio_path: Path, track: dict, cover_bytes: Optional[bytes]) -> bool:
    """
    Embed metadata & cover art into audio files (MP3, FLAC, M4A, Opus, etc.)
    for full compatibility with media players and file explorers (iTunes, Apple Music, VLC).
    Ensures Album Artist (TPE2 / aART / albumartist) is correctly set so albums
    with featured guest singers stay properly grouped into a single album in Apple Music/iTunes.
    """
    ext = audio_path.suffix.lower()

    def _clean(val: Any) -> str:
        if val is None:
            return ""
        return str(val).replace("\xa0", " ").strip()

    title = _clean(track.get("title", ""))
    artist = _clean(track.get("artist", ""))
    album = _clean(track.get("album_name") or title)

    raw_aa = track.get("album_artist")
    if raw_aa:
        album_artist = _clean(raw_aa)
    elif artist:
        album_artist = artist.split(",")[0].strip()
    else:
        album_artist = "Unknown"

    track_num = track.get("track_number", 1)
    disc_num = track.get("disc_number", 1)
    release = _clean(track.get("release_date", ""))

    is_compilation = bool(
        track.get("is_compilation")
        or album_artist.lower() in ("various artists", "soundtrack")
        or "soundtrack" in album.lower()
    )

    try:
        if ext == ".mp3":
            audio = MP3(str(audio_path), ID3=ID3)
            try:
                audio.add_tags()
            except Exception:
                pass

            tags = audio.tags
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))
            tags.add(TPE2(encoding=3, text=album_artist))
            tags.add(TRCK(encoding=3, text=str(track_num)))
            tags.add(TPOS(encoding=3, text=str(disc_num)))
            if is_compilation:
                tags.add(TCMP(encoding=3, text="1"))
            if release:
                tags.add(TDRC(encoding=3, text=release[:10]))
            tags.add(COMM(
                encoding=3, lang="eng", desc="Comment",
                text="Downloaded with spotify-dl"
            ))

            if cover_bytes:
                mime = "image/jpeg"
                if cover_bytes[:4] == b"\x89PNG":
                    mime = "image/png"
                tags.add(APIC(
                    encoding=3,
                    mime=mime,
                    type=3,  # front cover
                    desc="Cover",
                    data=cover_bytes,
                ))

            audio.save(v2_version=3)
            return True

        elif ext == ".flac" and FLAC:
            audio = FLAC(str(audio_path))
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if album_artist:
                audio["albumartist"] = album_artist
            if is_compilation:
                audio["compilation"] = "1"
            audio["tracknumber"] = str(track_num)
            audio["discnumber"] = str(disc_num)
            if release:
                audio["date"] = release[:10]
            if cover_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/png" if cover_bytes[:4] == b"\x89PNG" else "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_bytes
                audio.add_picture(pic)
            audio.save()
            return True

        elif ext in (".m4a", ".mp4") and MP4:
            audio = MP4(str(audio_path))
            audio["\xa9nam"] = [title]
            audio["\xa9ART"] = [artist]
            audio["\xa9alb"] = [album]
            if album_artist:
                audio["aART"] = [album_artist]
            if is_compilation:
                audio["cpil"] = True
            try:
                audio["trkn"] = [(int(track_num), 0)]
            except Exception:
                pass
            try:
                audio["disk"] = [(int(disc_num), 0)]
            except Exception:
                pass
            if release:
                audio["\xa9day"] = [release[:10]]
            if cover_bytes:
                img_fmt = MP4Cover.FORMAT_PNG if cover_bytes[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(cover_bytes, imageformat=img_fmt)]
            audio.save()
            return True

        elif ext in (".opus", ".ogg") and OggOpus:
            audio = OggOpus(str(audio_path))
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if album_artist:
                audio["albumartist"] = album_artist
            audio["tracknumber"] = str(track_num)
            if release:
                audio["date"] = release[:10]
            audio.save()
            return True

        return True
    except Exception as e:
        print(f"    {yellow('Warning: could not embed tags:')} {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Audio Transcoding & Downsizing (Quality Adjustment)
# ──────────────────────────────────────────────────────────────────────────────

def get_ffmpeg_executable() -> Optional[str]:
    """Find ffmpeg in PATH or common Windows / WinGet install locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            winget_pkgs = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
            if winget_pkgs.exists():
                try:
                    for f in winget_pkgs.glob("**/ffmpeg.exe"):
                        return str(f)
                except Exception:
                    pass
            for f in (Path(local_app) / "Programs").glob("**/ffmpeg.exe"):
                return str(f)
        prog_files = Path(os.environ.get("ProgramFiles", "C:\\Program Files"))
        if prog_files.exists():
            try:
                for f in prog_files.glob("**/ffmpeg.exe"):
                    return str(f)
            except Exception:
                pass
    return None


def transcode_audio_file(
    src_file: Path,
    dest_file: Path,
    target_bitrate: str = "192k",
    target_fmt: Optional[str] = None,
) -> bool:
    """
    Transcode an audio file to a target bitrate / format while preserving
    all ID3 / Vorbis / MP4 metadata and embedded album art.
    """
    ffmpeg_exe = get_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError("FFmpeg is required for audio downsizing but was not found.")

    out_ext = f".{target_fmt.lower().strip('.')}" if target_fmt else src_file.suffix.lower()
    temp_out = dest_file.with_name(f"{dest_file.stem}_transcoding_{int(time.time()*1000)}.tmp{out_ext}")

    try:
        # Run ffmpeg transcode with target bitrate
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i", str(src_file),
            "-vn",
            "-b:a", target_bitrate,
            str(temp_out),
        ]
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        p = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs
        )
        if p.returncode != 0 or not temp_out.exists() or temp_out.stat().st_size == 0:
            if temp_out.exists():
                try:
                    temp_out.unlink()
                except Exception:
                    pass
            return False

        # Copy metadata & album art
        src_ext = src_file.suffix.lower()
        if src_ext == ".mp3" and out_ext == ".mp3":
            try:
                from mutagen.id3 import ID3
                tags = ID3(str(src_file))
                tags.save(str(temp_out), v2_version=3)
            except Exception:
                pass
        elif src_ext == ".flac" and out_ext == ".flac" and FLAC:
            try:
                tags = FLAC(str(src_file))
                tags.save(str(temp_out))
            except Exception:
                pass
        elif src_ext in (".m4a", ".mp4") and out_ext in (".m4a", ".mp4") and MP4:
            try:
                tags = MP4(str(src_file))
                tags.save(str(temp_out))
            except Exception:
                pass
        else:
            # Cross-format: extract basic tags and cover art, then re-embed
            try:
                cover_bytes = None
                track_info = {}
                if src_ext == ".mp3":
                    from mutagen.id3 import ID3
                    id3 = ID3(str(src_file))
                    track_info["title"] = str(id3.get("TIT2", ""))
                    track_info["artist"] = str(id3.get("TPE1", ""))
                    track_info["album_name"] = str(id3.get("TALB", ""))
                    for k in id3.keys():
                        if k.startswith("APIC"):
                            cover_bytes = id3[k].data
                            break
                embed_id3_metadata(temp_out, track_info, cover_bytes)
            except Exception:
                pass

        # Atomic replace or move
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.exists():
            temp_out.replace(dest_file)
        else:
            temp_out.rename(dest_file)

        return True
    except Exception:
        if temp_out.exists():
            try:
                temp_out.unlink()
            except Exception:
                pass
        return False


def get_audio_file_bitrate(file_path: Path) -> Optional[int]:
    """
    Returns the bitrate of an audio file in kbps (e.g. 128, 192, 256, 320).
    For lossless audio (FLAC, WAV), returns 1000 to indicate high/lossless quality.
    """
    ext = file_path.suffix.lower()
    if ext in (".flac", ".wav"):
        return 1000
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if audio and hasattr(audio, "info") and hasattr(audio.info, "bitrate") and audio.info.bitrate:
            return round(audio.info.bitrate / 1000)
    except Exception:
        pass
    return None


def analyze_folder_audio(folder: Path) -> dict:
    """
    Scans a folder and determines total audio files, total size,
    and current detected audio bitrates.
    """
    if not folder.exists():
        return {
            "total_files": 0,
            "total_bytes": 0,
            "total_mb": 0.0,
            "max_bitrate": 0,
            "min_bitrate": 0,
            "avg_bitrate": 0,
            "is_lossless": False,
        }

    audio_exts = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".ogg"}
    try:
        files = [
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in audio_exts and f.stat().st_size > 50000
        ]
    except Exception:
        files = []

    if not files:
        return {
            "total_files": 0,
            "total_bytes": 0,
            "total_mb": 0.0,
            "max_bitrate": 0,
            "min_bitrate": 0,
            "avg_bitrate": 0,
            "is_lossless": False,
        }

    total_bytes = sum(f.stat().st_size for f in files)
    total_mb = round(total_bytes / (1024 * 1024), 2)
    bitrates = []
    has_lossless = False

    for f in files:
        br = get_audio_file_bitrate(f)
        if br == 1000:
            has_lossless = True
            bitrates.append(320)
        elif br:
            bitrates.append(br)

    max_br = max(bitrates) if bitrates else 256
    min_br = min(bitrates) if bitrates else 256
    avg_br = round(sum(bitrates) / len(bitrates)) if bitrates else 256

    return {
        "total_files": len(files),
        "total_bytes": total_bytes,
        "total_mb": total_mb,
        "max_bitrate": max_br,
        "min_bitrate": min_br,
        "avg_bitrate": avg_br,
        "is_lossless": has_lossless,
    }


def get_reducible_bitrate_options(current_max_kbps: int, total_mb: float = 0.0) -> List[Tuple[str, str, int]]:
    """
    Returns only tiers that are strictly lower than the current bitrate.
    Calculates the actual estimated percentage and MB reduction.
    Format: (raw_bitrate_str, formatted_label, tier_kbps)
    """
    standard_tiers = [
        (320, "320k", "Maximum Quality"),
        (256, "256k", "High Quality"),
        (192, "192k", "Standard"),
        (160, "160k", "Compact"),
        (128, "128k", "Smallest"),
        (96,  "96k",  "Ultra-light"),
    ]

    baseline = max(current_max_kbps, 128)
    results = []

    for tier_kbps, tier_str, tier_name in standard_tiers:
        # Only include tiers that are strictly lower than current max
        if tier_kbps < current_max_kbps:
            pct_saved = round((1.0 - (tier_kbps / baseline)) * 100)
            if total_mb > 0:
                mb_saved = total_mb * (pct_saved / 100.0)
                if mb_saved >= 1024:
                    saved_str = f"~{mb_saved / 1024:.2f} GB"
                else:
                    saved_str = f"~{mb_saved:.0f} MB"
                label = f"{tier_str} ({tier_name} / Save ~{pct_saved}% [{saved_str}])"
            else:
                label = f"{tier_str} ({tier_name} / Save ~{pct_saved}%)"
            results.append((tier_str, label, tier_kbps))

    return results


def batch_transcode_folder(
    folder: Path,
    target_bitrate: str = "192k",
    target_fmt: Optional[str] = None,
    in_place: bool = True,
    output_dir: Optional[Path] = None,
    max_workers: int = 4,
    callback: Optional[Callable[[str, float, dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Batch transcode all audio files in a folder to a target bitrate / format.
    Frees disk space while preserving all tags and cover artwork.
    Automatically skips files that are already at or below target bitrate.
    """
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")

    audio_exts = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".ogg"}
    audio_files: List[Path] = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in audio_exts and f.stat().st_size > 50000
    ]

    total_files = len(audio_files)
    if total_files == 0:
        return {
            "total_files": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "original_bytes": 0,
            "new_bytes": 0,
            "saved_bytes": 0,
            "cancelled": False,
        }

    initial_total_bytes = sum(f.stat().st_size for f in audio_files)
    processed_count = 0
    skipped_count = 0
    failed_count = 0
    progress_lock = threading.Lock()

    m_target = re.search(r"\d+", target_bitrate)
    target_kbps = int(m_target.group()) if m_target else 192

    def _notify(status: str, prog: float, stats: dict):
        if callback:
            try:
                callback(status, prog, stats)
            except Exception:
                pass

    _notify(f"Found {total_files} audio files. Starting smart downsizing...", 0.0, {
        "processed": 0, "skipped": 0, "failed": 0, "total": total_files, "saved_bytes": 0
    })

    def _transcode_worker(idx: int, file_path: Path):
        nonlocal processed_count, skipped_count, failed_count
        if cancel_check and cancel_check():
            return False

        # Smart check: if file is already at or below target bitrate (and format matches), skip it
        file_br = get_audio_file_bitrate(file_path)
        format_match = (target_fmt is None or target_fmt.lower().strip(".") == file_path.suffix.lower().strip("."))
        if file_br is not None and file_br <= target_kbps and format_match:
            with progress_lock:
                skipped_count += 1
                done = processed_count + skipped_count + failed_count
                _notify(f"[{done}/{total_files}] Skipped: {file_path.name} (already {file_br}k <= {target_bitrate})", done / total_files, {
                    "processed": processed_count, "skipped": skipped_count, "failed": failed_count, "total": total_files
                })
            return True

        with progress_lock:
            done = processed_count + skipped_count + failed_count
            _notify(f"[{done+1}/{total_files}] Downsizing: {file_path.name}", done / total_files, {
                "processed": processed_count, "skipped": skipped_count, "failed": failed_count, "total": total_files
            })

        out_ext = f".{target_fmt.lower().strip('.')}" if target_fmt else file_path.suffix.lower()
        if in_place or output_dir is None:
            dest_file = file_path.with_suffix(out_ext)
        else:
            dest_file = output_dir / f"{file_path.stem}{out_ext}"

        ok = transcode_audio_file(
            src_file=file_path,
            dest_file=dest_file,
            target_bitrate=target_bitrate,
            target_fmt=target_fmt,
        )

        with progress_lock:
            if ok:
                processed_count += 1
            else:
                failed_count += 1
            done = processed_count + skipped_count + failed_count
            _notify(f"[{done}/{total_files}] Completed: {file_path.name}", done / total_files, {
                "processed": processed_count, "skipped": skipped_count, "failed": failed_count, "total": total_files
            })
        return ok

    workers = max(1, min(max_workers, total_files))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_transcode_worker, idx, f): f
            for idx, f in enumerate(audio_files)
        }
        for future in concurrent.futures.as_completed(future_map):
            if cancel_check and cancel_check():
                executor.shutdown(wait=False, cancel_futures=True)
                break

    # Calculate final sizes
    final_files = [
        f for f in (output_dir if (output_dir and not in_place) else folder).iterdir()
        if f.is_file() and f.suffix.lower() in audio_exts
    ]
    final_total_bytes = sum(f.stat().st_size for f in final_files)
    saved_bytes = max(0, initial_total_bytes - final_total_bytes)

    result = {
        "total_files": total_files,
        "processed": processed_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "original_bytes": initial_total_bytes,
        "new_bytes": final_total_bytes,
        "saved_bytes": saved_bytes,
        "cancelled": bool(cancel_check and cancel_check()),
    }

    _notify("Downsizing complete!", 1.0, result)
    return result


def fix_library_album_tags(folder: Path, callback=None) -> dict:
    """
    Scans an audio folder and fixes fragmented/split albums for iTunes & Apple Music.
    Unifies the Album Artist tag (TPE2 / aART / albumartist) across all tracks belonging
    to each album and applies the iTunes compilation flag where appropriate.
    Zero audio re-encoding: only updates tag headers instantaneously.
    """
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")

    from collections import defaultdict, Counter

    audio_files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".mp4", ".flac")
    ]

    total_files = len(audio_files)
    if total_files == 0:
        return {"total_files": 0, "fixed_files": 0, "albums_fixed": 0, "fixed_albums_list": []}

    def _notify(status: str, prog: float):
        if callback:
            try:
                callback(status, prog)
            except Exception:
                pass

    _notify(f"Scanning {total_files} files for album grouping...", 0.0)

    # Group files by Album Title
    albums_map = defaultdict(list)
    for idx, f in enumerate(audio_files):
        ext = f.suffix.lower()
        try:
            if ext == ".mp3":
                id3 = ID3(str(f))
                alb = str(id3.get("TALB", "")).replace("\xa0", " ").strip()
                art = str(id3.get("TPE1", "")).replace("\xa0", " ").strip()
                tpe2 = str(id3.get("TPE2", "")).replace("\xa0", " ").strip()
                if alb:
                    albums_map[alb].append({
                        "path": f, "ext": ext, "id3": id3,
                        "album": alb, "artist": art, "album_artist": tpe2
                    })
            elif ext in (".m4a", ".mp4") and MP4:
                mp4 = MP4(str(f))
                alb = str(mp4.get("\xa9alb", [""])[0]).replace("\xa0", " ").strip()
                art = str(mp4.get("\xa9ART", [""])[0]).replace("\xa0", " ").strip()
                aart = str(mp4.get("aART", [""])[0]).replace("\xa0", " ").strip()
                if alb:
                    albums_map[alb].append({
                        "path": f, "ext": ext, "mp4": mp4,
                        "album": alb, "artist": art, "album_artist": aart
                    })
            elif ext == ".flac" and FLAC:
                fl = FLAC(str(f))
                alb = str(fl.get("album", [""])[0]).replace("\xa0", " ").strip()
                art = str(fl.get("artist", [""])[0]).replace("\xa0", " ").strip()
                albart = str(fl.get("albumartist", [""])[0]).replace("\xa0", " ").strip()
                if alb:
                    albums_map[alb].append({
                        "path": f, "ext": ext, "flac": fl,
                        "album": alb, "artist": art, "album_artist": albart
                    })
        except Exception:
            pass

    fixed_files = 0
    fixed_albums_list = []

    for alb_name, track_list in albums_map.items():
        if not alb_name or len(track_list) == 0:
            continue

        lead_artists = [
            t["artist"].split(",")[0].strip()
            for t in track_list
            if t["artist"]
        ]
        if not lead_artists:
            continue

        lead_counts = Counter(lead_artists)
        top_lead, count = lead_counts.most_common(1)[0]

        is_ost = (
            "soundtrack" in alb_name.lower()
            or "various" in alb_name.lower()
            or (len(lead_counts) > 2 and count < len(track_list) / 2)
        )
        unified_album_artist = "Various Artists" if is_ost else top_lead

        album_needed_fix = False
        for t in track_list:
            cur_aa = t.get("album_artist", "")
            needs_update = (
                cur_aa != unified_album_artist
                or "\xa0" in t["artist"]
                or "\xa0" in cur_aa
            )
            if not needs_update:
                continue

            album_needed_fix = True
            ext = t["ext"]
            try:
                if ext == ".mp3":
                    id3 = t["id3"]
                    id3.add(TPE2(encoding=3, text=unified_album_artist))
                    if id3.get("TPE1"):
                        id3.add(TPE1(encoding=3, text=str(id3.get("TPE1")).replace("\xa0", " ").strip()))
                    if id3.get("TALB"):
                        id3.add(TALB(encoding=3, text=alb_name))
                    if is_ost:
                        id3.add(TCMP(encoding=3, text="1"))
                    id3.save(v2_version=3)
                    fixed_files += 1
                elif ext in (".m4a", ".mp4") and MP4:
                    mp4 = t["mp4"]
                    mp4["aART"] = [unified_album_artist]
                    if is_ost:
                        mp4["cpil"] = True
                    mp4.save()
                    fixed_files += 1
                elif ext == ".flac" and FLAC:
                    fl = t["flac"]
                    fl["albumartist"] = unified_album_artist
                    if is_ost:
                        fl["compilation"] = "1"
                    fl.save()
                    fixed_files += 1
            except Exception:
                pass

        if album_needed_fix:
            fixed_albums_list.append(f"{alb_name} -> {unified_album_artist}")

    _notify(f"Album consolidation complete: {fixed_files} files updated across {len(fixed_albums_list)} albums.", 1.0)

    return {
        "total_files": total_files,
        "fixed_files": fixed_files,
        "albums_fixed": len(fixed_albums_list),
        "fixed_albums_list": fixed_albums_list,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main Pipeline
# ──────────────────────────────────────────────────────────────────────────────

def run_download(
    playlist_url: str,
    output_dir: Path,
    fmt: str = "mp3",
    bitrate: str = "320k",
    limit: Optional[int] = None,
    force_redownload: bool = False,
    verbose: bool = False,
    delay: float = 0.5,
    max_workers: int = 4,
    callback: Optional[Callable[[str, float, Optional[str]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Executes the download workflow:
    1. Fetches full playlist metadata with pagination.
    2. Scans folder & checks cache to identify already-downloaded songs.
    3. Downloads only newly added songs concurrently using a thread pool.
    4. Embeds metadata and album art.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def _notify(status: str, prog: float = -1.0, current_track: Optional[str] = None):
        if callback:
            try:
                callback(status, prog, current_track)
            except Exception:
                pass

    safe_print(f"\n{bold('=== Spotify Playlist Downloader ===')}")
    safe_print(f"  Playlist URL : {cyan(playlist_url)}")
    safe_print(f"  Output folder: {cyan(str(output_dir))}")
    safe_print(f"  Format       : {fmt.upper()} @ {bitrate}")
    safe_print(f"  Threads      : {max_workers} concurrent downloads\n")

    _notify("Connecting to Spotify & fetching playlist tracks...", 0.02, None)

    # 1. Fetch playlist metadata & tracks
    safe_print(f"  {bold('Fetching playlist tracks...')}", end="", flush=True)
    try:
        playlist_info, all_tracks = fetch_playlist_tracks(playlist_url)
    except Exception as e:
        err_msg = f"Failed to fetch tracks: {e}"
        safe_print(f"\n  {red('ERROR:')} {err_msg}")
        _notify(err_msg, 0.0, None)
        raise e

    if cancel_check and cancel_check():
        _notify("Cancelled by user.", 0.0, None)
        return {"downloaded": 0, "skipped": 0, "failed": 0, "total": 0, "cancelled": True}

    total_on_spotify = len(all_tracks)
    safe_print(f" {green('OK')}")
    safe_print(f"  Playlist : {bold(playlist_info['name'])}")
    safe_print(f"  Tracks   : {bold(str(total_on_spotify))} tracks found on Spotify\n")

    _notify(f"Found {total_on_spotify} tracks in '{playlist_info['name']}'. Checking local folder...", 0.05, None)

    if limit and limit < total_on_spotify:
        all_tracks = all_tracks[:limit]
        safe_print(f"  (Limited to first {limit} tracks)\n")

    # 2. Smart incremental check
    _notify(f"Checking existing files in {output_dir.name}...", 0.08, None)
    tracker = DownloadTracker(output_dir)

    to_download: List[dict] = []
    already_present: List[Tuple[dict, Path]] = []

    for track in all_tracks:
        if not force_redownload:
            is_present, existing_file = tracker.is_already_downloaded(track, fmt=fmt)
            if is_present and existing_file:
                already_present.append((track, existing_file))
                continue
        to_download.append(track)

    tracker.save_cache()

    skipped_count = len(already_present)
    new_count = len(to_download)

    if skipped_count > 0:
        safe_print(f"  {yellow(f'[*] {skipped_count} tracks already downloaded in this folder - skipping.')}")
        if new_count > 0:
            safe_print(f"  {green(f'[+] {new_count} new track(s) to download.')}\n")
            _notify(f"{skipped_count} already up to date. Downloading {new_count} new track(s)...", 0.1, None)
        else:
            msg = f"All {skipped_count} tracks are already downloaded and up to date!"
            safe_print(f"\n  {green(f'[OK] {msg}')}")
            safe_print(f"  Folder: {cyan(str(output_dir))}\n")
            _notify(msg, 1.0, None)
            return {
                "downloaded": 0,
                "skipped": skipped_count,
                "failed": 0,
                "total": total_on_spotify,
                "playlist_name": playlist_info.get("name", "Playlist"),
                "output_dir": str(output_dir),
            }

    # 3. Download newly added tracks concurrently
    downloaded_count = 0
    failed_count = 0
    progress_lock = threading.Lock()

    workers = max(1, min(max_workers, new_count))
    _notify(f"Downloading {new_count} tracks ({workers} at a time)...", 0.1, None)
    safe_print(f"  [*] Downloading {new_count} songs with {workers} parallel threads...\n")

    def _worker(item_idx: int, track: dict):
        nonlocal downloaded_count, failed_count
        if cancel_check and cancel_check():
            return False

        title = track.get("title", "Unknown")
        artist = track.get("artist", "Unknown")
        track_label = f"{artist} - {title}"

        with progress_lock:
            curr_done = downloaded_count + failed_count
            _notify(f"Downloading: {track_label}", curr_done / max(1, new_count), track_label)
        safe_print(f"  [{item_idx:>3}/{new_count}] Starting: {bold(title)} - {artist}")

        out_path = make_output_path(output_dir, track, fmt=fmt)

        # Enrich cover art if missing
        if not track.get("cover_url"):
            track = enrich_track_metadata(track)

        if cancel_check and cancel_check():
            return False

        # Download audio stream
        ok = download_track_audio(
            track, out_path, fmt=fmt, bitrate=bitrate, verbose=verbose
        )

        if not ok or (cancel_check and cancel_check()):
            with progress_lock:
                failed_count += 1
                curr_done = downloaded_count + failed_count
                _notify(f"Failed [{curr_done}/{new_count}]: {track_label}", curr_done / max(1, new_count), track_label)
            safe_print(f"  [{item_idx:>3}/{new_count}] {red('FAILED')}: {title} - {artist}")
            return False

        # Fetch cover art
        cover_bytes = download_image_bytes(track.get("cover_url"))

        # Embed metadata
        tagged = embed_id3_metadata(out_path, track, cover_bytes)

        with progress_lock:
            tracker.mark_downloaded(track, out_path)
            tracker.save_cache()
            downloaded_count += 1
            curr_done = downloaded_count + failed_count
            _notify(f"Saved [{curr_done}/{new_count}]: {track_label}", curr_done / max(1, new_count), track_label)

        safe_print(f"  [{item_idx:>3}/{new_count}] {green('Done')}: {title} - {artist}")
        time.sleep(delay)
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_worker, idx, track): track
            for idx, track in enumerate(to_download, start=1)
        }
        for future in concurrent.futures.as_completed(future_map):
            if cancel_check and cancel_check():
                executor.shutdown(wait=False, cancel_futures=True)
                _notify("Download stopped by user.", (downloaded_count + failed_count) / max(1, new_count), None)
                break

    # 4. Summary
    final_msg = f"Done! {downloaded_count} downloaded, {skipped_count} skipped."
    _notify(final_msg, 1.0, None)

    safe_print()
    safe_print(f"  {bold('Summary')}")
    safe_print(f"    {green('[OK]')} Newly downloaded : {downloaded_count}")
    safe_print(f"    {yellow('[-]')} Already up to date: {skipped_count}")
    if failed_count > 0:
        safe_print(f"    {red('[X]')} Failed            : {failed_count}")
    safe_print(f"  Output folder      : {cyan(str(output_dir))}\n")

    return {
        "downloaded": downloaded_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "total": total_on_spotify,
        "playlist_name": playlist_info.get("name", "Playlist"),
        "output_dir": str(output_dir),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Interactive / CLI Mode
# ──────────────────────────────────────────────────────────────────────────────

def interactive_prompt() -> None:
    """Prompt the user for inputs if no CLI arguments were passed."""
    print(f"\n{bold('╔═══════════════════════════════════════════════╗')}")
    print(f"{bold('║          Spotify Playlist Downloader          ║')}")
    print(f"{bold('╚═══════════════════════════════════════════════╝')}\n")

    url = input("Paste Spotify playlist/album/track link: ").strip()
    if not url:
        print("No URL provided. Exiting.")
        sys.exit(0)

    default_dir = str(Path.home() / "Music" / "Spotify Downloads")
    folder_input = input(f"Destination folder [{default_dir}]: ").strip()
    target_dir = Path(folder_input).expanduser().resolve() if folder_input else Path(default_dir)

    fmt_input = input("Audio format (mp3 / flac / m4a) [mp3]: ").strip().lower()
    fmt = fmt_input if fmt_input in ["mp3", "flac", "m4a", "opus", "wav"] else "mp3"

    run_download(
        playlist_url=url,
        output_dir=target_dir,
        fmt=fmt,
        bitrate="320k",
    )


def main() -> None:
    if len(sys.argv) == 1:
        # If user simply runs `python spotify_dl.py`, launch interactive mode
        interactive_prompt()
        return

    parser = argparse.ArgumentParser(
        prog="spotify-dl",
        description=(
            "Download songs from a Spotify playlist/album/track URL.\n"
            "Features smart incremental sync (only downloads new songs) and full\n"
            "ID3 tags + high-resolution album art."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a playlist to a specific folder:
  python spotify_dl.py https://open.spotify.com/playlist/xxx -o "D:\\Music"

  # Run incremental sync (skips existing, downloads any newly added songs):
  python spotify_dl.py https://open.spotify.com/playlist/xxx -o "D:\\Music"

  # Interactive mode:
  python spotify_dl.py
        """,
    )

    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="Spotify playlist, album, or track URL",
    )
    parser.add_argument(
        "-o", "--output",
        default=str(Path.home() / "Music" / "Spotify Downloads"),
        help="Destination folder for downloaded songs (default: ~/Music/Spotify Downloads)",
    )
    parser.add_argument(
        "--format",
        choices=["mp3", "m4a", "flac", "opus", "wav"],
        default="mp3",
        help="Audio format (default: mp3)",
    )
    parser.add_argument(
        "--bitrate",
        choices=["128k", "192k", "256k", "320k"],
        default="320k",
        help="Audio bitrate (default: 320k)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Limit to first N tracks",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download of songs even if already present",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Delay between downloads (default: 1.0s)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed yt-dlp logs",
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=4,
        metavar="N",
        help="Number of concurrent/parallel song downloads (default: 4)",
    )
    parser.add_argument(
        "--downsize",
        metavar="FOLDER",
        default=None,
        help="Downsize / convert audio quality of all songs in a folder (e.g. --downsize ~/Music/Downloads --bitrate 192k)",
    )
    parser.add_argument(
        "--fix-tags",
        metavar="FOLDER",
        default=None,
        help="Fix fragmented albums for iTunes/Apple Music by unifying Album Artist tags without re-encoding audio",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="spotify-dl 2.1.0",
    )

    args = parser.parse_args()

    if args.fix_tags:
        target_folder = Path(args.fix_tags).expanduser().resolve()
        safe_print(f"\n{bold('=== Unifying Album Tags for iTunes & Apple Music ===')}")
        safe_print(f"  Target Folder: {cyan(str(target_folder))}\n")
        res = fix_library_album_tags(
            target_folder,
            callback=lambda msg, prog: safe_print(f"  [*] {msg}")
        )
        safe_print(f"\n  {green('[OK] Album Tag Fix Complete!')}")
        safe_print(f"  Scanned Files : {res.get('total_files', 0)}")
        safe_print(f"  Updated Tracks: {res.get('fixed_files', 0)}")
        safe_print(f"  Fixed Albums  : {res.get('albums_fixed', 0)}")
        for alb in res.get("fixed_albums_list", []):
            safe_print(f"    - {alb}")
        safe_print()
        return

    if args.downsize:
        target_folder = Path(args.downsize).expanduser().resolve()
        safe_print(f"\n{bold('=== Downsizing Audio in Folder ===')}")
        safe_print(f"  Target Folder : {cyan(str(target_folder))}")
        safe_print(f"  Target Bitrate: {bold(args.bitrate)}")
        safe_print(f"  Parallelism   : {args.threads} workers\n")
        res = batch_transcode_folder(
            folder=target_folder,
            target_bitrate=args.bitrate,
            target_fmt=args.format if args.format != "mp3" else None,
            in_place=True,
            max_workers=args.threads,
            callback=lambda msg, prog, stats: safe_print(f"  [*] {msg}") if "Completed:" in msg or "Downsizing complete!" in msg else None
        )
        saved_mb = res.get("saved_bytes", 0) / (1024 * 1024)
        orig_mb = res.get("original_bytes", 0) / (1024 * 1024)
        new_mb = res.get("new_bytes", 0) / (1024 * 1024)
        safe_print(f"\n  {green('[OK] Downsizing Complete!')}")
        safe_print(f"  Processed    : {res.get('processed', 0)} of {res.get('total_files', 0)} files")
        safe_print(f"  Original Size: {orig_mb:.2f} MB")
        safe_print(f"  New Size     : {new_mb:.2f} MB")
        safe_print(f"  Storage Saved: {bold(f'{saved_mb:.2f} MB')}\n")
        return

    if not args.url:
        interactive_prompt()
        return

    output_path = Path(args.output).expanduser().resolve()
    run_download(
        playlist_url=args.url,
        output_dir=output_path,
        fmt=args.format,
        bitrate=args.bitrate,
        limit=args.limit,
        force_redownload=args.force,
        verbose=args.verbose,
        delay=args.delay,
        max_workers=args.threads,
    )


if __name__ == "__main__":
    main()
