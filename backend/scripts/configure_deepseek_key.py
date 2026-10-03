"""Store a user-entered Key locally; never print it or make a network request."""

import os
import re
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[2]
DIALOG = '''
tell application "System Events"
    activate
    set resultDialog to display dialog "请在下方粘贴 DeepSeek API Key。字符会隐藏；仅保存到本机后端配置，不发起模型请求。" default answer "" with hidden answer with title "本地配置 DeepSeek Key" buttons {"取消", "保存到本机"} default button "保存到本机" cancel button "取消"
    return text returned of resultDialog
end tell
'''


def store_key(root: Path, key: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{16,512}", key):
        raise ValueError("Key format unavailable")
    target = root / ".env"
    if target.is_symlink():
        raise ValueError("Unsupported configuration path")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", ".env"], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if ignored.returncode != 0 or tracked.returncode != 1:
        raise ValueError("Configuration is not excluded from Git")
    values = {
        "MODEL_MODE": "demo",
        "MODEL_BASE_URL": "https://api.deepseek.com",
        "MODEL_NAME": "deepseek-flash",
        "MODEL_API_KEY": key,
    }
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    output: list[str] = []
    written: set[str] = set()
    for binding in parse_stream(StringIO(original)):
        if binding.error:
            raise ValueError("Existing configuration needs repair")
        if binding.key in values:
            if binding.key not in written:
                output.append(f"{binding.key}='{values[binding.key]}'\n")
                written.add(binding.key)
        else:
            output.append(binding.original.string)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(f"{name}='{value}'\n" for name, value in values.items() if name not in written)
    descriptor, name = tempfile.mkstemp(prefix=".env.key-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("".join(output))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", DIALOG], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=300, check=False,
        )
        if result.returncode != 0:
            print("未保存：已取消或系统未允许打开输入窗口。")
            return 1
        store_key(ROOT, result.stdout.strip())
    except Exception:
        # Exceptions can contain subprocess output; never display their contents.
        print("未保存：输入或本地配置检查未通过。请把这条提示告诉 AI，不要发送 Key。")
        return 1
    print("Key 已保存到本机后端配置，文件仅当前用户可读写。")
    print("当前仍为规则演示模式，真实模型待验。无需发送 Key 或截图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
