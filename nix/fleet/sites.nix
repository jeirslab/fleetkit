{ config, lib, ... }:

# Per-site settings (INFRA-307).
#
# `fleet.settings` and `fleet.network` are ESTATE-WIDE: one value, every
# host. That is correct while a fleet is one site, and wrong the moment a
# second site exists that cannot route to the first. Every setting naming
# an address then becomes a site-1 value that every site-2 host has to
# override by hand, in its own host file, forever.
#
# The failure mode is not cosmetic. A site-2 host inheriting site 1's
# `fleet.settings.cache.substituters` spends a connect timeout on an
# unroutable substituter before every nix operation falls through to the
# public cache; a site-2 host inheriting site 1's `fleet.network.gateway`
# gets a default route it cannot install, which holds systemd-networkd at
# SETUP `configuring` until `systemd-networkd-wait-online` times out and
# the whole colmena activation is reported as failed.
#
# So: declare the sites, say which provider instances belong to each, and
# give the site the handful of values that differ. A host resolves its own
# site from the provider instance that provisions it and reads the resolved
# value through `fleet.self.settings.*`.
#
# ── Reading a call site ───────────────────────────────────────────────
#   config.fleet.settings.X        estate-wide, same on every host
#   config.fleet.self.settings.X   site-resolved for THIS host
# The distinction is deliberately visible: a module that reads the former
# is asserting the value is an estate fact, and that assertion should be
# reviewable without chasing definitions.
#
# ── What this does NOT cover yet ──────────────────────────────────────
# Nix-side module eval only. Three other consumers still read the
# estate-wide values and are unchanged by this module:
#
#   * the terranix render path (nix/lib/tf/proxmox.nix passes
#     cache.substituters into the bootstrap image builder; nix/lib/pve-iso.nix
#     puts gateway + resolvers into cloud-init),
#   * `nix/modules/infra/provisioning/pve-installer-answers` (installer DNS),
#   * the `fleet` CLI, which reads a FLAT `fleet-catalog.json`
#     (`pve build-template` via pve.py, `fleet pki` via pki_group.py).
#
# Keying the catalog by site is the second half of INFRA-307 and is
# deliberately not in this pass: it changes the CLI's data contract, and
# those operations are single-site today. Until it lands, a per-site value
# set here is honoured by deployed NixOS config and ignored by create-time
# provisioning — which is the safe direction of the two, but is a real
# seam and not a subtlety to discover later.
#
# ── Backwards compatibility ───────────────────────────────────────────
# Every per-site field is `nullOr` defaulting to null, meaning "inherit the
# estate-wide value". A consumer that declares no `fleet.sites` resolves
# every field to exactly what it resolved to before, so an existing fleet
# renders byte-identically. That is what makes this safe to land while an
# incumbent site is frozen.

let
  inherit (lib) mkOption types;

  cfg = config.fleet;

  claims = lib.concatLists
    (lib.mapAttrsToList (_: site: site.providerInstances) cfg.sites);

  # A provider instance in two sites makes the lookup below silently
  # order-dependent, so refuse it rather than resolve it. `throwIf` rather
  # than an assertion: this module is evaluated by the MANIFEST eval too
  # (flake.nix builds fleetEval from ./nix/fleet alone), and that eval has
  # no NixOS `assertions` option to collect into — same reason the fleet
  # validator in ./default.nix throws.
  claimCounts = lib.foldl'
    (acc: pi: acc // { ${pi} = (acc.${pi} or 0) + 1; })
    { }
    claims;
  doubleClaimed = lib.attrNames (lib.filterAttrs (_: n: n > 1) claimCounts);

  # provider instance → site label. Built from each site's own
  # providerInstances list, so the consumer declares the mapping once, in
  # the direction they think about it ("this site runs these hypervisors").
  siteOfProvider = lib.throwIf (doubleClaimed != [ ])
    ("fleet.sites: provider instance(s) claimed by more than one site: "
     + lib.concatStringsSep ", " doubleClaimed
     + ". Each provider_instance must belong to at most one site.")
    (lib.listToAttrs (lib.concatLists (lib.mapAttrsToList
      (label: site: map (pi: lib.nameValuePair pi label) site.providerInstances)
      cfg.sites)));

  selfSite =
    if cfg.self.providerInstance == "" then null
    else siteOfProvider.${cfg.self.providerInstance} or null;

  # The resolved site's overrides, or an all-null site when this host
  # belongs to no declared site (which is every host in a fleet that has
  # not adopted `fleet.sites`).
  s = if selfSite == null then null else cfg.sites.${selfSite};

  # `or` on a null-valued attr returns the null, not the fallback, so the
  # inheritance test has to be explicit.
  pick = f: fallback:
    if s == null then fallback
    else let v = f s; in if v == null then fallback else v;

  siteSubmodule = types.submodule {
    options = {
      providerInstances = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "proxmox.dc2" ];
        description = ''
          `fleet.compute.*.provider_instance` values provisioned at this
          site, in "<provider>.<instance>" form. A host resolves its site by
          looking itself up here. A provider instance may belong to at most
          one site (asserted); listing several is how a site that spans a
          hypervisor plus an appliance layer is described.
        '';
      };

      network = {
        gateway = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "192.0.2.1";
          description = "Per-site override of `fleet.network.gateway` — the default route for single-NIC hosts on the internal bridge. null ⇒ inherit the estate-wide value.";
        };
        internalResolvers = mkOption {
          type = types.nullOr (types.listOf types.str);
          default = null;
          example = [ "192.0.2.1" ];
          description = "Per-site override of `fleet.network.internal_resolvers` — the fleet DNS servers pinned to this site's links. Must be fleet DNS only, never a public resolver (INFRA-107). null ⇒ inherit the estate-wide value.";
        };
        upstreamResolvers = mkOption {
          type = types.nullOr (types.listOf types.str);
          default = null;
          example = [ "192.0.2.254" "1.1.1.1" ];
          description = "Per-site override of `fleet.settings.network.upstreamResolvers` — where this site's fleet DNS forwards non-fleet queries. Usually the site's own LAN gateway. null ⇒ inherit the estate-wide value.";
        };
      };

      cache = {
        substituters = mkOption {
          type = types.nullOr (types.listOf types.str);
          default = null;
          example = [ "http://192.0.2.25:5000" ];
          description = "Per-site override of `fleet.settings.cache.substituters` — the in-fleet binary caches this site's hosts can actually reach. Replaces the estate-wide list rather than extending it, because the point is usually to drop a cache that does not route. null ⇒ inherit the estate-wide value.";
        };
        trustedPublicKeys = mkOption {
          type = types.nullOr (types.listOf types.str);
          default = null;
          example = [ "cache.example.dev:MExampleExampleExampleExampleExampleExampleExa=" ];
          description = "Per-site override of `fleet.settings.cache.trustedPublicKeys`, matching this site's `substituters`. null ⇒ inherit the estate-wide value.";
        };
      };

      internalCa = {
        acmeDirectory = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "https://ca.dc2.example.lan:9000/acme/acme/directory";
          description = "Per-site override of `fleet.settings.internalCa.acmeDirectory` — this site's own step-ca. A second site generally needs its own internal CA, because the estate-wide directory is named by an internal zone the second site's resolver does not serve. null ⇒ inherit the estate-wide value.";
        };
        certFile = mkOption {
          type = types.nullOr types.path;
          default = null;
          example = lib.literalExpression "./certs/dc2-root-ca.crt";
          description = ''
            Per-site override of `fleet.settings.internalCa.certFile` — the
            root certificate this site's hosts trust. null ⇒ inherit the
            estate-wide value.

            This travels with `acmeDirectory` and is the half that is easy to
            forget. Each step-ca mints its own root at first start, so two
            sites running their own CA have two unrelated roots. Overriding
            only the directory gives a host that orders successfully from its
            local CA and then cannot verify the result, because the root in
            its trust store belongs to the other site's CA.

            It is also needed WITHOUT an `acmeDirectory` override in the
            split-horizon case, where both sites share one directory URL and
            each site's resolver answers it locally: the name is estate-wide,
            the root behind it is not.
          '';
        };
      };
    };
  };
in
{
  options.fleet.sites = mkOption {
    type = types.attrsOf siteSubmodule;
    default = { };
    example = lib.literalExpression ''
      {
        dc2 = {
          providerInstances = [ "proxmox.dc2" ];
          network.gateway = "192.0.2.1";
          network.internalResolvers = [ "192.0.2.1" ];
          network.upstreamResolvers = [ "192.0.2.254" "1.1.1.1" ];
          cache.substituters = [ "http://192.0.2.25:5000" ];
          cache.trustedPublicKeys = [ "cache.example.dev:MExampleExampleExampleExampleExampleExampleExa=" ];
        };
      }
    '';
    description = ''
      Physically separate sites, each overriding the estate-wide settings
      that name an address. Keyed by an operator-chosen label; the label
      itself has no meaning to the framework beyond identifying the site in
      `fleet.self.site`.

      Declaring no sites (the default) leaves every host resolving the
      estate-wide values exactly as before. A host whose provider instance
      appears in no site's `providerInstances` likewise inherits every
      estate-wide value — that is not an error, it is how the incumbent
      site keeps working while a second one is described. Declare the
      incumbent with only a `providerInstances` list (no overrides) when you
      want the estate stated explicitly; it changes nothing it renders.

      Site membership is by provider instance rather than per host, because
      a site IS the substrate: every guest on a hypervisor shares that
      hypervisor's gateway, resolvers and reachable caches, and a per-host
      mapping would be the same fact restated once per guest.
    '';
  };

  options.fleet.self = {
    providerInstance = mkOption {
      type = types.str;
      default = "";
      internal = true;
      description = ''
        The `provider_instance` of the host currently being evaluated,
        injected per host by `mkHosts` from `fleet.hostsJson`. Empty in the
        manifest eval (which has no single host) and on hosts with no
        provisioned fleet entry, both of which resolve to no site.
      '';
    };

    site = mkOption {
      type = types.nullOr types.str;
      default = null;
      internal = true;
      description = "Label of the `fleet.sites` entry this host belongs to, resolved from its provider instance. null ⇒ the host is in no declared site and inherits every estate-wide value.";
    };

    settings = {
      network = {
        gateway = mkOption {
          type = types.nullOr types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.network.gateway` for this host. Read this instead of the estate-wide option in any module that runs on hosts at more than one site.";
        };
        internalResolvers = mkOption {
          type = types.listOf types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.network.internal_resolvers` for this host.";
        };
        upstreamResolvers = mkOption {
          type = types.listOf types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.settings.network.upstreamResolvers` for this host.";
        };
      };
      cache = {
        substituters = mkOption {
          type = types.listOf types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.settings.cache.substituters` for this host.";
        };
        trustedPublicKeys = mkOption {
          type = types.listOf types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.settings.cache.trustedPublicKeys` for this host.";
        };
      };
      internalCa = {
        acmeDirectory = mkOption {
          type = types.nullOr types.str;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.settings.internalCa.acmeDirectory` for this host.";
        };
        certFile = mkOption {
          type = types.nullOr types.path;
          internal = true;
          readOnly = true;
          description = "Site-resolved `fleet.settings.internalCa.certFile` for this host.";
        };
      };
    };
  };

  config = {
    fleet.self.site = selfSite;

    fleet.self.settings = {
      network = {
        gateway = pick (x: x.network.gateway) cfg.network.gateway;
        internalResolvers = pick (x: x.network.internalResolvers) cfg.network.internal_resolvers;
        upstreamResolvers = pick (x: x.network.upstreamResolvers) cfg.settings.network.upstreamResolvers;
      };
      cache = {
        substituters = pick (x: x.cache.substituters) cfg.settings.cache.substituters;
        trustedPublicKeys = pick (x: x.cache.trustedPublicKeys) cfg.settings.cache.trustedPublicKeys;
      };
      internalCa = {
        acmeDirectory =
          pick (x: x.internalCa.acmeDirectory) cfg.settings.internalCa.acmeDirectory;
        certFile =
          pick (x: x.internalCa.certFile) cfg.settings.internalCa.certFile;
      };
    };
  };
}
