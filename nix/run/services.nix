# nix/run/services.nix — service catalog utilities.
#
# Commands:
#   nix run .#service-catalog          — print fleet-wide service catalog as JSON
#   nix run .#service-catalog -- table — print as a human-readable table
#   nix run .#service-catalog -- md    — print as grouped markdown (for docs/wiki)
#   nix run .#service-catalog -- doc   — write wiki/src/reference/service-catalog.md
#
# Filtering:
#   nix run .#service-catalog -- --server mempool             — show only mempool
#   nix run .#service-catalog -- --server mempool,grafana     — show mempool + grafana
#   nix run .#service-catalog -- table --server mempool       — table view, filtered
#
{ pkgs, lib, nixosConfigurations }:
let
  # Internal domain from the fleet's settings (identical on every host).
  #
  # Nullable, and legitimately so: a fleet with no internal DNS reaches its
  # services by IP over the tailnet and leaves this unset. Every use below
  # has to survive that -- see `external`.
  domain =
    if nixosConfigurations == { } then "internal"
    else (lib.head (lib.attrValues nixosConfigurations)).config.fleet.settings.domain.internal;

  # Collect infra.services from every NixOS host that defines them.
  allServices = lib.mapAttrs (hostName: hostCfg:
    let
      svcs = hostCfg.config.infra.services or {};
      hostIp = hostCfg.config.infra.networking.internalIp or "";
    in lib.mapAttrs (svcName: svc:
      let
        caddyHostname =
          if svc.caddy.hostname != null
          then "${svc.caddy.hostname}.${domain}"
          else "${hostName}.${domain}";
        path = if svc.caddy.path == "/" then "" else svc.caddy.path;
      in {
        inherit (svc) description category tags;
        host = hostName;
        ip = hostIp;
        urls = {
          internal = "http://${hostIp}:${toString svc.port}${path}";
          # Null when there is no name to reach the service by. Without an
          # internal domain a non-Caddy service has no external URL -- the
          # honest answer is "none", and `internal` above already carries the
          # IP form. Saying so rather than crashing matters because this is
          # lazy: a fleet with no domain.internal evaluated fine right up
          # until the first service that did not sit behind Caddy, which made
          # a settings gap look like a bug in whatever host introduced it.
          external =
            if domain == null then null
            else if svc.caddy.enable then "https://${caddyHostname}${path}"
            else "http://${hostName}.${domain}:${toString svc.port}${path}";
        };
        ports =
          [{ port = svc.port; ui = svc.caddy.enable; protocol = "http"; }]
          ++ map (ep: { inherit (ep) port protocol ui; }) svc.extraPorts;
        caddy = svc.caddy.enable;
      }
    ) svcs
  ) nixosConfigurations;

  # Filter out hosts with no services.
  withServices = lib.filterAttrs (_: svcs: svcs != {}) allServices;

  # Collect SOPS secret key paths from every host.
  allSecrets = lib.mapAttrs (_: hostCfg:
    builtins.sort builtins.lessThan
      (builtins.attrNames (hostCfg.config.sops.secrets or {}))
  ) nixosConfigurations;

  withSecrets = lib.filterAttrs (_: keys: keys != []) allSecrets;

  servicesJson = builtins.toJSON withServices;
  secretsJson = builtins.toJSON withSecrets;

  script = pkgs.writeShellScriptBin "service-catalog" ''
    set -euo pipefail

    CATALOG='${servicesJson}'
    SECRETS='${secretsJson}'
    MODE=""
    SERVER_FILTER=""
    SCRATCH="$(mktemp)"
    trap 'rm -f "$SCRATCH"' EXIT

    # Parse args: positional mode + optional --server flag
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --server)
          SERVER_FILTER="$2"
          shift 2
          ;;
        --server=*)
          SERVER_FILTER="''${1#--server=}"
          shift
          ;;
        *)
          MODE="$1"
          shift
          ;;
      esac
    done
    MODE="''${MODE:-json}"

    # Apply server filter if specified (comma-separated list of service or host names)
    if [[ -n "$SERVER_FILTER" ]]; then
      # Build jq filter: keep entries where service name or host name matches any filter term
      IFS=',' read -ra FILTERS <<< "$SERVER_FILTER"
      JQ_COND=""
      for f in "''${FILTERS[@]}"; do
        f="$(echo "$f" | xargs)"  # trim whitespace
        if [[ -n "$JQ_COND" ]]; then
          JQ_COND="$JQ_COND or"
        fi
        JQ_COND="$JQ_COND (.key == \"$f\" or \$host == \"$f\")"
      done
      CATALOG=$(echo "$CATALOG" | ${pkgs.jq}/bin/jq --arg dummy "" "
        to_entries | map(
          .key as \$host |
          .value |= (to_entries | map(select($JQ_COND)) | from_entries)
        ) | map(select(.value | length > 0)) | from_entries
      ")
      # Filter secrets to matching hosts
      SECRETS=$(${pkgs.jq}/bin/jq -n \
        --argjson secrets "$SECRETS" \
        --argjson hosts "$(echo "$CATALOG" | ${pkgs.jq}/bin/jq '[keys[]]')" \
        '$secrets | with_entries(select(.key as $k | $hosts | index($k)))')
    fi

    case "$MODE" in
      json)
        ${pkgs.jq}/bin/jq -n \
          --argjson services "$CATALOG" \
          --argjson secrets "$SECRETS" \
          '{services: $services, secrets: $secrets}'
        ;;
      table)
        echo "$CATALOG" | ${pkgs.jq}/bin/jq -r '
          ["HOST", "IP", "SERVICE", "EXTERNAL URL", "INTERNAL URL", "PORTS", "CATEGORY", "DESCRIPTION"],
          ["----", "--", "-------", "------------", "------------", "-----", "--------", "-----------"],
          (to_entries[] | .key as $host | .value | to_entries[] |
            [
              $host,
              .value.ip,
              .key,
              .value.urls.external,
              .value.urls.internal,
              ([.value.ports[] | "\(.port)\(if .ui then "*" else "" end)"] | join(",")),
              .value.category,
              .value.description
            ]
          ) | @tsv
        ' | ${pkgs.util-linux}/bin/column -t -s $'\t'
        echo ""
        echo "  * = UI port (behind Caddy reverse proxy)"
        echo ""
        echo "HOST SECRETS"
        echo "============"
        echo "$SECRETS" | ${pkgs.jq}/bin/jq -r '
          to_entries | sort_by(.key)[] |
          "\n\(.key) (\(.value | length) keys):",
          (.value[] | "  \(.)")
        '
        ;;
      md|doc)
        {
          echo "# Fleet Service Catalog"
          echo ""
          echo "> Auto-generated — do not edit manually. Lives in the handbook at"
          echo "> \`wiki/src/reference/service-catalog.md\`."
          echo "> Regenerate: \`nix run .#service-catalog -- doc\` (or \`nix run .#wiki-refresh\`)"
          echo ""

          # Group by category and emit sections
          echo "$CATALOG" | ${pkgs.jq}/bin/jq -r '
            # Flatten into a list of services
            [to_entries[] | .key as $host | .value | to_entries[] |
              .value + {svcName: .key, host: $host}
            ]
            # Group by category
            | group_by(.category)
            | map({
                category: .[0].category,
                services: (sort_by(.host + .svcName))
              })
            | sort_by(.category)
            | .[] |
            "## \(.category | ascii_upcase)\n",
            "| Service | Host | URL | Description |",
            "|---------|------|-----|-------------|",
            (.services[] |
              "| **\(.svcName)** | \(.host) (\(.ip)) | \(if .caddy and .urls.external != null then "[\(.urls.external)](\(.urls.external))" else "\(.urls.internal)" end) | \(.description) |"
            ),
            ""
          '

          echo "---"
          echo ""
          echo "### Host Map"
          echo ""
          echo "| Host | IP | Services |"
          echo "|------|----|----------|"
          echo "$CATALOG" | ${pkgs.jq}/bin/jq -r '
            to_entries | sort_by(.key)[] |
            "| **\(.key)** | \(.value | to_entries[0].value.ip) | \([.value | keys[]] | join(", ")) |"
          '

          echo ""
          echo "---"
          echo ""
          echo "## Secrets by Host"
          echo ""
          echo "> SOPS secret key paths per host (keys only — values are encrypted)."
          echo ""
          echo "$SECRETS" | ${pkgs.jq}/bin/jq -r '
            to_entries | sort_by(.key)[] |
            "### \(.key)\n",
            (.value[] | "- `\(.)`"),
            ""
          '
        } > "$SCRATCH"

        if [ "$MODE" = "doc" ]; then
          # Find repo root
          ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo ".")"
          mkdir -p "$ROOT/wiki/src/reference"
          cp "$SCRATCH" "$ROOT/wiki/src/reference/service-catalog.md"
          echo "Wrote wiki/src/reference/service-catalog.md"
        else
          cat "$SCRATCH"
        fi
        ;;
      *)
        echo "Usage: service-catalog [json|table|md|doc] [--server name1,name2,...]" >&2
        exit 1
        ;;
    esac
  '';
in
{
  service-catalog = script;
}
