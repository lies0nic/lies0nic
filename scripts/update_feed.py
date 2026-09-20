#!/usr/bin/env python3
"""
update_feed.py - Automatic RSS feed & JSON chapters updater for PRISMATIC (lies0nic).

Produces a feed strictly ISO to the parent Acast podcast feed, enriched with Podcast Index
chapters (<podcast:chapters>) and generates JSON chapter files (.json) for each episode
based on tracklists from the YouTube playlist RSS feed.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

PARENT_ACAST_FEED_URL = "https://feeds.acast.com/public/shows/593eded1acfa040562f3480b"
YOUTUBE_PLAYLIST_FEED_URL = "https://www.youtube.com/feeds/videos.xml?playlist_id=PLAxy1YpvZDdUoia97oMCW2VQRHXsVbz_A"
DEFAULT_CHAPTERS_BASE_URL = "https://lies0nic.github.io/lies0nic/"

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


def format_track_time(track: Dict) -> str:
    """Format track time string for display (e.g. '00:32')."""
    if "timeStr" in track and track["timeStr"]:
        return track["timeStr"]
    sec = track.get("startTime", 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def parse_duration_to_seconds(d_str: Optional[str]) -> int:
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


def extract_episode_number(title: str) -> Optional[int]:
    """Extract episode number from title string (e.g. 'Episode 037' -> 37, 'PRISMATIC by Tiësto 037' -> 37)."""
    m = re.search(r'(?:Episode\s*|#|\b0*)(\d+)\b', title, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def parse_tracklist_from_description(desc: str) -> List[Dict[str, Any]]:
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


def fetch_youtube_episodes(playlist_feed_url: str = YOUTUBE_PLAYLIST_FEED_URL) -> Dict[int, Dict]:
    """Fetch and parse YouTube playlist RSS feed for episode tracklists."""
    print(f"Fetching YouTube playlist feed: {playlist_feed_url}")
    xml_data = fetch_url(playlist_feed_url)
    root = ET.fromstring(xml_data)
    
    ns = {
        'atom': NAMESPACES['atom'],
        'media': NAMESPACES['media'],
        'yt': NAMESPACES['yt'],
    }
    
    episodes = {}
    for entry in root.findall('atom:entry', ns):
        title_el = entry.find('atom:title', ns)
        title = title_el.text if title_el is not None and title_el.text else ""
        
        desc_el = entry.find('media:group/media:description', ns)
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        
        ep_num = extract_episode_number(title)
        if ep_num is not None:
            tracks = parse_tracklist_from_description(desc)
            episodes[ep_num] = {
                "title": title,
                "description": desc,
                "tracks": tracks,
            }
    
    print(f"Parsed {len(episodes)} episodes from YouTube playlist: {sorted(episodes.keys())}")
    return episodes


def generate_json_chapters(tracks: List[Dict], output_file: str, force: bool = False) -> bool:
    """Generate Podcast Index JSON chapters file. Returns True if written."""
    data = {
        "version": "1.2.0",
        "chapters": [
            {
                "startTime": t["startTime"],
                "title": t["title"]
            }
            for t in tracks
        ]
    }
    new_content = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if os.path.exists(output_file) and not force:
        with open(output_file, 'r', encoding='utf-8') as f:
            if f.read() == new_content:
                return False
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(new_content)
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
    chapter_file_pattern = re.compile(r'^episode-\d+-chapters\.json$')

    for filename in os.listdir(chapters_dir):
        chapter_path = os.path.join(chapters_dir, filename)
        is_feed_asset = os.path.splitext(filename)[1].lower() in {'.json', '.xml'}
        if (
            filename in retained_filenames
            or os.path.isdir(chapter_path)
            or (
                not clean_feed_assets
                and not chapter_file_pattern.match(filename)
            )
            or (
                clean_feed_assets
                and not is_feed_asset
            )
        ):
            continue

        if dry_run:
            print(f"[DRY RUN] Would remove unretained feed file: {chapter_path}")
        else:
            os.remove(chapter_path)
            print(f"Removed unretained feed file: {chapter_path}")
        changed = True

    return changed


def add_chapters_to_source_xml(
    source_xml: str,
    chapter_urls_by_episode: Dict[int, str],
) -> str:
    """Add chapter references while preserving every other byte of the source XML."""
    newline = '\r\n' if '\r\n' in source_xml else '\n'
    if 'xmlns:podcast=' not in source_xml:
        source_xml = re.sub(
            r'(<rss\b[^>]*)(>)',
            rf'\1 xmlns:podcast="{NAMESPACES["podcast"]}"\2',
            source_xml,
            count=1,
        )

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
    base_chapters_url: str = DEFAULT_CHAPTERS_BASE_URL,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """
    Main update logic:
    1. Fetches parent Acast feed (the authoritative source for episodes & metadata).
    2. Fetches YouTube playlist feed for tracklists & chapters.
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
    
    yt_episodes = fetch_youtube_episodes()
    chapter_urls_by_episode: Dict[int, str] = {}
    retained_chapter_filenames: set[str] = set()
    changes_made = False
    
    # Process each item in parent feed
    for item in parent_items:
        title_el = item.find('title')
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        ep_num = extract_episode_number(title)
        
        if ep_num is not None and ep_num in yt_episodes:
            yt_data = yt_episodes[ep_num]
            tracks = yt_data["tracks"]
            
            if tracks:
                json_filename = f"episode-{ep_num:03d}-chapters.json"
                json_path = os.path.join(chapters_dir, json_filename)
                retained_chapter_filenames.add(json_filename)
                
                if not dry_run:
                    json_written = generate_json_chapters(tracks, json_path, force=force)
                    if json_written:
                        changes_made = True
                
                chapter_url = f"{base_chapters_url.rstrip('/')}/{json_filename}"
                chapter_urls_by_episode[ep_num] = chapter_url
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
    output_xml = add_chapters_to_source_xml(source_xml, chapter_urls_by_episode)
    
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
    else:
        with open(feed_path, 'w', encoding='utf-8', newline='') as f:
            f.write(output_xml)
        print(f"\nSuccessfully saved updated ISO feed to: {feed_path}")
        
    return True


def auto_detect_feed_path() -> Tuple[str, str]:
    """Auto-detect feed.xml and chapters output directory."""
    candidates = [
        os.path.join(os.getcwd(), "feed.xml"),
        os.path.join(os.getcwd(), "lies0nic", "feed.xml"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "feed.xml"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lies0nic", "feed.xml"),
    ]
    for p in candidates:
        if os.path.exists(p):
            feed_file = os.path.abspath(p)
            return feed_file, os.path.dirname(feed_file)
            
    feed_file = os.path.abspath("feed.xml")
    return feed_file, os.path.dirname(feed_file)


def main():
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
            
    parser = argparse.ArgumentParser(description="Update PRISMATIC podcast feed & chapters from parent feed & YouTube.")
    parser.add_argument("--feed-xml", "-f", help="Path to local feed.xml file")
    parser.add_argument("--chapters-dir", "-c", help="Directory to save .json chapters")
    parser.add_argument("--base-url", "-u", default=DEFAULT_CHAPTERS_BASE_URL, help="Base URL for chapters in feed.xml")
    parser.add_argument("--force", action="store_true", help="Force update feed and chapter files")
    parser.add_argument("--dry-run", action="store_true", help="Simulate run without writing files")
    
    args = parser.parse_args()
    
    feed_path = args.feed_xml
    chapters_dir = args.chapters_dir
    
    if not feed_path:
        feed_path, detected_dir = auto_detect_feed_path()
        if not chapters_dir:
            chapters_dir = detected_dir
    elif not chapters_dir:
        chapters_dir = os.path.dirname(os.path.abspath(feed_path))
        
    print(f"Target Feed XML: {feed_path}")
    print(f"Chapters Directory: {chapters_dir}")
    print(f"Base Chapters URL: {args.base_url}")
    print(f"Dry Run: {args.dry_run}")
    print(f"Force: {args.force}\n")
    
    updated = update_feed(
        feed_path=feed_path,
        chapters_dir=chapters_dir,
        base_chapters_url=args.base_url,
        force=args.force,
        dry_run=args.dry_run,
    )
    
    if updated:
        print("\n[SUCCESS] ISO Feed and chapters updated successfully.")
    else:
        print("\n[INFO] Feed already in sync with parent.")


if __name__ == "__main__":
    main()
