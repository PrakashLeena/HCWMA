"""Owner commands for the Gmail login (build step 15). Run by hand, never by an agent:

    python -m integrations.adapters.official_api.gmail_login import-client <client_secret.json>
    python -m integrations.adapters.official_api.gmail_login login      # opens your browser
    python -m integrations.adapters.official_api.gmail_login status
    python -m integrations.adapters.official_api.gmail_login logout     # forget the login

`import-client` moves the OAuth client id/secret into the OS credential store (the file is not
copied anywhere and is not touched). `login` runs the Google consent in your browser and keeps
only the refresh token in the credential store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from integrations.adapters.official_api.gmail_api import GmailApi
from integrations.adapters.official_api.gmail_auth import (
    KEY_CLIENT,
    KEY_REFRESH,
    GmailLoginNeeded,
    GmailSetupError,
    KeyringStore,
    TokenProvider,
    account_address,
    import_client,
    login,
    logout,
    remember_account,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmail_login", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser(
        "import-client", help="store the OAuth client file in the credential store"
    )
    imp.add_argument("file", type=Path)
    sub.add_parser("login", help="sign in to Gmail in your browser")
    sub.add_parser("status", help="show whether a login is saved and still works")
    sub.add_parser("logout", help="forget the saved Gmail login")
    args = parser.parse_args(argv)

    store = KeyringStore()
    try:
        if args.command == "import-client":
            client_id = import_client(args.file, store)
            print(f"OAuth client imported into the credential store ({client_id[:12]}...).")
            print("The file was not copied anywhere; keep it outside the repository.")
        elif args.command == "login":
            print("Opening your browser for the Google sign-in (Gmail: read, drafts and send).")
            login(store)
            address = GmailApi(TokenProvider(store)).profile_address()
            remember_account(store, address)
            print(f"Logged in to Gmail as {address}.")
        elif args.command == "logout":
            logout(store)
            print("The saved Gmail login was removed from the credential store.")
        else:
            print(f"OAuth client saved: {bool(store.get(KEY_CLIENT))}")
            print(f"login saved:        {bool(store.get(KEY_REFRESH))}")
            try:
                address = GmailApi(TokenProvider(store)).profile_address()
                remember_account(store, address)
                print(f"login works:        yes ({address})")
            except GmailLoginNeeded as exc:
                print(f"login works:        no - {exc}")
            print(f"account on file:    {account_address(store) or '-'}")
    except GmailSetupError as exc:
        print(f"Cannot continue: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
