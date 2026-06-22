#!/usr/bin/env python3
"""Delete release assets older than purge_after_hours; drop feed entries older than feed_keep_days."""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CHANNELS_FILE = ROOT / "channels.json"
STATE_FILE = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
FEED_FILE = DOCS_DIR / "feed.xml"

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]
HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def log(m): print(f"[purge] {m}", flush=True)


def load_json(p, d):
    return json.loads(p.read_text()) if p.exists() else d


def save_json(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def delete_release(tag: str) -> None:
    r = requests.get(f"https://api.github.com/repos/{GH_REPO}/releases/tags/{tag}", headers=HEADERS)
    if r.status_code != 200:
        return
    rel = r.json()
    for a in rel.get("assets", []):
        requests.delete(f"https://api.github.com/repos/{GH_REPO}/releases/assets/{a['id']}", headers=HEADERS)
    requests.delete(f"https://api.github.com/repos/{GH_REPO}/releases/{rel['id']}", headers=HEADERS)


def main() -> int:
    config = load_json(CHANNELS_FILE, {"channels": [], "settings": {}})
    settings = config.get("settings", {})
    purge_hours = int(settings.get("purge_after_hours", 48))
    keep_days = int(settings.get("feed_keep_days", 7))

    state = load_json(STATE_FILE, {"channels": {}})
    now = datetime.now(timezone.utc)
    purge_cutoff = now - timedelta(hours=purge_hours)
    drop_cutoff = now - timedelta(days=keep_days)

    purged = 0
    dropped = 0
    for ch_id, ch_state in state.get("channels", {}).items():
        eps = ch_state.get("episodes", {})
        to_drop = []
        for vid_id, ep in eps.items():
            downloaded = datetime.fromisoformat(ep["downloaded_at"]) if ep.get("downloaded_at") else None
            if ep.get("purged_at"):
                purged_at = datetime.fromisoformat(ep["purged_at"])
                if purged_at < drop_cutoff:
                    to_drop.append(vid_id)
                continue
            if downloaded and downloaded < purge_cutoff:
                log(f"purging {vid_id}")
                delete_release(f"v-{vid_id}")
                ep["audio_url"] = None
                ep["audio_size"] = 0
                ep["purged_at"] = now.isoformat()
                purged += 1
        for vid_id in to_drop:
            del eps[vid_id]
            dropped += 1

    save_json(STATE_FILE, state)
    log(f"done. purged={purged} dropped={dropped}")

    # Regenerate feed via sync.py's builder (simple: invoke sync to rebuild XML only)
    # We just leave feed regeneration to next sync run; but rewrite minimal feed now to drop entries.
    try:
        from importlib import util as _u
        spec = _u.spec_from_file_location("sync_mod", ROOT / "scripts" / "sync.py")
        mod = _u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        DOCS_DIR.mkdir(exist_ok=True)
        FEED_FILE.write_text(mod.build_feed(config.get("channels", []), state, settings))
    except Exception as e:
        log(f"feed rebuild skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
