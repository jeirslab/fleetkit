#!/usr/bin/env python3
"""Render fleetkit's options.json into a structured mdBook chapter tree.

Replaces the single-page optionsCommonMark dump: one page per option
group (fleet/settings, fleet/network, …, infra/<module>), a generated
SUMMARY.md, and index pages with per-group option counts.

Usage: generate.py <options.json> <src-dir>
"""
from __future__ import annotations

import html
import json
import sys
from collections import defaultdict
from pathlib import Path

# fleet.* second components rendered as manifest chapters, in this order.
FLEET_ORDER = ["settings", "network", "compute", "providers", "resources"]

# infra.* second components (strata) rendered as module chapters, in this
# order: strata first (mirroring nix/modules/infra/<stratum>/), then the
# always-on base groups.
INFRA_ORDER = ["network", "ingress", "pki", "observability", "data", "build",
               "mail", "auth", "provisioning", "integrations", "addons",
               "services", "networking", "platform"]

# Strata whose pages nest one section per module (infra.<stratum>.<module>).
# The rest (infra.ingress, the base groups) are a single flat option tree.
NESTED_STRATA = {"network", "pki", "observability", "data", "build",
                 "mail", "auth", "provisioning", "integrations", "addons"}


def md_escape_anchor(name: str) -> str:
    return name.replace("<", "_").replace(">", "_").replace(".", "").replace("*", "_").lower()


def render_value(v) -> str:
    """Render a default/example object from options.json as markdown."""
    if v is None:
        return ""
    if isinstance(v, dict) and "_type" in v:
        text = v.get("text", "")
        return f"```nix\n{text}\n```"
    return f"```nix\n{json.dumps(v, indent=2)}\n```"


def render_option(name: str, o: dict) -> str:
    parts = [f"### `{name}`\n"]
    desc = (o.get("description") or "").strip()
    if desc:
        parts.append(desc + "\n")
    parts.append(f"**Type:** `{o.get('type', '?')}`")
    if o.get("readOnly"):
        parts.append("**Read-only:** computed by fleetkit; not settable.")
    default = o.get("default")
    if default is not None:
        parts.append("**Default:**\n" + render_value(default))
    else:
        parts.append("**Default:** none (required when its feature is enabled)")
    example = o.get("example")
    if example is not None:
        parts.append("**Example:**\n" + render_value(example))
    decls = o.get("declarations") or []
    links = []
    for d in decls:
        if isinstance(d, dict):
            links.append(f"[{d.get('name', 'source')}]({d.get('url', '')})")
        else:
            links.append(f"`{d}`")
    if links:
        parts.append("**Declared by:** " + ", ".join(links))
    return "\n\n".join(parts) + "\n\n---\n"


def group_of(name: str) -> tuple[str, str] | None:
    """Map an option name to (chapter_dir, group). None = skip."""
    parts = name.split(".")
    if parts[0] == "fleet" and len(parts) > 1:
        head = parts[1]
        if head in FLEET_ORDER:
            return ("fleet", head)
        return ("fleet", "other")
    if parts[0] == "infra" and len(parts) > 1:
        return ("infra", parts[1])
    # _module.args and foreign leftovers
    return None


def main(options_json: str, src_dir: str) -> None:
    opts = json.loads(Path(options_json).read_text())
    src = Path(src_dir)

    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for name, o in sorted(opts.items()):
        g = group_of(name)
        if g is None:
            continue
        groups[g][name] = o

    # Emit one page per group. Infra strata pages get one section per
    # module (infra.<stratum>.<module>) so the strata stay navigable.
    for (chapter, group), items in groups.items():
        d = src / chapter
        d.mkdir(parents=True, exist_ok=True)
        page = [f"# {chapter}.{group}\n"]
        page.append(f"*{len(items)} options*\n")
        if chapter == "infra" and group in NESTED_STRATA:
            current_module = None
            for name, o in items.items():
                module = name.split(".")[2]
                if module != current_module:
                    page.append(f"## `infra.{group}.{module}`\n")
                    current_module = module
                page.append(render_option(name, o))
        else:
            for name, o in items.items():
                page.append(render_option(name, o))
        (d / f"{group}.md").write_text("\n".join(page))

    # Index pages.
    fleet_groups = [g for (c, g) in groups if c == "fleet"]
    fleet_sorted = [g for g in FLEET_ORDER if g in fleet_groups] + sorted(
        g for g in fleet_groups if g not in FLEET_ORDER)
    infra_groups = [g for (c, g) in groups if c == "infra"]
    infra_sorted = [g for g in INFRA_ORDER if g in infra_groups] + sorted(
        g for g in infra_groups if g not in INFRA_ORDER)

    def index_page(chapter: str, title: str, blurb: str, names: list[str]) -> None:
        lines = [f"# {title}\n", blurb, ""]
        for g in names:
            n = len(groups[(chapter, g)])
            lines.append(f"- [`{chapter}.{g}`](./{g}.md) — {n} options")
        (src / chapter / "index.md").write_text("\n".join(lines) + "\n")

    index_page("fleet", "Fleet manifest",
               "The declarative fleet description: what your fleet *is*. "
               "Consumers set these in the modules passed to `mkFleet`.",
               fleet_sorted)
    index_page("infra", "Infra modules",
               "NixOS service modules every fleet host can enable, grouped "
               "into strata (`infra.<stratum>.<module>`). Each module reads "
               "its site values from `fleet.settings.*`.",
               infra_sorted)

    # SUMMARY.md drives mdBook's sidebar — fully generated.
    summary = [
        "# Summary\n",
        "- [Introduction](./introduction.md)",
        "- [Compute surface and networking modes](./compute-surface.md)",
        "- [Sibling estates and the manifest (proposal)](./sibling-manifest.md)",
        "- [Fleet manifest](./fleet/index.md)",
    ]
    summary += [f"  - [fleet.{g}](./fleet/{g}.md)" for g in fleet_sorted]
    summary.append("- [Infra modules](./infra/index.md)")
    summary += [f"  - [infra.{g}](./infra/{g}.md)" for g in infra_sorted]
    (src / "SUMMARY.md").write_text("\n".join(summary) + "\n")

    total = sum(len(v) for v in groups.values())
    print(f"rendered {total} options into {len(groups)} pages")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
