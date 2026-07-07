#!/usr/bin/env python3
"""Convert a TV Time GDPR export into a Ryot Generic JSON Importer file.

TV Time is shutting down (July 2026). Its GDPR data export no longer matches
what the existing community converters (SirMartin/TvTimeToRyot,
IAM-marco/TVTime2Ryot) expect: there's no seen_episode.csv or
seen_episode_source.csv in a current export. This script reads the files
TV Time actually ships today instead:

  - followed_tv_show.csv          the list of shows you've followed
  - tracking-prod-records-v2.csv  per-episode show watch events (one row per
                                   episode watched, season_number/episode_number
                                   populated directly) - this is the complete
                                   history; it's a strict superset of
                                   watched_on_episode.csv, which this script
                                   does not need to read at all
  - tracking-prod-records.csv     movie watch/rewatch events (entity_type=movie)

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


def load_export(data_dir: Path):
    full_history = {}  # show_name -> list of (season, episode, watched_at)
    with open(data_dir / "tracking-prod-records-v2.csv", newline="") as f:
        for row in csv.DictReader(f):
            show = row["series_name"]
            if not show or not row.get("season_number") or not row.get("episode_number"):
                continue
            full_history.setdefault(show, []).append(
                (int(row["season_number"]), int(row["episode_number"]), row["created_at"])
            )

    followed = []
    with open(data_dir / "followed_tv_show.csv", newline="") as f:
        for row in csv.DictReader(f):
            followed.append(row["tv_show_name"])

    all_shows = set(followed) | set(full_history)
    return all_shows, full_history


def load_movies(data_dir: Path):
    # movie_watch_events: movie_name -> list of watched_at timestamps (watch + rewatch)
    # movie_release_dates: movie_name -> release_date, used to disambiguate TMDB search
    movie_watch_events = {}
    movie_release_dates = {}
    path = data_dir / "tracking-prod-records.csv"
    if not path.exists():
        return movie_watch_events, movie_release_dates
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("entity_type") != "movie" or row.get("type") not in ("watch", "rewatch"):
                continue
            name = row["movie_name"]
            if not name:
                continue
            movie_watch_events.setdefault(name, []).append(row["created_at"])
            if row.get("release_date"):
                movie_release_dates[name] = row["release_date"]
    return movie_watch_events, movie_release_dates


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


def tmdb_search_movie(movie_name, release_date):
    # TV Time uses 0001-01-01 as a sentinel for "unknown release date"
    year = release_date[:4] if release_date and not release_date.startswith("0001") else None

    url = "https://api.themoviedb.org/3/search/movie"
    params = {"query": movie_name, "include_adult": "false", "language": "en-US", "page": 1}
    if year:
        params["year"] = year
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])

    exact = [r for r in results if r["title"] == movie_name]
    if exact:
        return exact[0]["id"]
    return results[0]["id"] if results else None


def build_seen_history(show_name, full_history):
    if show_name not in full_history:
        return []
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_dir", type=Path, help="path to the unzipped TV Time GDPR export")
    parser.add_argument("-o", "--output", type=Path, default=Path("ryot_data.json"), help="output JSON path (default: ryot_data.json)")
    args = parser.parse_args()

    if not TOKEN:
        print("error: TMDB_READ_ACCESS_TOKEN environment variable is not set", file=sys.stderr)
        sys.exit(1)

    all_shows, full_history = load_export(args.export_dir)
    movie_watch_events, movie_release_dates = load_movies(args.export_dir)
    print(f"{len(all_shows)} shows and {len(movie_watch_events)} watched movies to match against TMDB")

    ryot_json = []
    not_found = []
    for i, show_name in enumerate(sorted(all_shows), 1):
        try:
            tmdb_id = tmdb_search(show_name)
        except requests.RequestException as e:
            print(f"[show {i}/{len(all_shows)}] {show_name}: TMDB request failed ({e})")
            not_found.append(show_name)
            continue

        if tmdb_id is None:
            print(f"[show {i}/{len(all_shows)}] {show_name}: no TMDB match")
            not_found.append(show_name)
            continue

        seen_history = build_seen_history(show_name, full_history)
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
        print(f"[show {i}/{len(all_shows)}] {show_name} -> TMDB {tmdb_id} ({len(seen_history)} episodes)")
        time.sleep(0.05)  # stay well under TMDB's rate limit

    for i, (movie_name, watched_ats) in enumerate(sorted(movie_watch_events.items()), 1):
        try:
            tmdb_id = tmdb_search_movie(movie_name, movie_release_dates.get(movie_name))
        except requests.RequestException as e:
            print(f"[movie {i}/{len(movie_watch_events)}] {movie_name}: TMDB request failed ({e})")
            not_found.append(movie_name)
            continue

        if tmdb_id is None:
            print(f"[movie {i}/{len(movie_watch_events)}] {movie_name}: no TMDB match")
            not_found.append(movie_name)
            continue

        seen_history = [
            {"progress": "100", "started_on": to_iso(watched_at), "ended_on": to_iso(watched_at), "state": "completed"}
            for watched_at in sorted(watched_ats)
        ]
        ryot_json.append(
            {
                "collections": [],
                "identifier": str(tmdb_id),
                "lot": "movie",
                "reviews": [],
                "source": "tmdb",
                "source_id": str(tmdb_id),
                "seen_history": seen_history,
            }
        )
        print(f"[movie {i}/{len(movie_watch_events)}] {movie_name} -> TMDB {tmdb_id} ({len(seen_history)} watch(es))")
        time.sleep(0.05)

    # Ryot's Generic JSON importer expects a CompleteExport object, not a bare array
    complete_export = {"metadata": ryot_json}
    with open(args.output, "w") as f:
        json.dump(complete_export, f, indent=2)

    print(f"\nWrote {len(ryot_json)} items to {args.output}")
    if not_found:
        print(f"\n{len(not_found)} items need manual TMDB lookup (name mismatch or removed from TMDB):")
        for name in not_found:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
