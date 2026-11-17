"""Start HCWMA as a desktop app:  python -m app.desktop

Starts the local backend (127.0.0.1 only) unless one is already running, then opens the
window and the tray icon. Quit from the tray icon's menu.
"""

from __future__ import annotations

import sys

from app.desktop.backend import BackendClient, BackendThread
from backend.api.__main__ import build_app
from backend.auth.token import get_or_create_token
from backend.config import load_backend_config


def main() -> int:
    config = load_backend_config()
    token = get_or_create_token()
    client = BackendClient(config, token)

    server: BackendThread | None = None
    if client.is_up():
        if not client.token_accepted():
            print(
                f"Something else is already listening on {client.base_url} and does not accept "
                "HCWMA's token. Close it and start again.",
                file=sys.stderr,
            )
            return 1
        print(f"Using the HCWMA backend already running on {client.base_url}")
    else:
        server = BackendThread(build_app(config, token), config)
        if not server.start():
            print("The HCWMA backend did not start. Is the port in use?", file=sys.stderr)
            return 1

    from app.desktop.shell import run_shell  # Qt is loaded only once the backend is up

    def on_exit() -> None:
        client.stop_everything()  # nothing keeps running after Quit
        if server is not None:
            server.stop()

    return run_shell(client, client.base_url, token, on_exit)


if __name__ == "__main__":
    raise SystemExit(main())
