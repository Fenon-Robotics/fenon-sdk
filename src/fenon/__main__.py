"""Fenon CLI commands for starting, configuring and revoking a device session."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from . import __version__
from ._core import (
    AuthError,
    ConsoleSession,
    DeviceCredentials,
    DeviceSession,
    FenonError,
    Settings,
    changed_project,
    credential_path,
    edit_credentials,
    finish_replacement,
    private_url,
    read_credentials,
    save_credentials,
)


def _terminal_text(value: str) -> str:
    return "".join(character if character.isprintable() else " " for character in value)


def _choose(console: ConsoleSession, token: str, project: str | None) -> tuple[str, str]:
    catalog = console.projects(token)
    choices = [
        (item.id, f"{workspace.name} / {item.name}")
        for workspace in catalog.workspaces
        for item in workspace.projects
    ]
    if not choices:
        raise AuthError("Set up a workspace and project in the Fenon console first.", code="no_projects")
    if project is not None:
        if any(identifier == project for identifier, _ in choices):
            return project, catalog.user.email
        raise AuthError("That project is unavailable in your workspaces.", code="project_not_found")
    if len(choices) == 1:
        return choices[0][0], catalog.user.email
    for index, (identifier, label) in enumerate(choices, start=1):
        print(f"{index}. {_terminal_text(label)} ({identifier})")
    try:
        selected = int(input("Default project: "))
    except (EOFError, ValueError):
        selected = 0
    if not 1 <= selected <= len(choices):
        raise AuthError("Choose a project number or use --project with its ID.", code="project_required")
    return choices[selected - 1][0], catalog.user.email


def _discard_unstored(console: ConsoleSession, path: Path, token: str, *, save_started: bool) -> None:
    with suppress(FenonError):
        if save_started:
            stored = read_credentials(path)
            if stored is not None and token in (
                stored.token,
                stored.retired.token if stored.retired else None,
            ):
                return
        console.revoke(token)


def _login(args: argparse.Namespace) -> None:
    settings = Settings()
    api_base = private_url(settings.base_url)
    console = ConsoleSession(settings.console_url)
    grant = console.begin()
    code = grant.user_code
    print(f"Open {grant.verification_uri_complete} and confirm {code[:4]}-{code[4:]}.", flush=True)
    if not args.no_browser:
        with suppress(webbrowser.Error):
            webbrowser.open(grant.verification_uri_complete)
    token = console.redeem(grant)
    path = credential_path()
    save_started = False
    try:
        project, email = _choose(console, token, args.project)
        with edit_credentials(path):
            previous = read_credentials(path)
            retired = None
            if previous is not None:
                previous = finish_replacement(path, previous)
                retired = DeviceSession(previous.token, previous.console)
            current = DeviceCredentials(token, console.origin, api_base, project, retired)
            save_started = True
            save_credentials(path, current)
            finish_replacement(path, current)
    except (FenonError, KeyboardInterrupt):
        _discard_unstored(console, path, token, save_started=save_started)
        raise
    print(f"Signed in as {_terminal_text(email)}. Default project: {project}.")


def _logout() -> None:
    path = credential_path()
    with edit_credentials(path):
        credentials = read_credentials(path)
        if credentials is None:
            print("You're signed out.")
            return
        ConsoleSession(credentials.console).revoke(credentials.token)
        try:
            finish_replacement(path, credentials)
        except FenonError as error:
            raise AuthError(
                "This device signed out. Run `fenon logout` again to revoke the previous login.",
                code="pending_revocation",
            ) from error
        path.unlink(missing_ok=True)
    print("Signed out.")


def _set_project(project: str | None) -> None:
    path = credential_path()
    with edit_credentials(path):
        credentials = read_credentials(path)
        if credentials is None:
            raise AuthError("Sign in first with `fenon login`.", code="missing_credentials")
        credentials = finish_replacement(path, credentials)
        selected, _ = _choose(ConsoleSession(credentials.console), credentials.token, project)
        save_credentials(path, changed_project(credentials, selected))
    print(f"Default project: {selected}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fenon")
    parser.add_argument("--version", action="version", version=f"fenon {__version__}")
    commands = parser.add_subparsers(dest="command")
    login = commands.add_parser("login")
    login.add_argument("--project", metavar="ID")
    login.add_argument("--no-browser", action="store_true")
    commands.add_parser("logout")
    projects = commands.add_parser("projects").add_subparsers(dest="project_command", required=True)
    projects.add_parser("use").add_argument("project", nargs="?", metavar="ID")
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            _login(args)
        elif args.command == "logout":
            _logout()
        elif args.command == "projects":
            _set_project(args.project)
        else:
            print(f"fenon {__version__}")
    except FenonError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
