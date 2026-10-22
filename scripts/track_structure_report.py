#!/usr/bin/env python3
"""
Track Structure Report v3 - Mit korrekten GPS-Punkten
"""
import sys
import os
import importlib.util

# Load track_report_generator directly without going through package __init__
spec = importlib.util.spec_from_file_location(
    "track_report_generator",
    os.path.join(os.path.dirname(__file__), '..', 'artrack', 'routes', 'track_report_generator.py')
)
track_report_generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(track_report_generator)
generate_track_report = track_report_generator.generate_track_report

API_KEY = os.environ.get("API_KEY", "")
BASE_URL = "https://api.arkturian.com/artrack"


if __name__ == "__main__":
    track_id = 24  # Default
    show_descriptions = False

    # Parse arguments
    for arg in sys.argv[1:]:
        if arg == "-full":
            show_descriptions = True
        else:
            try:
                track_id = int(arg)
            except ValueError:
                print(f"⚠️  Unknown argument: {arg}")
                print("Usage: python track_structure_report.py [track_id] [-full]")
                sys.exit(1)

    # Generate and print report using shared logic
    report = generate_track_report(
        track_id=track_id,
        show_descriptions=show_descriptions,
        api_key=API_KEY,
        base_url=BASE_URL
    )
    print(report)
