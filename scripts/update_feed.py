#!/usr/bin/env python3
"""
update_feed.py - Automatic RSS feed & JSON chapters updater for PRISMATIC (lies0nic).

Produces a feed strictly ISO to the parent Acast podcast feed, enriched with Podcast Index
chapters (<podcast:chapters>) and generates JSON chapter files (.json) for each episode
based on tracklists from the YouTube playlist and each episode's watch page data.
"""

import argparse
import contextlib
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

PARENT_ACAST_FEED_URL = "https://feeds.acast.com/public/shows/593eded1acfa040562f3480b"
DEFAULT_FEED_BASE_URL = "https://lies0nic.github.io/lies0nic/"

YOUTUBE_PLAYLIST_ID = "PLAxy1YpvZDdUoia97oMCW2VQRHXsVbz_A"
# Public "WEB" client credentials shipped in every browser, not secrets.
# Chapter images only exist as signed, per-chapter URLs in the watch page data:
# they can be fetched, never built (signed and pinned to an exact millisecond).
YOUTUBE_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
YOUTUBE_CLIENT_VERSION = "2.20261029.11.00"
YOUTUBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHAPTER_MATCH_TOLERANCE = 1.5  # seconds

NAMESPACES = {
    'atom': 'http://www.w3.org/2005/Atom',
    'googleplay': 'http://www.google.com/schemas/play-podcasts/1.0',
    'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
    'media': 'http://search.yahoo.com/mrss/',
    'podaccess': 'https://access.acast.com/schema/1.0/',
    'acast': 'https://schema.acast.com/1.0/',
    'podcast': 'https://podcastindex.org/namespace/1.0',
    'yt': 'http://www.youtube.com/xml/schemas/2015',
}

for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


def fetch_url(url: str) -> bytes:
    """Fetch content from a URL with User-Agent header."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_time_to_seconds(t_str: str) -> int:
    """Parse timecode string (hh:mm:ss or mm:ss) into integer seconds."""
    parts = list(map(int, t_str.strip().split(':')))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def format_track_time(track: dict) -> str:
    """Format track time string for display (e.g. '00:32')."""
    if track.get("timeStr"):
        return track["timeStr"]
    sec = track.get("startTime", 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def parse_duration_to_seconds(d_str: str | None) -> int:
    """Parse duration string from itunes:duration into integer seconds."""
    if not d_str:
        return 3600
    d_str = d_str.strip()
    if ':' in d_str:
        return parse_time_to_seconds(d_str)
    try:
        return int(float(d_str))
    except ValueError:
        return 3600


def extract_episode_number(title: str) -> int | None:
    """Extract episode number from title string (e.g. 'Episode 037' -> 37, 'PRISMATIC by Tiësto 037' -> 37)."""
    m = re.search(r'(?:Episode\s*|#|\b0*)(\d+)\b', title, re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_tracklist_from_description(desc: str) -> list[dict[str, Any]]:
    """
    Parse tracklist lines from YouTube video description.
    Handles formats like:
      [00:00] Intro
      [00:32] 1 Artist - Title
      00:32 1 Artist - Title
    """
    tracks = []
    lines = desc.split('\n')
    for line in lines:
        line = line.strip()
        m = re.match(r'^(?:\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?)\s*(.+)$', line)
        if m:
            time_str, track_title = m.groups()
            sec = parse_time_to_seconds(time_str)
            tracks.append({
                "startTime": sec,
                "timeStr": time_str,
                "title": track_title.strip()
            })
    return tracks


def youtube_api(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the InnerTube endpoint the YouTube web app itself calls."""
    body = {
        "context": {"client": {
            "clientName": "WEB",
            "clientVersion": YOUTUBE_CLIENT_VERSION,
            "hl": "en",
            "gl": "US",
        }},
        **payload,
    }
    req = urllib.request.Request(
        f"https://www.youtube.com/youtubei/v1/{endpoint}?key={YOUTUBE_INNERTUBE_KEY}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": YOUTUBE_USER_AGENT,
            "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": YOUTUBE_CLIENT_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def walk_json(node: Any, key: str):
    """Yield every value stored under `key`, wherever it sits in the tree."""
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from walk_json(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from walk_json(value, key)


def fetch_playlist_episodes() -> dict[int, dict]:
    """Episodes of the playlist: {number: {title, video_id}} from the playlist listing."""
    print(f"Fetching YouTube playlist: {YOUTUBE_PLAYLIST_ID}")
    root = youtube_api("browse", {"browseId": "VL" + YOUTUBE_PLAYLIST_ID})

    episodes = {}
    for lockup in walk_json(root, "lockupViewModel"):
        video_id = lockup.get("contentId", "")
        title = (
            lockup.get("metadata", {})
            .get("lockupMetadataViewModel", {})
            .get("title", {})
            .get("content", "")
        )
        ep_num = extract_episode_number(title)
        if ep_num is not None and video_id:
            episodes[ep_num] = {"title": title, "video_id": video_id}

    print(f"Parsed {len(episodes)} episodes from YouTube playlist: {sorted(episodes.keys())}")
    return episodes


def fetch_watch_data(video_id: str) -> dict[str, Any]:
    """Description and per-chapter image URLs from the watch page data, best effort."""
    if not video_id:
        return {"description": "", "frames": []}
    try:
        root = youtube_api("next", {"videoId": video_id})
    except Exception as exc:  # noqa: BLE001 - keep published chapters when YouTube is mute
        print(f"  Warning: watch data unavailable for {video_id}: {type(exc).__name__}")
        return {"description": "", "frames": []}

    description = ""
    for info in walk_json(root, "videoSecondaryInfoRenderer"):
        description = info.get("attributedDescription", {}).get("content", "")
        if not description:
            runs = info.get("description", {}).get("runs", [])
            description = "".join(run.get("text", "") for run in runs)
        if description:
            break

    frames = []
    for renderer in walk_json(root, "chapterRenderer"):
        thumbs = renderer.get("thumbnail", {}).get("thumbnails", [])
        url = thumbs[-1]["url"] if thumbs else ""
        if is_chapter_frame(url):
            frames.append({"ms": renderer["timeRangeStartMillis"], "img": url})

    return {"description": description, "frames": frames}


def is_chapter_frame(url: str) -> bool:
    """True for a real per-chapter frame, not the episode's single video thumbnail."""
    return "hqdefault_" in url


def match_chapter_images(tracks: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[str | None]:
    """One image URL per track, matched to the closest chapter start time."""
    used: set[int] = set()
    images: list[str | None] = []
    for track in tracks:
        best, best_gap = None, CHAPTER_MATCH_TOLERANCE
        for index, frame in enumerate(frames):
            if index in used:
                continue
            gap = abs(frame["ms"] / 1000 - track["startTime"])
            if gap <= best_gap:
                best, best_gap = index, gap
        if best is None:
            images.append(None)
        else:
            used.add(best)
            images.append(frames[best]["img"])
    return images


def read_existing_chapters(json_path: str) -> list[dict[str, Any]]:
    """Already published chapters, used as fallback when YouTube is unreachable."""
    if not os.path.exists(json_path):
        return []
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("chapters", [])


def chapter_image_is_alive(url: str) -> bool:
    """Cheap canary: a video's chapter images share one signed batch, so one probe is enough."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method='HEAD', headers={"User-Agent": YOUTUBE_USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - unreachable means "refresh"
        return False


def write_if_changed(
    path: str,
    content: str,
    force: bool = False,
    newline: str | None = None,
) -> bool:
    """Write content unless the file already holds it exactly. Returns True if written."""
    if not force and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            if f.read() == content:
                return False
    with open(path, 'w', encoding='utf-8', newline=newline) as f:
        f.write(content)
    return True


def generate_json_chapters(
    tracks: list[dict],
    output_file: str,
    images: list[str | None] | None = None,
    force: bool = False,
) -> bool:
    """Generate Podcast Index JSON chapters file. Returns True if written."""
    images = images or [None] * len(tracks)
    data = {
        "version": "1.2.0",
        "chapters": [
            {
                "startTime": t["startTime"],
                "title": t["title"],
                **({"img": image} if image else {}),
            }
            for t, image in zip(tracks, images)
        ]
    }
    new_content = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if not write_if_changed(output_file, new_content, force=force):
        return False
    print(f"Generated JSON chapters: {output_file}")
    return True


def clean_feed_files(
    chapters_dir: str,
    retained_filenames: set[str],
    dry_run: bool = False,
    clean_feed_assets: bool = False,
) -> bool:
    """Clean unretained files from the feed directory."""
    changed = False
    chapter_file_pattern = re.compile(r'episode-\d+-chapters\.json')

    for filename in os.listdir(chapters_dir):
        chapter_path = os.path.join(chapters_dir, filename)
        if filename in retained_filenames or os.path.isdir(chapter_path):
            continue
        if clean_feed_assets:
            if os.path.splitext(filename)[1].lower() not in {'.json', '.xml'}:
                continue
        elif not chapter_file_pattern.fullmatch(filename):
            continue

        if dry_run:
            print(f"[DRY RUN] Would remove unretained feed file: {chapter_path}")
        else:
            os.remove(chapter_path)
            print(f"Removed unretained feed file: {chapter_path}")
        changed = True

    return changed


def format_tracklist_html(tracks: list[dict[str, Any]]) -> str:
    """Render a compact tracklist snippet for an episode description."""
    parts = []
    for track in tracks:
        title = html.escape(str(track.get('title', '')).strip(), quote=False)
        if title:
            parts.append(f'{format_track_time(track)} - {title}')
    return '<br>'.join(parts)


def build_enriched_description(raw_description: str, tracks: list[dict[str, Any]]) -> str:
    """Append tracklist details to the existing description while preserving the Acast footer."""
    tracklist_html = format_tracklist_html(tracks)
    if not tracklist_html:
        return raw_description

    if '<hr>' in raw_description:
        intro, footer = raw_description.split('<hr>', 1)
        footer = '<hr>' + footer
    else:
        intro = raw_description
        footer = ''

    return f'{intro}<br>Tracklist:<br>{tracklist_html}{footer}'


def add_chapters_to_source_xml(
    source_xml: str,
    chapter_urls_by_episode: dict[int, str],
    chapter_tracks_by_episode: dict[int, list[dict[str, Any]]] | None = None,
    feed_base_url: str = DEFAULT_FEED_BASE_URL,
) -> str:
    """Add chapter references while preserving every other byte of the source XML."""
    newline = '\r\n' if '\r\n' in source_xml else '\n'
    feed_url = feed_base_url.rstrip('/') + '/feed.xml'

    # The source feed's self reference and new-feed-url point clients at the origin
    # host, so podcast apps migrate away and lose the enriched descriptions. The
    # inherited values stay, commented out, above ours.
    def repoint(match: re.Match[str], attribute: str | None = None) -> str:
        indent, tag = match.group(1), match.group(2)
        if attribute:
            if f'{attribute}="{feed_url}"' in tag:
                return match.group(0)
            fixed = re.sub(rf'{attribute}="[^"]*"', f'{attribute}="{feed_url}"', tag)
        else:
            if f'>{feed_url}<' in tag:
                return match.group(0)
            fixed = re.sub(r'>.*?<', f'>{feed_url}<', tag, count=1, flags=re.DOTALL)
        return f'{indent}<!-- {tag} -->{newline}{indent}{fixed}{newline}'

    source_xml = re.sub(
        r'^([ \t]*)(<atom:link\b[^>]*\brel="self"[^>]*>)[ \t]*(?:\r?\n)?',
        lambda m: repoint(m, 'href'),
        source_xml,
        count=1,
        flags=re.MULTILINE,
    )
    source_xml = re.sub(
        r'^([ \t]*)(<itunes:new-feed-url>.*?</itunes:new-feed-url>)[ \t]*(?:\r?\n)?',
        repoint,
        source_xml,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if 'xmlns:podcast=' not in source_xml:
        source_xml = re.sub(
            r'(<rss\b[^>]*)(>)',
            rf'\1 xmlns:podcast="{NAMESPACES["podcast"]}"\2',
            source_xml,
            count=1,
        )

    chapter_tracks_by_episode = chapter_tracks_by_episode or {}
    item_pattern = re.compile(r'(<item\b[^>]*>.*?</item\s*>)', re.DOTALL | re.IGNORECASE)
    chapter_pattern = re.compile(
        r'^[ \t]*<podcast:chapters\b[^>]*/>[ \t]*(?:\r?\n)?',
        re.MULTILINE,
    )

    def enrich_item(match: re.Match[str]) -> str:
        item_xml = match.group(1)
        title_match = re.search(r'<title\b[^>]*>(.*?)</title\s*>', item_xml, re.DOTALL | re.IGNORECASE)
        title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ''
        episode_number = extract_episode_number(title)
        chapter_url = chapter_urls_by_episode.get(episode_number) if episode_number is not None else None
        tracks = chapter_tracks_by_episode.get(episode_number, []) if episode_number is not None else []

        if tracks:
            def replace_value(tag_name: str) -> str:
                pattern = re.compile(
                    rf'(<{tag_name}\b[^>]*>\s*)(?:<!\[CDATA\[(.*?)\]\]>|(.*?))(\s*</{tag_name}>)',
                    re.DOTALL | re.IGNORECASE,
                )

                def rebuild(tag_match: re.Match[str]) -> str:
                    cdata, plain = tag_match.group(2), tag_match.group(3)
                    value = build_enriched_description(
                        cdata if cdata is not None else plain, tracks
                    )
                    if cdata is None:
                        return f'{tag_match.group(1)}{value}{tag_match.group(4)}'
                    return f'{tag_match.group(1)}<![CDATA[{value}]]>{tag_match.group(4)}'

                return pattern.sub(rebuild, item_xml, count=1)

            item_xml = replace_value('description')
            item_xml = replace_value('itunes:summary')

        if not chapter_url:
            return item_xml

        chapter_line = (
            f'<podcast:chapters url="{chapter_url}" type="application/json+chapters"/>'
        )
        existing = chapter_pattern.search(item_xml)
        if existing:
            indentation = re.match(r'[ \t]*', existing.group(0)).group(0)
            return item_xml[:existing.start()] + f'{indentation}{chapter_line}{newline}' + item_xml[existing.end():]

        description_match = re.search(
            r'(?m)^([ \t]*)(<description\b)',
            item_xml,
            re.IGNORECASE,
        )
        if not description_match:
            return item_xml

        line_start = description_match.start()
        line_indent = description_match.group(1)
        return item_xml[:line_start] + f'{line_indent}{chapter_line}{newline}' + item_xml[line_start:]

    return item_pattern.sub(enrich_item, source_xml)


def update_feed(
    feed_path: str,
    chapters_dir: str,
    feed_base_url: str = DEFAULT_FEED_BASE_URL,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """
    Main update logic:
    1. Fetches parent Acast feed (the authoritative source for episodes & metadata).
    2. Fetches the YouTube playlist, then each episode's watch data for tracklists,
       chapters and per-chapter image URLs.
    3. Keeps feed ISO to parent feed, with exactly the parent feed's items and metadata.
    4. Adds <podcast:chapters> tag and generates .json chapter files for each episode.
    5. Removes files that are no longer represented in the current feed.
    6. Writes feed.xml if changes were made.
    """
    os.makedirs(chapters_dir, exist_ok=True)
    
    print(f"Fetching parent Acast feed: {PARENT_ACAST_FEED_URL}")
    parent_xml = fetch_url(PARENT_ACAST_FEED_URL)
    parent_root = ET.fromstring(parent_xml)
    parent_channel = parent_root.find('./channel')
    if parent_channel is None:
        print("Error: parent feed has no <channel> element.")
        return False
        
    parent_items = parent_channel.findall('item')
    print(f"Found {len(parent_items)} items in parent feed.")
    
    yt_episodes = fetch_playlist_episodes()
    chapter_urls_by_episode: dict[int, str] = {}
    chapter_tracks_by_episode: dict[int, list[dict[str, Any]]] = {}
    retained_chapter_filenames: set[str] = set()
    changes_made = False
    
    # Process each item in parent feed
    for item in parent_items:
        title_el = item.find('title')
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        ep_num = extract_episode_number(title)
        
        if ep_num is not None and ep_num in yt_episodes:
            json_filename = f"episode-{ep_num:03d}-chapters.json"
            json_path = os.path.join(chapters_dir, json_filename)
            known = read_existing_chapters(json_path)

            watch_data = fetch_watch_data(yt_episodes[ep_num]["video_id"])
            tracks = parse_tracklist_from_description(watch_data["description"]) or known
            if not tracks:
                print(f"  No tracklist for {title}, skipping chapters")
                continue

            retained_chapter_filenames.add(json_filename)

            if not dry_run:
                # Keep a published URL as long as it answers, so a run doesn't churn
                # every signature; a dead one means the batch expired and is refetched.
                images = match_chapter_images(tracks, watch_data["frames"])
                known_images = {chapter["startTime"]: chapter["img"] for chapter in known if chapter.get("img")}
                images = [new or known_images.get(track["startTime"]) for track, new in zip(tracks, images)]
                canary = next((image for image in images if image), "")
                if known_images and all(images) and chapter_image_is_alive(canary):
                    images = [known_images.get(track["startTime"]) for track in tracks]

                json_written = generate_json_chapters(tracks, json_path, images, force=force)
                if json_written:
                    changes_made = True
                print(f"  {sum(1 for image in images if image)}/{len(tracks)} chapter images")

            chapter_url = f"{feed_base_url.rstrip('/')}/{json_filename}"
            chapter_urls_by_episode[ep_num] = chapter_url
            chapter_tracks_by_episode[ep_num] = tracks
            print(f"Prepared <podcast:chapters> tag for {title}")

    if clean_feed_files(
        chapters_dir,
        retained_chapter_filenames,
        dry_run=dry_run,
    ):
        changes_made = True

    if force:
        retained_filenames = retained_chapter_filenames.copy()
        if os.path.abspath(os.path.dirname(feed_path) or os.curdir) == os.path.abspath(chapters_dir):
            retained_filenames.add(os.path.basename(feed_path))
        if clean_feed_files(
            chapters_dir,
            retained_filenames,
            dry_run=dry_run,
            clean_feed_assets=True,
        ):
            changes_made = True

    source_xml = parent_xml.decode('utf-8')
    output_xml = add_chapters_to_source_xml(
        source_xml,
        chapter_urls_by_episode,
        chapter_tracks_by_episode,
        feed_base_url=feed_base_url,
    )
    
    # Check if feed.xml on disk is different
    current_xml = ""
    if os.path.exists(feed_path):
        with open(feed_path, 'r', encoding='utf-8', newline='') as f:
            current_xml = f.read()
            
    if current_xml != output_xml:
        changes_made = True
        
    if not changes_made and not force:
        print("\nFeed and chapter files are already up-to-date and ISO to parent feed. No changes needed.")
        return False
        
    if dry_run:
        print(f"\n[DRY RUN] Would write updated ISO feed to {feed_path}")
    elif write_if_changed(feed_path, output_xml, force=True, newline=''):
        print(f"\nSuccessfully saved updated ISO feed to: {feed_path}")
        
    return True


def auto_detect_feed_path() -> tuple[str, str]:
    """Auto-detect feed.xml and chapters output directory."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (os.getcwd(), here):
        for relative in ("feed.xml", os.path.join("lies0nic", "feed.xml")):
            path = os.path.join(base, relative)
            if os.path.exists(path):
                feed_file = os.path.abspath(path)
                return feed_file, os.path.dirname(feed_file)

    feed_file = os.path.abspath("feed.xml")
    return feed_file, os.path.dirname(feed_file)


def main():
    if sys.stdout.encoding != 'utf-8':
        with contextlib.suppress(AttributeError, OSError):
            sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="Update PRISMATIC podcast feed & chapters from parent feed & YouTube.")
    parser.add_argument("--feed-xml", "-f", help="Path to local feed.xml file")
    parser.add_argument("--base-url", "-u", default=DEFAULT_FEED_BASE_URL, help="Base URL for feed.xml and its chapters")
    parser.add_argument("--chapters-dir", "-c", help="Directory to save .json chapters")
    parser.add_argument("--force", action="store_true", help="Force update feed and chapter files")
    parser.add_argument("--dry-run", action="store_true", help="Simulate run without writing files")
    
    args = parser.parse_args()
    
    feed_path = args.feed_xml
    if feed_path:
        chapters_dir = args.chapters_dir or os.path.dirname(os.path.abspath(feed_path))
    else:
        feed_path, detected_dir = auto_detect_feed_path()
        chapters_dir = args.chapters_dir or detected_dir
        
    print(f"Target Feed XML: {feed_path}")
    print(f"Base feed URL: {args.base_url}")
    print(f"Chapters Directory: {chapters_dir}")
    print(f"Dry Run: {args.dry_run}")
    print(f"Force: {args.force}\n")
    
    updated = update_feed(
        feed_path=feed_path,
        chapters_dir=chapters_dir,
        feed_base_url=args.base_url,
        force=args.force,
        dry_run=args.dry_run,
    )
    
    if updated:
        print("\n[SUCCESS] ISO Feed and chapters updated successfully.")
    else:
        print("\n[INFO] Feed already in sync with parent.")


if __name__ == "__main__":
    main()
