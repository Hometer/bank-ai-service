"""Supply non-secret paths directly to ego-browser's isolated JavaScript runtime."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--space", type=int, help="Resume the task space used for this verification")
    parser.add_argument("--version", choices=["v12", "v13", "v14"], default="v12")
    parser.add_argument("--catalog", type=Path, help="Isolated V1.3 catalog copy for disable/restore checks")
    args = parser.parse_args()
    script = "verify_browser_v13.mjs" if args.version in ("v13", "v14") else "verify_browser.mjs"
    browser_source = Path(__file__).with_name(script).read_text()
    if args.version == "v14":
        browser_source = browser_source.replace("http://127.0.0.1:18763", "http://127.0.0.1:18765")
        browser_source = browser_source.replace("docs/evidence/阶段2/V1.3修订", "docs/evidence/阶段2/V1.4修订")
        browser_source = browser_source.replace("银行客服 V1.3 本机验收", "银行客服 V1.4 验收")
        browser_source = browser_source.replace("await task.finish({keep:[]});", "")
        browser_source += "\n" + Path(__file__).with_name("verify_browser_v14.mjs").read_text()
    source = (
        "globalThis.bankDemoProjectRoot = " + json.dumps(str(ROOT)) + ";\n"
        "globalThis.bankDemoBrowserSpace = " + json.dumps(args.space) + ";\n"
        "globalThis.bankDemoTestCatalog = " + json.dumps(str(args.catalog.resolve()) if args.catalog else None) + ";\n"
        + browser_source
    )
    subprocess.run(["ego-browser", "nodejs"], input=source, text=True, check=True)


if __name__ == "__main__":
    main()
