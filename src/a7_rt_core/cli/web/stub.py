# cli/web/stub.py — Web interface stub (not implemented)

"""Web interface placeholder for A7-RT.

This module serves as a stub for a future web-based viewer.
The web interface is planned but not currently implemented.
"""


def run_web_server(host: str = "localhost", port: int = 8080, **kwargs) -> int:
    """Stub web server entry point.

    Prints a message indicating the web interface is not yet implemented
    and directs users to the CLI commands.

    Args:
        host: Ignored in stub.
        port: Ignored in stub.
        **kwargs: Ignored in stub.

    Returns:
        Exit code 1 to indicate feature not available.
    """
    print("=" * 60)
    print("A7-RT Web Interface")
    print("=" * 60)
    print()
    print("The web interface is a planned feature and not yet implemented.")
    print()
    print("Please use the CLI commands instead:")
    print()
    print("  python -m cli --help         Show all available commands")
    print("  python -m cli init <path>    Initialize a new session")
    print("  python -m cli run <path>     Run a session headlessly")
    print("  python -m cli narrative      View session narrative")
    print()
    print("=" * 60)
    return 1
