"""Pre-Build: erzeugt FIRMWARE_VERSION aus dem Git-Stand.

Damit steht im Web-Interface neben jedem Terminal, welcher Commit darauf
laeuft - ohne die Datei manuell pflegen zu muessen.
"""

import subprocess

Import("env")  # noqa: F821 - von PlatformIO bereitgestellt


def git(*args: str, fallback: str = "") -> str:
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return fallback


commit = git("rev-parse", "--short", "HEAD", fallback="nogit")
dirty = "+" if git("status", "--porcelain") else ""
version = f"2.0-{commit}{dirty}"

print(f"Firmware-Version: {version}")
env.Append(CPPDEFINES=[("FIRMWARE_VERSION", env.StringifyMacro(version))])  # noqa: F821
