"""Keep what a release saves, so every later version is tested against it.

    python tools/snapshot_release.py v1.0.0           write the snapshot
    python tools/snapshot_release.py --check v1.0.0   the release build runs this

The snapshot is test_fixtures/released/<version>/config.json and history.db,
as this version's code writes them, with a miner in each mode. test_upgrade.py
loads every snapshot with the current code. A later change that cannot read
one, or that changes a value it saved, fails CI until it has a migration.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import boards  # noqa: E402
import config  # noqa: E402
import history  # noqa: E402

RELEASED = os.path.join(ROOT, "test_fixtures", "released")
VERSION = re.compile(r"v\d+\.\d+\.\d+")
SUPRA_402 = boards.board_for_info({"ASICModel": "BM1368", "boardVersion": "402"})
# A fixed moment, so the same code always writes the same snapshot.
MOMENT = 1_800_000_000


def snapshot_config():
    """config.json after setup, with a miner in each mode."""
    saved = config.get_default_config()
    saved.update(
        setup_done=True,
        default_mode="balanced",
        supply_watts=30.0,
        weather_enabled=True,
        location={
            "name": "Moose Jaw, Saskatchewan, Canada",
            "latitude": 50.40005,
            "longitude": -105.53445,
            "timezone": "America/Regina",
            "source": "search",
        },
    )
    for key in config.INTERNET_SWITCHES:
        saved[key] = True
    miners = [
        ("192.168.1.31", "fleet", boards.GAMMA_601, "max_hashrate"),
        ("192.168.1.32", "stock", boards.GAMMA_601, "balanced"),
        ("192.168.1.33", "office", SUPRA_402, "efficiency"),
    ]
    for ip, name, board, mode in miners:
        record = config.new_miner_record(
            f"{board.asic.model} {board.version}", ip, name, saved, board, mode
        )
        record["enabled"] = True
        record["repasted_on"] = "2026-10-01"
        if not board.verified:
            record["experimental_ok"] = True
        saved["miners"].append(record)
    return saved


def write_history(path):
    """history.db with one settled sample and one reset."""
    info = {
        "frequency": 525,
        "coreVoltage": 1150,
        "hashRate_10m": 1100.0,
        "errorPercentage": 0.5,
        "temp": 58.0,
        "vrTemp": 61.0,
        "power": 17.5,
    }
    weather = {"fetched_at": MOMENT, "outdoor_temp": -12.5, "weather_code": 71}
    sample = history.sample_from_info(
        "192.168.1.31", "fleet", info, "hold", True, weather, MOMENT
    )
    history.record_samples([sample], path)
    history.record_event("192.168.1.31", "reset", MOMENT - 3600, path)


def _schema(path):
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    return [(kind, name, " ".join((sql or "").split())) for kind, name, sql in rows]


def released_versions(root=None):
    """Tags with a snapshot, oldest first by name."""
    root = root or RELEASED
    if not os.path.isdir(root):
        return []
    return sorted(
        name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))
    )


def _current_schema():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "history.db")
        write_history(path)
        return _schema(path)


def write_snapshot(version, root=None):
    """Write the snapshot for `version`. A released snapshot is never replaced."""
    directory = os.path.join(root or RELEASED, version)
    if os.path.exists(directory):
        raise FileExistsError(f"{directory} already exists. Released snapshots stay.")
    os.makedirs(directory)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as file:
        json.dump(snapshot_config(), file, indent=4)
        file.write("\n")
    write_history(os.path.join(directory, "history.db"))
    return directory


def check_snapshot(version, root=None):
    """Problems with the snapshot for `version`. Empty when it matches this code."""
    directory = os.path.join(root or RELEASED, version)
    config_path = os.path.join(directory, "config.json")
    history_file = os.path.join(directory, "history.db")
    problems = []
    if not os.path.exists(config_path):
        problems.append(f"{config_path} is missing.")
    else:
        with open(config_path, encoding="utf-8") as file:
            if json.load(file) != snapshot_config():
                problems.append(f"{config_path} is not what this code writes.")
    if not os.path.exists(history_file):
        problems.append(f"{history_file} is missing.")
    elif _schema(history_file) != _current_schema():
        problems.append(f"{history_file} has a different schema from this code.")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="The release tag, for example v1.0.0.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check the snapshot instead of writing it.",
    )
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        print(f"{args.version} is not a release tag like v1.0.0.")
        return 2
    if args.check:
        problems = check_snapshot(args.version)
        for problem in problems:
            print(problem)
        if problems:
            print(
                f"Run python tools/snapshot_release.py {args.version} on the "
                "released code and commit test_fixtures/released before tagging."
            )
            return 1
        print(f"The {args.version} snapshot matches this code.")
        return 0
    try:
        directory = write_snapshot(args.version)
    except FileExistsError as exc:
        print(exc)
        return 1
    print(f"Wrote {directory}. Commit it with the release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
