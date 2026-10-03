"""Supply non-secret paths directly to ego-browser's isolated JavaScript runtime."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space", type=int, help="Resume the task space used for this verification")
    parser.add_argument("--version", choices=["v12", "v13"], default="v12")
    parser.add_argument("--catalog", type=Path, help="Isolated V1.3 catalog copy for disable/restore checks")
    args = parser.parse_args()
    script = "verify_browser_v13.mjs" if args.version == "v13" else "verify_browser.mjs"
    source = (
        "globalThis.bankDemoProjectRoot = " + json.dumps(str(ROOT)) + ";\n"
        "globalThis.bankDemoBrowserSpace = " + json.dumps(args.space) + ";\n"
        "globalThis.bankDemoTestCatalog = " + json.dumps(str(args.catalog.resolve()) if args.catalog else None) + ";\n"
        + Path(__file__).with_name(script).read_text()
    )
    subprocess.run(["ego-browser", "nodejs"], input=source, text=True, check=True)


if __name__ == "__main__":
    main()
