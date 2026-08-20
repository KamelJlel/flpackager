"""Entry point for the packaged executable and ``python -m flpackager``.

Launches the GUI by default. Falls back to the CLI when the first argument is
a known subcommand, so one binary serves both.
"""

import sys

CLI_COMMANDS = {"pack", "unpack", "--help", "-h", "--version"}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS:
        from flpackager.cli import main as cli_main

        return cli_main()

    from flpackager.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
