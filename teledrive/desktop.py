"""PyInstaller entry point and desktop/CLI dispatcher.

The same frozen executable launches the Qt GUI normally and handles its
internal --cli subprocess invocations without relying on a source .py file.
"""
import sys

from teledrive.uploader import main as uploader_main
from app.main import main as desktop_main


def main() -> int:
    if "--cli" in sys.argv[1:]:
        sys.argv.remove("--cli")
        return uploader_main()
    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
