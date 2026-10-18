"""Create a local review template. Blank categories and intervals are intentional."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from ml.local_io import private_path, run_cli

FIELDS = ["video_id", "binary_label", "reference_timecode_sec", "event_type", "start_sec",
          "end_sec", "timecode_sec", "group_id", "review_status", "context_visible", "notes"]


def create_template(manifest: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for record in manifest["videos"]:
            for timestamp in record["timestamps_sec"] or [""]:
                # Guard spreadsheet formula execution on later CSV import.
                safe_id = record["video_id"]
                if safe_id.startswith(("=", "+", "-", "@")):
                    safe_id = "'" + safe_id
                group = record["group_id"] or ""
                if group.startswith(("=", "+", "-", "@")):
                    group = "'" + group
                writer.writerow({"video_id": safe_id, "binary_label": record["label"],
                    "reference_timecode_sec": timestamp, "group_id": group, "review_status": "pending"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest_path = private_path(args.manifest, args.private_root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    create_template(manifest, private_path(args.output, args.private_root))
    print("Local annotation template saved; event types and intervals require human review.")


if __name__ == "__main__":
    run_cli(main)
