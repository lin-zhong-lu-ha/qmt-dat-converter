"""Launch from a checkout without installing the package."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from qmt_dat_converter.__main__ import cli_main, gui_main


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main())
    gui_main()
