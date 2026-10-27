#!/usr/bin/env bash
# NixOS-module add-ons (addons/nix, nixosModules.addons). Evaluation only. Checks that:
#   - importing nixosModules.addons with nothing switched on changes nothing
#     (tailscale, headscale, the cert unit and timer, warnings, assertions);
#   - fleetkit.addons.tailscale.enable sets services.tailscale (enable, package);
#   - cert.enable renders the tailscale-cert service and timer, the group, the
#     read-only certFile/keyFile paths; no domain is refused by an assertion;
#   - headscale.certificates merges dns.certificates and dns.challenge.provider
#     into services.headscale.settings without replacing the host's keys, and an
#     older Headscale gets a warning.
# Prints one JSON line: {"nix-addons":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! nix eval --impure --json --expr "
  let
    kit = builtins.getFlake (toString $ROOT);
    inherit (kit.inputs) nixpkgs;
    inherit (nixpkgs) lib;
    sys = extra: (nixpkgs.lib.nixosSystem {
      modules = [
        kit.nixosModules.addons
        { nixpkgs.hostPlatform = \"x86_64-linux\"; system.stateVersion = \"25.05\"; boot.loader.grub.enable = false; fileSystems.\"/\" = { device = \"/dev/vda\"; fsType = \"ext4\"; }; }
      ] ++ extra;
    }).config;
    plain = sys [ ];
    ts = sys [ { fleetkit.addons.tailscale.enable = true; } ];
    cert = sys [ { fleetkit.addons.tailscale = {
      enable = true;
      cert = { enable = true; domain = \"dumpy.tamarin-gray.ts.net\"; reloadUnits = [ \"caddy.service\" ]; };
    }; } ];
    nodomain = sys [ { fleetkit.addons.tailscale = { enable = true; cert.enable = true; }; } ];
    hs = sys [ {
      services.headscale = { enable = true; package = nixpkgs.legacyPackages.x86_64-linux.headscale // { version = \"0.40.0\"; }; settings.dns.base_domain = \"ts.example.com\"; };
      fleetkit.addons.tailscale.headscale.certificates = {
        enable = true;
        zone = \"example.com\";
        challenge.provider = { name = \"cloudflare\"; apiTokenFile = \"/run/credentials/headscale.service/cloudflare-token\"; };
      };
    } ];
    hsOld = sys [ {
      services.headscale = { enable = true; package = nixpkgs.legacyPackages.x86_64-linux.headscale // { version = \"0.20.0\"; }; };
      fleetkit.addons.tailscale.headscale.certificates = {
        enable = true; zone = \"example.com\";
        challenge.provider = { name = \"cloudflare\"; apiTokenFile = \"/x\"; };
      };
    } ];
    svc = cert.systemd.services.tailscale-cert;
  in {
    offTailscale = plain.services.tailscale.enable;
    offHeadscale = plain.services.headscale.enable;
    offCert = plain.systemd.services ? tailscale-cert;
    offTimer = plain.systemd.timers ? tailscale-cert;
    offGroup = plain.users.groups ? tailscale-cert;
    offWarnings = builtins.length (builtins.filter (w: lib.hasInfix \"fleetkit.addons\" w) plain.warnings);
    tsEnable = ts.services.tailscale.enable;
    tsPkg = ts.services.tailscale.package == nixpkgs.legacyPackages.x86_64-linux.tailscale;
    tsNoCert = !(ts.systemd.services ? tailscale-cert);
    certPaths = [ cert.fleetkit.addons.tailscale.cert.certFile cert.fleetkit.addons.tailscale.cert.keyFile ];
    certGroup = cert.users.groups ? tailscale-cert;
    certType = svc.serviceConfig.Type;
    certAfter = svc.after;
    certScript = svc.script;
    certTimer = { inherit (cert.systemd.timers.tailscale-cert.timerConfig) OnCalendar Persistent; wantedBy = cert.systemd.timers.tailscale-cert.wantedBy; };
    noDomainFails = map (a: a.message) (builtins.filter (a: !a.assertion) nodomain.assertions);
    hsDns = { inherit (hs.services.headscale.settings.dns) certificates challenge base_domain; };
    hsWarn = builtins.length (builtins.filter (w: lib.hasInfix \"older than\" w) hs.warnings);
    hsOldWarn = builtins.length (builtins.filter (w: lib.hasInfix \"older than\" w) hsOld.warnings);
  }
" >"$TMP/r.json" 2>"$TMP/r.err"; then
  tail -n 40 "$TMP/r.err" >&2
  echo '{"nix-addons":"fail"}'
  exit 1
fi

python3 - "$TMP/r.json" <<'PY' >&2
import json, sys
r = json.load(open(sys.argv[1]))
bad = []
def want(name, cond):
    if not cond:
        bad.append(name)
want("off: nothing on", not r["offTailscale"] and not r["offHeadscale"] and not r["offCert"]
     and not r["offTimer"] and not r["offGroup"] and r["offWarnings"] == 0)
want("tailscale enable and package", r["tsEnable"] and r["tsPkg"] and r["tsNoCert"])
want("cert paths", r["certPaths"] == [
    "/var/lib/tailscale-cert/dumpy.tamarin-gray.ts.net.crt",
    "/var/lib/tailscale-cert/dumpy.tamarin-gray.ts.net.key"])
want("cert group", r["certGroup"])
s = r["certScript"]
want("cert service", r["certType"] == "oneshot" and "tailscaled.service" in r["certAfter"]
     and "tailscale cert" in s and "dumpy.tamarin-gray.ts.net" in s
     and "chmod 0640" in s and "try-reload-or-restart caddy.service" in s)
want("cert timer", r["certTimer"] == {"OnCalendar": "daily", "Persistent": True, "wantedBy": ["timers.target"]})
want("no domain refused", len(r["noDomainFails"]) == 1 and "cert.domain" in r["noDomainFails"][0])
want("headscale settings", r["hsDns"] == {
    "base_domain": "ts.example.com",
    "certificates": {"enabled": True, "zone": "example.com"},
    "challenge": {"provider": {"name": "cloudflare",
        "api_token_path": "/run/credentials/headscale.service/cloudflare-token"}}})
want("no warning on current headscale", r["hsWarn"] == 0)
want("warning on old headscale", r["hsOldWarn"] == 1)
for b in bad:
    print("FAIL", b, file=sys.stderr)
sys.exit(1 if bad else 0)
PY
if [ $? -eq 0 ]; then echo '{"nix-addons":"pass"}'; else echo '{"nix-addons":"fail"}'; exit 1; fi
