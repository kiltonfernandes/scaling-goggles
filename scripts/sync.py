#!/usr/bin/env python3
"""Sync YouTube channels into a podcast RSS feed.

- Reads channels.json (UI-managed list)
- For each channel: fetches YouTube Atom feed, identifies new videos vs state.json
- Downloads top-N newest as mp3 (mono, 96 kbps), uploads to GitHub Release
- Regenerates docs/feed.xml (podcast format with itunes namespace + enclosures)
- Updates state.json
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from feedgen.feed import FeedGenerator

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_FILE = ROOT / "channels.json"
STATE_FILE = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
FEED_FILE = DOCS_DIR / "feed.xml"

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]          # "owner/repo"
GH_OWNER = os.environ["GH_OWNER"]
PAGES_BASE = os.environ["PAGES_BASE"]    # "https://owner.github.io/repo"

GH_API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

YT_ATOM_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def fetch_channel_videos(channel_id: str, limit: int) -> list[dict]:
    """Return latest videos from YouTube Atom feed (newest first)."""
    r = requests.get(YT_ATOM_FEED.format(channel_id=channel_id), timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    videos = []
    for entry in root.findall("a:entry", ATOM_NS)[:limit]:
        vid = entry.find("yt:videoId", ATOM_NS).text
        title = entry.find("a:title", ATOM_NS).text
        published = entry.find("a:published", ATOM_NS).text
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={vid}"
        thumb_el = entry.find("media:group/media:thumbnail", ATOM_NS)
        thumb = thumb_el.get("url") if thumb_el is not None else ""
        desc_el = entry.find("media:group/media:description", ATOM_NS)
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        videos.append({
            "video_id": vid,
            "title": title,
            "published": published,
            "url": link,
            "thumbnail": thumb,
            "description": desc,
        })
    return videos


def fetch_channel_avatar(channel_id: str) -> str | None:
    """Scrape the channel page to find the profile picture URL."""
    try:
        url = f"https://www.youtube.com/channel/{channel_id}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        m = re.search(r'"avatar":\{"thumbnails":\[\{"url":"([^"]+)"', r.text)
        if m:
            return m.group(1)
        m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"avatar fetch failed for {channel_id}: {e}")
    return None


def download_mp3(video_url: str, out_dir: Path, bitrate_kbps: int) -> Path:
    """Use yt-dlp to download audio as mp3, mono, given bitrate."""
    out_template = str(out_dir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--postprocessor-args", f"ffmpeg:-ac 1 -b:a {bitrate_kbps}k",
        "--no-progress",
        "--no-warnings",
        "-o", out_template,
        video_url,
    ]
    subprocess.run(cmd, check=True)
    mp3s = list(out_dir.glob("*.mp3"))
    if not mp3s:
        raise RuntimeError(f"no mp3 produced for {video_url}")
    return mp3s[0]


def gh_release_for_video(video_id: str) -> dict:
    """Get-or-create a release tagged with the video id."""
    tag = f"v-{video_id}"
    r = requests.get(f"{GH_API}/repos/{GH_REPO}/releases/tags/{tag}", headers=HEADERS)
    if r.status_code == 200:
        return r.json()
    r = requests.post(
        f"{GH_API}/repos/{GH_REPO}/releases",
        headers=HEADERS,
        json={"tag_name": tag, "name": tag, "body": f"Audio for video {video_id}"},
    )
    r.raise_for_status()
    return r.json()


def gh_upload_asset(release: dict, mp3_path: Path) -> str:
    """Upload mp3 to release, return browser_download_url."""
    upload_url = release["upload_url"].split("{")[0]
    name = mp3_path.name
    # remove existing asset with same name (idempotent re-runs)
    for a in release.get("assets", []):
        if a["name"] == name:
            requests.delete(f"{GH_API}/repos/{GH_REPO}/releases/assets/{a['id']}", headers=HEADERS)
    with mp3_path.open("rb") as f:
        data = f.read()
    r = requests.post(
        f"{upload_url}?name={urllib.parse.quote(name)}",
        headers={**HEADERS, "Content-Type": "audio/mpeg"},
        data=data,
    )
    r.raise_for_status()
    return r.json()["browser_download_url"]


def build_feed(channels: list[dict], state: dict, settings: dict) -> str:
    """Generate iTunes podcast RSS from all known episodes in state."""
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title("YouTube Podcast Feed")
    fg.link(href=f"{PAGES_BASE}/feed.xml", rel="self")
    fg.link(href=PAGES_BASE, rel="alternate")
    fg.description("Auto-generated podcast feed from selected YouTube channels.")
    fg.language("en")
    fg.author({"name": GH_OWNER})

    # collect & sort all non-purged episodes by published desc
    episodes = []
    for ch_id, ch_state in state.get("channels", {}).items():
        ch_meta = next((c for c in channels if c["id"] == ch_id), None)
        for ep in ch_state.get("episodes", {}).values():
            if ep.get("purged_at"):
                # purged but still in feed_keep window? Skip enclosure.
                continue
            if not ep.get("audio_url"):
                continue
            ep["_channel_name"] = ch_meta["name"] if ch_meta else ch_id
            ep["_channel_avatar"] = (ch_meta or {}).get("avatar") or ""
            episodes.append(ep)
    episodes.sort(key=lambda e: e["published"], reverse=True)

    for ep in episodes:
        fe = fg.add_entry()
        fe.id(ep["url"])
        fe.title(f"[{ep['_channel_name']}] {ep['title']}")
        fe.link(href=ep["url"])
        fe.description(ep.get("description") or ep["title"])
        fe.published(ep["published"])
        fe.enclosure(ep["audio_url"], str(ep.get("audio_size", 0)), "audio/mpeg")
        # itunes per-episode image = video thumbnail
        thumb = ep.get("thumbnail") or ep.get("_channel_avatar")
        if thumb:
            fe.podcast.itunes_image(thumb)

    # itunes channel image = first channel avatar (if any)
    first_avatar = next((c.get("avatar") for c in channels if c.get("avatar")), None)
    if first_avatar:
        fg.podcast.itunes_image(first_avatar)
    fg.podcast.itunes_category("Technology")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_author(GH_OWNER)

    return fg.rss_str(pretty=True).decode("utf-8")


def main() -> int:
    config = load_json(CHANNELS_FILE, {"channels": [], "settings": {}})
    channels = config.get("channels", [])
    settings = config.get("settings", {})
    bitrate = int(settings.get("audio_bitrate_kbps", 96))

    state = load_json(STATE_FILE, {"channels": {}})
    state.setdefault("channels", {})

    if not channels:
        log("no channels configured — generating empty feed")
    else:
        log(f"processing {len(channels)} channels")

    # Refresh avatars (cheap, helps keep podcast art current)
    for ch in channels:
        if not ch.get("avatar"):
            ch["avatar"] = fetch_channel_avatar(ch["id"])

    for ch in channels:
        ch_id = ch["id"]
        max_videos = int(ch.get("max_videos", 3))
        ch_state = state["channels"].setdefault(ch_id, {"episodes": {}})
        try:
            videos = fetch_channel_videos(ch_id, limit=max_videos)
        except Exception as e:
            log(f"failed to fetch feed for {ch_id}: {e}")
            continue

        for vid in videos:
            vid_id = vid["video_id"]
            if vid_id in ch_state["episodes"]:
                continue
            log(f"new video for {ch.get('name', ch_id)}: {vid['title']}")
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                try:
                    mp3 = download_mp3(vid["url"], tmp_path, bitrate)
                except Exception as e:
                    log(f"  download failed: {e}")
                    continue
                size = mp3.stat().st_size
                try:
                    release = gh_release_for_video(vid_id)
                    audio_url = gh_upload_asset(release, mp3)
                except Exception as e:
                    log(f"  upload failed: {e}")
                    continue
                ch_state["episodes"][vid_id] = {
                    **vid,
                    "audio_url": audio_url,
                    "audio_size": size,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                    "purged_at": None,
                }
                log(f"  uploaded {size//1024} KB")

    # Persist updated channels (avatars filled in) back into channels.json
    save_json(CHANNELS_FILE, {"channels": channels, "settings": settings})
    save_json(STATE_FILE, state)
    DOCS_DIR.mkdir(exist_ok=True)
    FEED_FILE.write_text(build_feed(channels, state, settings))
    (DOCS_DIR / ".nojekyll").touch()
    log(f"feed written: {FEED_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
