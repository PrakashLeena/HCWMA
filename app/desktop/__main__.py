"""Start HCWMA as a desktop app:  python -m app.desktop

Only one HCWMA runs at a time (build step 18): the app takes the single-instance lock first.
If another HCWMA holds it, this start asks that one to bring its window forward and exits.
If something answers on the backend port without holding the lock (an HCWMA from before
step 18, or another program), this start refuses instead of reusing it — reusing an old
instance is how stale code used to keep running.

Then it starts the local backend (127.0.0.1 only) and opens the window and the tray icon.
Quit from the tray icon's menu.
"""

from __future__ import annotations

import sys

from app.desktop.backend import BackendClient, BackendThread
from backend.api.__main__ import build_app
from backend.auth.token import get_or_create_token
from backend.config import load_backend_config
from backend.reliability.instance_lock import InstanceLock


def main() -> int:
    config = load_backend_config()
    token = get_or_create_token()
    client = BackendClient(config, token)

    lock = InstanceLock()
    if not lock.acquire("desktop app"):
        print(lock.refusal(), file=sys.stderr)
        client.ask_to_show()  # bring the running one's window forward, if it can
        return 1
    try:
        if client.is_up():
            print(
                f"Something is already answering on {client.base_url} but it is not this "
                "HCWMA (an older HCWMA, or another program). Close it (tray: Quit HCWMA, or "
                "Task Manager) and start again.",
                file=sys.stderr,
            )
            return 1
        server = BackendThread(build_app(config, token), config)
        if not server.start():
            print("The HCWMA backend did not start. Is the port in use?", file=sys.stderr)
            return 1

        from app.desktop.shell import run_shell  # Qt is loaded only once the backend is up

        def on_exit() -> None:
            client.stop_everything()  # nothing keeps running after Quit
            server.stop()

        return run_shell(client, client.base_url, token, on_exit)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
