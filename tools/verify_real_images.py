"""Replay real OCR samples against a temporary profile."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import Api


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nickname",
        required=True,
        help="The player's in-game nickname in these screenshots.",
    )
    parser.add_argument(
        "--rank", type=Path, default=ROOT / "data/sample/references/rank.png"
    )
    parser.add_argument(
        "--summary", type=Path, default=ROOT / "data/sample/references/summary.png"
    )
    parser.add_argument("--hud", type=Path)
    parser.add_argument("--lobby", type=Path)
    args = parser.parse_args()
    for path in (args.rank, args.summary, args.hud, args.lobby):
        if path is not None and not path.is_file():
            parser.error(f"Missing sample: {path.name}")
    output = ROOT / "output/tmp"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        api = Api(tmp, desktop=False)
        try:
            api._store.configure({"self_name": args.nickname})
            round_id, captured_at = api._journal.upload_round()

            def ingest(path):
                return api._process_file(
                    path, manual=True, round_id=round_id, captured_at=captured_at
                )

            rank = ingest(args.rank)
            summary = ingest(args.summary)
            if not (
                rank.get("saved")
                and summary.get("saved")
                and rank["id"] == summary["id"]
            ):
                raise ValueError(
                    "The rank and summary samples did not produce one match for this profile"
                )
            ingest(args.summary)
            state = api.state()
            if len(state["matches"]) != 1:
                raise ValueError("Repeated evidence created an extra match")
            match = state["matches"][0]
            report = {
                "matches": 1,
                "rank_delta": match["rank"]["delta"],
                "placement": match["summary"]["placement"],
            }
            if args.hud:
                with Image.open(args.hud) as image:
                    hud = api._save_hud_backup(image.convert("RGB"))
                if not hud or hud["verified"]:
                    raise ValueError(
                        "HUD sample did not produce an unverified snapshot"
                    )
                report["hud_snapshot"] = True
            if args.lobby:
                with Image.open(args.lobby) as image:
                    lobby = api._save_lobby(image.convert("RGB"), force=True)
                if not lobby or not api.state()["lobby_rank"]:
                    raise ValueError("Lobby sample did not produce a rank checkpoint")
                report["lobby_checkpoint"] = True
            print(json.dumps(report, ensure_ascii=False))
        finally:
            api._store.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
