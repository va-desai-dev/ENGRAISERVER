"""Entry point for the standalone distribution (not a second CLI)."""

import multiprocessing

from engrai_server.cli import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
