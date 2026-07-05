#!/usr/bin/env python3
"""Convert a TV Time GDPR export into a Ryot Generic JSON Importer file.

TV Time is shutting down (July 2026). Its GDPR data export no longer matches
what the existing community converters (SirMartin/TvTimeToRyot,
IAM-marco/TVTime2Ryot) expect: there's no seen_episode.csv or
seen_episode_source.csv in a current export. This script reads the files
TV Time actually ships today instead:

  - followed_tv_show.csv          the list of shows you've followed
  - watched_on_episode.csv        a full per-episode watch log, where TV Time
                                   logged one (not guaranteed for every show)
  - tracking-prod-records-v2.csv  a "last episode watched" marker per show,
                                   used as a fallback when there's no
                                   per-episode log

For each show it resolves a TMDB ID (handling TV Time's "Show Name (Year)"
disambiguation suffix, which throws off a plain TMDB search) and writes a
Ryot-compatible CompleteExport JSON file, ready for Settings > Import & Export
> Generic JSON in Ryot.

Usage:
    ./tvtime_to_ryot.py /path/to/gdpr-export-dir [-o output.json]

Requires a TMDB read access token (v4 auth) in the TMDB_READ_ACCESS_TOKEN
environment variable. Get one free at https://www.themoviedb.org/settings/api.
"""
import argparse
import ast
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

TOKEN = os.environ.get("TMDB_READ_ACCESS_TOKEN")
HEADERS = {"accept": "application/json", "Authorization": f"Bearer {TOKEN}"}


def to_iso(dt_str):
    # TV Time timestamps are "YYYY-MM-DD HH:MM:SS" (naive, treated as UTC)
    return dt_str.replace(" ", "T") + ".000Z"


def parse_most_recent(raw):
    # raw looks like: map[ep_id:1.081442e+07 ep_no:10 s_no:1 uuid:... watch_date:1.767e+15]
    # space-separated key:value pairs inside map[...] -> needs commas before it's parseable
    cleaned = raw.replace("[", "{").replace("]", "}").replace("map", "").replace(" ", ", ")
    # some rows include a first_air_date field like 2022-07-13T00:00:00Z; its internal
    # colons would otherwise collide with the key:value quoting regex below
    cleaned = re.sub(r"(\d{4}-\d\d-\d\dT\d\d):(\d\d):(\d\dZ)", r"\1.\2.\3", cleaned)
    cleaned = re.sub(r"([A-Za-z0-9_\-\+\.]+):([A-Za-z0-9_\-\+\.]+)", r'"\1":"\2"', cleaned)
    d = ast.literal_eval(cleaned)
    return int(float(d["s_no"])), int(float(d["ep_no"]))


def load_export(data_dir: Path):
    full_history = {}  # show_name -> list of (season, episode, watched_at)
    with open(data_dir / "watched_on_episode.csv", newline="") as f:
        for row in csv.DictReader(f):
            show = row["tv_show_name"]
            full_history.setdefault(show, []).append(
                (int(row["episode_season_number"]), int(row["episode_number"]), row["created_at"])
            )

    progress_marker = {}  # show_name -> (season, episode, watched_at)
    with open(data_dir / "tracking-prod-records-v2.csv", newline="") as f:
        for row in csv.DictReader(f):
            show = row["series_name"]
            if not show or not row.get("most_recent_ep_watched"):
                continue
            try:
                season, episode = parse_most_recent(row["most_recent_ep_watched"])
            except (ValueError, SyntaxError):
                continue
            progress_marker[show] = (season, episode, row["updated_at"])

    followed = []
    with open(data_dir / "followed_tv_show.csv", newline="") as f:
        for row in csv.DictReader(f):
            followed.append(row["tv_show_name"])

    all_shows = set(followed) | set(full_history) | set(progress_marker)
    return all_shows, full_history, progress_marker


YEAR_SUFFIX = re.compile(r"^(.*)\s\((\d{4})\)$")


def tmdb_search(show_name):
    m = YEAR_SUFFIX.match(show_name)
    query, year = (m.group(1), m.group(2)) if m else (show_name, None)

    url = "https://api.themoviedb.org/3/search/tv"
    params = {"query": query, "include_adult": "false", "language": "en-US", "page": 1}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])

    if year:
        by_year = [r for r in results if (r.get("first_air_date") or "").startswith(year)]
        if by_year:
            return by_year[0]["id"]

    exact = [r for r in results if r["name"] == query]
    if exact:
        return exact[0]["id"]
    return results[0]["id"] if results else None


def build_seen_history(show_name, full_history, progress_marker):
    if show_name in full_history:
        return [
            {
                "progress": "100",
                "show_season_number": season,
                "show_episode_number": episode,
                "started_on": to_iso(watched_at),
                "ended_on": to_iso(watched_at),
                "state": "completed",
            }
            for season, episode, watched_at in sorted(full_history[show_name], key=lambda t: t[2])
        ]
    if show_name in progress_marker:
        season, episode, watched_at = progress_marker[show_name]
        return [
            {
                "progress": "100",
                "show_season_number": season,
                "show_episode_number": episode,
                "started_on": to_iso(watched_at),
                "ended_on": to_iso(watched_at),
                "state": "completed",
            }
        ]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_dir", type=Path, help="path to the unzipped TV Time GDPR export")
    parser.add_argument("-o", "--output", type=Path, default=Path("ryot_data.json"), help="output JSON path (default: ryot_data.json)")
    args = parser.parse_args()

    if not TOKEN:
        print("error: TMDB_READ_ACCESS_TOKEN environment variable is not set", file=sys.stderr)
        sys.exit(1)

    all_shows, full_history, progress_marker = load_export(args.export_dir)
    print(f"{len(all_shows)} shows to match against TMDB")

    ryot_json = []
    not_found = []
    for i, show_name in enumerate(sorted(all_shows), 1):
        try:
            tmdb_id = tmdb_search(show_name)
        except requests.RequestException as e:
            print(f"[{i}/{len(all_shows)}] {show_name}: TMDB request failed ({e})")
            not_found.append(show_name)
            continue

        if tmdb_id is None:
            print(f"[{i}/{len(all_shows)}] {show_name}: no TMDB match")
            not_found.append(show_name)
            continue

        seen_history = build_seen_history(show_name, full_history, progress_marker)
        ryot_json.append(
            {
                "collections": [],
                "identifier": str(tmdb_id),
                "lot": "show",
                "reviews": [],
                "source": "tmdb",
                "source_id": str(tmdb_id),
                "seen_history": seen_history,
            }
        )
        print(f"[{i}/{len(all_shows)}] {show_name} -> TMDB {tmdb_id} ({len(seen_history)} episodes)")
        time.sleep(0.05)  # stay well under TMDB's rate limit

    # Ryot's Generic JSON importer expects a CompleteExport object, not a bare array
    complete_export = {"metadata": ryot_json}
    with open(args.output, "w") as f:
        json.dump(complete_export, f, indent=2)

    print(f"\nWrote {len(ryot_json)} shows to {args.output}")
    if not_found:
        print(f"\n{len(not_found)} shows need manual TMDB lookup (name mismatch or removed from TMDB):")
        for show in not_found:
            print(f"  - {show}")


if __name__ == "__main__":
    main()
