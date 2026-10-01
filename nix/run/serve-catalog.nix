# nix/run/serve-catalog.nix — tailnet serveUI catalog (ADR-036).
#
# Walks every host's `infra.network.tailnet.serveUI` and emits the browser-trusted
# tailnet URLs they declare. Pure eval — desired state, not liveness; a URL
# here only means the host is *configured* to serve it, not that it's up.
#
# Commands:
#   nix run .#serve-catalog            — flat list of URLs, one per line (default)
#   nix run .#serve-catalog -- json    — structured JSON (host/name/url/backend)
#   nix run .#serve-catalog -- table   — human-readable table
#
{ pkgs, lib, nixosConfigurations }:
let
  # One flat list of entries across the whole fleet, enabled UIs only.
  entries = lib.concatLists (lib.mapAttrsToList (hostName: hostCfg:
    let
      ts       = hostCfg.config.infra.network.tailnet or {};
      domain   = let d = ts.serveUIDomain or (hostCfg.config.fleet.settings.domain.tailnetSuffix or "");
                 in if d == null then "" else d;
      # The tailnet name, not the registry key: a CTID-keyed or renamed host
      # (and any host with an explicit tailnet.hostname) serves its UIs on
      # the name the module put into its Caddy vhost.
      fqdn     = "${ts.hostname or hostName}.${domain}";
      enabled  = lib.filterAttrs (_: u: u.enable) (ts.serveUI or {});
      # serveUI.backendAddress is loopback (tailscale-serve proxies to a local
      # port *on the host*), which is useless in a fleet-wide catalog — every
      # row would just read 127.0.0.1. Report the host's reachable internal IP
      # instead, falling back to the declared backendAddress when a host has no
      # internalIp set.
      hostIp   = let ip = hostCfg.config.infra.networking.internalIp or ""; in
                 if ip != "" then ip else null;
    in lib.mapAttrsToList (name: u:
      let
        portSuffix = lib.optionalString (u.servePort != 443) ":${toString u.servePort}";
        pathPart   = if u.path == "/" || u.path == "" then "" else u.path;
        backendHost = if hostIp != null then hostIp else u.backendAddress;
      in {
        host = hostName;
        inherit name;
        url = "https://${fqdn}${portSuffix}${pathPart}";
        servePort = u.servePort;
        path = u.path;
        backend = "${backendHost}:${toString u.backendPort}";
      }
    ) enabled
  ) nixosConfigurations);

  # Stable ordering: by URL.
  sorted = builtins.sort (a: b: a.url < b.url) entries;

  entriesJson = builtins.toJSON sorted;
  urlList     = lib.concatStringsSep "\n" (map (e: e.url) sorted);

  script = pkgs.writeShellScriptBin "serve-catalog" ''
    set -euo pipefail

    ENTRIES='${entriesJson}'
    URLS='${urlList}'
    MODE="''${1:-urls}"

    case "$MODE" in
      urls)
        printf '%s\n' "$URLS"
        ;;
      json)
        printf '%s' "$ENTRIES" | ${pkgs.jq}/bin/jq '.'
        ;;
      table)
        {
          printf '%s\t%s\t%s\n' "HOST" "URL" "BACKEND"
          printf '%s\t%s\t%s\n' "----" "---" "-------"
          printf '%s' "$ENTRIES" | ${pkgs.jq}/bin/jq -r '
            .[] | [.host, .url, .backend] | @tsv
          '
        } | ${pkgs.util-linux}/bin/column -t -s $'\t'
        ;;
      *)
        echo "Usage: serve-catalog [urls|json|table]" >&2
        exit 1
        ;;
    esac
  '';
in
{
  serve-catalog = script;
}
