"""Supply non-secret paths directly to ego-browser's isolated JavaScript runtime."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space", type=int, help="Resume the task space used for this verification")
    args = parser.parse_args()
    source = (
        "globalThis.bankDemoProjectRoot = " + json.dumps(str(ROOT)) + ";\n"
        "globalThis.bankDemoBrowserSpace = " + json.dumps(args.space) + ";\n"
        + Path(__file__).with_name("verify_browser.mjs").read_text()
    )
    subprocess.run(["ego-browser", "nodejs"], input=source, text=True, check=True)


if __name__ == "__main__":
    main()
