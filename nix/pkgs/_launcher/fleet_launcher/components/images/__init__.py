"""Images component CLI — folded in from the former fleetkit-deployer.

Exports the `images` (build golden images) and `templates` (register them as
platform templates) Click groups via the COMMANDS manifest, which
register_cli_manifest resolves upward into `fleet images` / `fleet templates`.
Heavy back-end deps (proxmoxer, paramiko, websocket-client) are imported
lazily inside the platform back-ends, so importing this package is cheap.
"""
from .images import images
from .templates import templates

# The CLI-composition manifest (same vocabulary as consumer cli-ext modules).
COMMANDS = [images, templates]
ATTACH: dict = {}
