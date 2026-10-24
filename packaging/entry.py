"""PyInstaller's entry point: the same `main` the `debugueira` console script runs."""

import sys

from debugueira.cli import main

sys.exit(main())
