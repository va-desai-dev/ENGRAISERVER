"""Command line entry point for ENGRAI SERVER.

    engrai-server install --now
    engrai-server serve --host 0.0.0.0 --port 8400
    engrai-server config
    engrai-server preflight
    engrai-server routes
    engrai-server runtime status

Flags override environment variables, which override the built-in defaults.
That order is implemented by writing the flag into the environment before
Settings is first constructed, so there is one precedence chain rather than
two that can disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .identity import DISTRIBUTION_NAME, PRODUCT_NAME


DESCRIPTION = "Sovereign host control plane and runtime orchestrator."

# Flag -> environment variable. Settings reads the environment, so this is all
# the wiring an override needs.
OVERRIDES = {
    "host": "ENGRAI_BIND_HOST",
    "port": "ENGRAI_BIND_PORT",
    "forwarded_allow_ips": "ENGRAI_FORWARDED_ALLOW_IPS",
    "home": "ENGRAI_HOME",
    "runtime_dir": "ENGRAI_RUNTIME_DIR",
    "text_engine_port": "ENGRAI_TEXT_ENGINE_PORT",
    "routes": "ENGRAI_ROUTE_DIR",
}


def apply_overrides(args: argparse.Namespace) -> None:
    for attribute, variable in OVERRIDES.items():
        value = getattr(args, attribute, None)
        if value is not None:
            os.environ[variable] = str(value)

    # Settings is cached on first construction. In a fresh process nothing has
    # built it yet, but anything that imports it before main() runs would
    # otherwise pin the pre-override values and silently ignore every flag.
    from .settings import get_settings

    get_settings.cache_clear()


def version() -> str:
    try:
        from importlib.metadata import version as package_version

        return package_version(DISTRIBUTION_NAME)
    except Exception:
        return "unknown"


def command_serve(args: argparse.Namespace) -> int:
    # Imported after the overrides land, because Settings is built on import
    # and cached from then on.
    import uvicorn

    from .settings import get_settings

    settings = get_settings()
    print(
        f"{PRODUCT_NAME} {version()} → http://{settings.bind_host}:{settings.bind_port}",
        file=sys.stderr,
    )
    uvicorn.run(
        "engrai_server.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_peers,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).resolve().parent)] if args.reload else None,
    )
    return 0


def command_config(args: argparse.Namespace) -> int:
    from .host import describe_paths
    from .settings import get_settings

    settings = get_settings()
    paths = describe_paths()
    from .runtime import IMAGE_RUNTIME, inspect_runtime

    runtime = inspect_runtime(settings.runtime_dir, verify=False)
    image_runtime = inspect_runtime(settings.runtime_dir, IMAGE_RUNTIME, verify=False)
    rows: list[tuple[str, Any, str]] = [
        ("layout", paths["layout"], "ENGRAI_HOME"),
        ("config_home", paths["config"], "XDG_CONFIG_HOME"),
        ("data_home", paths["data"], "XDG_DATA_HOME"),
        ("state_home", paths["state"], "XDG_STATE_HOME"),
        ("environment", paths["environment"], ""),
        ("bind_host", settings.bind_host, "ENGRAI_BIND_HOST"),
        ("bind_port", settings.bind_port, "ENGRAI_BIND_PORT"),
        ("forwarded_allow_ips", settings.forwarded_allow_ips, "ENGRAI_FORWARDED_ALLOW_IPS"),
        ("runtime_dir", settings.runtime_dir, "ENGRAI_RUNTIME_DIR"),
        ("primary_executable", runtime["executable"] or "not installed", ""),
        ("image_executable", image_runtime["executable"] or "not installed", ""),
        ("text_engine", settings.text_engine_base_url, "ENGRAI_TEXT_ENGINE_PORT"),
        ("image_engine", settings.image_engine_base_url, "ENGRAI_IMAGE_ENGINE_PORT"),
        ("routes", settings.route_dir, "ENGRAI_ROUTE_DIR"),
        ("credentials", settings.credentials_path, "ENGRAI_CREDENTIALS_PATH"),
        ("preferences", settings.preferences_path, "ENGRAI_PREFERENCES_PATH"),
        ("deployments", settings.deployment_dir, "ENGRAI_DEPLOYMENT_DIR"),
        ("model_search_roots", ":".join(str(r) for r in settings.search_roots), "ENGRAI_MODEL_SEARCH_ROOTS"),
        ("model_download_dir", settings.model_download_dir, "ENGRAI_MODEL_DOWNLOAD_DIR"),
    ]
    if args.json:
        print(json.dumps({name: str(value) for name, value, _ in rows}, indent=2))
        return 0

    print(f"\nENGRAI SERVER {version()}\n")
    for name, value, variable in rows:
        source = "env" if variable and os.environ.get(variable) else "default"
        print(f"  {name:<20} {str(value):<52} [{source}]")
    reach = (
        "this machine only"
        if settings.bind_host.startswith("127.")
        else "every interface"
        if settings.bind_host == "0.0.0.0"
        else f"only the network holding {settings.bind_host}"
    )
    print(f"\n  Listening reaches: {reach}\n")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    from . import preflight

    sys.argv = ["preflight"] + (["--json"] if args.json else [])
    return preflight.main()


def command_routes(args: argparse.Namespace) -> int:
    from .profiles import ProfileStore
    from .settings import get_settings

    settings = get_settings()
    store = ProfileStore(settings.route_dir, settings.search_roots)
    profiles = store.list()
    if args.json:
        print(json.dumps([
            {"id": p.id, "name": p.name, "routable": p.routable, "model": str(p.model_path)}
            for p in profiles
        ], indent=2))
        return 0
    if not profiles:
        print(f"No routes in {settings.route_dir}")
        print("Create one in the web UI or install a manifest from @models/catalog/")
        return 0
    print()
    for profile in profiles:
        mark = "ok " if profile.routable else "MISSING"
        print(f"  [{mark}] {profile.id:<26} {profile.name}")
        print(f"           {profile.model_path}")
        for path in profile.missing_paths:
            print(f"           missing companion file: {path}")
    print()
    return 0


def _runtime_result(status: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, indent=2))
        return
    from .runtime import print_status

    print_status(status)


def command_runtime_status(args: argparse.Namespace) -> int:
    from .runtime import IMAGE_RUNTIME, TEXT_RUNTIME, inspect_runtime
    from .settings import get_settings

    runtime_id = IMAGE_RUNTIME if args.runtime == "image" else TEXT_RUNTIME
    status = inspect_runtime(get_settings().runtime_dir, runtime_id)
    _runtime_result(status, as_json=args.json)
    return 0 if status["present"] and status["verified"] else 1


def command_runtime_install(args: argparse.Namespace) -> int:
    from .runtime import RuntimeManagerError, install_runtime
    from .settings import get_settings

    try:
        status = install_runtime(
            Path(args.bundle),
            get_settings().runtime_dir,
            force=args.force,
        )
    except RuntimeManagerError as exc:
        print(f"Runtime install failed: {exc}", file=sys.stderr)
        return 1
    _runtime_result(status, as_json=args.json)
    return 0


def _print_paths(*, as_json: bool = False) -> None:
    from .host import describe_paths

    paths = describe_paths()
    if as_json:
        print(json.dumps(paths, indent=2))
        return
    print("ENGRAI SERVER filesystem layout:")
    for name, value in paths.items():
        print(f"  {name:<12} {value}")


def command_init(args: argparse.Namespace) -> int:
    from .host import initialize_layout

    initialize_layout()
    _print_paths(as_json=args.json)
    return 0


def command_install(args: argparse.Namespace) -> int:
    """Prepare the current Linux user without depending on a source checkout."""

    from .host import HostServiceError, initialize_layout, install_user_service
    from .runtime import RuntimeManagerError, inspect_runtime, install_runtime
    from .settings import get_settings

    initialize_layout()
    settings = get_settings()
    if not args.no_runtime:
        current = inspect_runtime(settings.runtime_dir)
        if current["verified"] and not args.runtime_bundle:
            print(f"Keeping active runtime: {current['path']}")
        elif not args.runtime_bundle:
            print(
                "Runtime install requires an ENGRAI bundle: "
                "engrai-server install --runtime-bundle <bundle.tar.gz>",
                file=sys.stderr,
            )
            return 1
        else:
            try:
                status = install_runtime(
                    Path(args.runtime_bundle),
                    settings.runtime_dir,
                    force=args.force_runtime,
                )
            except RuntimeManagerError as exc:
                print(f"Runtime install failed: {exc}", file=sys.stderr)
                return 1
            _runtime_result(status, as_json=False)
    try:
        unit = install_user_service(
            executable=args.executable,
            enable=not args.no_enable,
            start=args.now,
        )
    except HostServiceError as exc:
        print(f"Host install failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed user service: {unit}")
    if not args.now:
        print("Start it with: engrai-server service start")
    return 0


def command_service_install(args: argparse.Namespace) -> int:
    from .host import HostServiceError, install_user_service

    try:
        unit = install_user_service(
            executable=args.executable,
            enable=not args.no_enable,
            start=args.now,
        )
    except HostServiceError as exc:
        print(f"Service install failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed user service: {unit}")
    return 0


def command_service_action(args: argparse.Namespace) -> int:
    from .host import HostServiceError, service_action

    try:
        result = service_action(args.service_command)
    except HostServiceError as exc:
        print(f"Service command failed: {exc}", file=sys.stderr)
        return 1
    if result.stdout:
        print(result.stdout, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engrai-server", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"engrai-server {version()}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_shared(sub: argparse.ArgumentParser) -> None:
        # Every override is accepted everywhere, so `config --host 0.0.0.0`
        # previews exactly what `serve --host 0.0.0.0` would do.
        sub.add_argument(
            "--host",
            help="address to bind. 127.0.0.1 (default) is reachable only from "
            "this machine; 0.0.0.0 serves every interface; a specific address "
            "serves only the network holding it",
        )
        sub.add_argument("--port", type=int, help="port to bind, when the default is taken")
        sub.add_argument(
            "--forwarded-allow-ips",
            help="proxy addresses whose X-Forwarded-* headers are trusted",
        )
        sub.add_argument(
            "--home",
            help="use a portable data root instead of the default XDG layout",
        )
        sub.add_argument("--runtime-dir", help="directory containing managed ENGRAI runtimes")
        sub.add_argument("--routes", help="directory holding route JSON files")
        sub.add_argument(
            "--text-engine-port",
            type=int,
            help="loopback port for the text worker (the image worker uses the next port)",
        )

    serve = subparsers.add_parser("serve", help="run the gateway")
    serve.add_argument("--reload", action="store_true", help="restart on source changes")
    add_shared(serve)
    serve.set_defaults(func=command_serve)

    init = subparsers.add_parser(
        "init",
        help="create the user configuration, data, state, and cache directories",
    )
    init.add_argument("--json", action="store_true")
    add_shared(init)
    init.set_defaults(func=command_init)

    install = subparsers.add_parser(
        "install",
        help="install the runtime and systemd user service for this Linux user",
    )
    install.add_argument("--runtime-bundle", help="verified ENGRAI runtime bundle to install")
    install.add_argument("--no-runtime", action="store_true")
    install.add_argument("--force-runtime", action="store_true")
    install.add_argument("--no-enable", action="store_true")
    install.add_argument("--now", action="store_true", help="start the service immediately")
    install.add_argument("--executable", help=argparse.SUPPRESS)
    add_shared(install)
    install.set_defaults(func=command_install)

    config = subparsers.add_parser("config", help="show effective settings and where they came from")
    config.add_argument("--json", action="store_true")
    add_shared(config)
    config.set_defaults(func=command_config)

    check = subparsers.add_parser("preflight", help="check dependencies and routes")
    check.add_argument("--json", action="store_true")
    add_shared(check)
    check.set_defaults(func=command_preflight)

    routes = subparsers.add_parser("routes", help="list saved model routes")
    routes.add_argument("--json", action="store_true")
    add_shared(routes)
    routes.set_defaults(func=command_routes)

    runtime = subparsers.add_parser(
        "runtime",
        help="install and inspect ENGRAI-owned inference runtime bundles",
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)

    runtime_status = runtime_commands.add_parser(
        "status",
        help="inspect the configured runtime and verify its install receipt",
    )
    runtime_status.add_argument(
        "--runtime",
        choices=("text", "image"),
        default="text",
        help="which runtime to inspect (default: text)",
    )
    runtime_status.add_argument("--json", action="store_true")
    add_shared(runtime_status)
    runtime_status.set_defaults(func=command_runtime_status)

    runtime_install = runtime_commands.add_parser(
        "install",
        help="verify, install, and activate an ENGRAI runtime bundle",
    )
    runtime_install.add_argument("bundle", help="bundle directory or .tar.gz path")
    runtime_install.add_argument(
        "--force",
        action="store_true",
        help="replace an installed bundle at the same version and target",
    )
    runtime_install.add_argument("--json", action="store_true")
    add_shared(runtime_install)
    runtime_install.set_defaults(func=command_runtime_install)

    service = subparsers.add_parser(
        "service",
        help="install and operate the systemd user service",
    )
    service_commands = service.add_subparsers(dest="service_command", required=True)
    service_install = service_commands.add_parser(
        "install", help="write and enable the user service"
    )
    service_install.add_argument("--now", action="store_true")
    service_install.add_argument("--no-enable", action="store_true")
    service_install.add_argument("--executable", help=argparse.SUPPRESS)
    add_shared(service_install)
    service_install.set_defaults(func=command_service_install)
    for action in ("start", "stop", "restart", "status"):
        command = service_commands.add_parser(action, help=f"{action} the user service")
        command.set_defaults(func=command_service_action)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply_overrides(args)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
