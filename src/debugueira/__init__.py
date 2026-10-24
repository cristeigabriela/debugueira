"""debugueira: a WinDbg session manager for agents."""

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
