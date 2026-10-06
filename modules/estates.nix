# fleet.estates: the owning organisations (homelab, xgcs) and their core
# settings: owner, domains, tailnets, environments, backend, pools,
# placement. Secrets are in ./secrets.nix, the loose blocks (git, cache,
# build, substrate, ...) in ./loose.nix; all three extend the same estate
# submodule, and none of them has a freeform type.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  ids = config.fleet.report.ids;

  providerKinds = [ "proxmox" ];

  range =
    what:
    types.submodule {
      options = {
        from = mkOption {
          type = types.int;
          description = "First ${what} of the range (inclusive).";
        };
        to = mkOption {
          type = types.int;
          description = "Last ${what} of the range (inclusive).";
        };
      };
    };

  tailnetType =
    estate:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify estate name);
          kind = mkOption {
            type = types.enum [
              "tailscale"
              "headscale"
            ];
          };
          suffix = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "MagicDNS suffix (tailscale only).";
          };
          apiKeyRef = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "sops ref of the API key (tailscale only).";
          };
          controlUrl = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "Control server URL (headscale only).";
          };
          server = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "Guest id running the control server (headscale only).";
          };
        };
      }
    );

  environmentType =
    estate:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify estate name);
          branch = mkOption { type = types.str; };
          backend = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "Backend id; null means the estate backend.";
          };
        };
      }
    );

  # pools.<providerKind>.<name>, id "<estate>/<providerKind>/<name>".
  poolType =
    parent:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify parent name);
          provider = mkOption {
            type = types.str;
            description = "Provider id.";
          };
          access = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Grant ids that may use the pool.";
          };
          vmid = mkOption {
            type = types.listOf (range "vmid");
            default = [ ];
          };
          networks = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options.hosts = mkOption {
                  type = types.listOf (range "host octet");
                  default = [ ];
                };
              }
            );
            default = { };
            description = "Per network NAME of the provider's site, the host ranges reserved.";
          };
        };
      }
    );

  poolKindType =
    estate:
    types.submodule (
      { name, ... }:
      {
        freeformType = types.lazyAttrsOf (poolType (h.qualify estate name));
      }
    );

  estateType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        owner = mkOption {
          type = types.str;
          description = "Owning organisation (display name).";
        };
        domains = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Zone ids owned by this estate.";
        };
        backend = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Backend id of the estate.";
        };
        environments = mkOption {
          type = types.lazyAttrsOf (environmentType name);
          default = { };
        };
        tailnets = mkOption {
          type = types.lazyAttrsOf (tailnetType name);
          default = { };
        };
        placement = mkOption {
          type = types.nullOr (
            types.submodule {
              options = {
                provider = mkOption {
                  type = types.str;
                  description = "Provider id the estate's guests are placed on.";
                };
                pool = mkOption {
                  type = types.str;
                  description = "Pool id of that provider.";
                };
                tokenRef = mkOption {
                  type = types.nullOr types.str;
                  default = null;
                  description = "sops ref (in this estate's own secrets) of the API credential this estate uses on the provider; null = the provider's tokenRef, which must then be in this estate's own secrets (the estate that owns the cluster).";
                };
              };
            }
          );
          default = null;
        };
        pools = mkOption {
          type = types.lazyAttrsOf (poolKindType name);
          default = { };
        };
      };
    }
  );

  # Last "/"-segment and everything before it.
  lastSeg = s: lib.last (lib.splitString "/" s);
  firstSeg = s: lib.head (lib.splitString "/" s);

  poolsOf = e: lib.concatLists (lib.mapAttrsToList (k: ps: lib.mapAttrsToList (p: v: { inherit k p v; }) ps) e.pools);
  allPools = lib.concatMap poolsOf (lib.attrValues config.fleet.estates);
  poolById = lib.listToAttrs (map (x: lib.nameValuePair x.v.id x.v) allPools);

  rangeOk = lo: hi: r: r.from <= r.to && r.from >= lo && r.to <= hi;

  estateAssertions =
    ename: e:
    let
      at = path: "fleet.estates.${ename}.${path}";
      zoneOwner =
        z:
        let
          zone = config.fleet.zones.${z} or null;
        in
        {
          assertion = zone == null || zone.owner == e.id;
          message = "${at "domains"}: zone \"${z}\" is owned by \"${if zone == null then "?" else zone.owner}\", not by estate \"${e.id}\"";
        };
      tailnetAsserts = lib.concatLists (
        lib.mapAttrsToList (
          tn: t:
          let
            w = at "tailnets.${tn}";
            need = field: {
              assertion = t.${field} != null;
              message = "${w}: ${t.kind} tailnet requires ${field}";
            };
            forbid = field: {
              assertion = t.${field} == null;
              message = "${w}: ${t.kind} tailnet must not set ${field}";
            };
          in
          if t.kind == "tailscale" then
            [
              (need "suffix")
              (need "apiKeyRef")
              (forbid "controlUrl")
              (forbid "server")
            ]
            ++ lib.optionals (t.apiKeyRef != null) (
              h.secretRefAssertions {
                where = "${w}.apiKeyRef";
                ids = ids.secretRef;
                estate = ename;
              } t.apiKeyRef
            )
          else
            [
              (need "controlUrl")
              (need "server")
              (forbid "suffix")
              (forbid "apiKeyRef")
            ]
            ++ lib.optional (t.server != null) (
              h.refAssertion {
                where = "${w}.server";
                kind = "guest";
                ids = ids.guest;
              } t.server
            )
        ) e.tailnets
      );
      envAsserts = lib.concatLists (
        lib.mapAttrsToList (
          en: env: h.optionalRefAssertions { where = at "environments.${en}.backend"; kind = "backend"; ids = ids.backend; } env.backend
        ) e.environments
      );
      placementAsserts = lib.optionals (e.placement != null) [
        (h.refAssertion { where = at "placement.provider"; kind = "provider"; ids = ids.provider; } e.placement.provider)
        (h.refAssertion { where = at "placement.pool"; kind = "pool"; ids = ids.pool; } e.placement.pool)
        {
          assertion = !(builtins.elem e.placement.pool ids.pool) || poolById.${e.placement.pool}.provider == e.placement.provider;
          message = "${at "placement"}: pool \"${e.placement.pool}\" belongs to provider \"${(poolById.${e.placement.pool} or { provider = "?"; }).provider}\", not \"${e.placement.provider}\"";
        }
      ]
      ++ lib.optionals (e.placement != null && e.placement.tokenRef != null) (
        h.secretRefAssertions {
          where = at "placement.tokenRef";
          ids = ids.secretRef;
          estate = ename;
        } e.placement.tokenRef
      );
      poolAsserts = lib.concatMap (
        x:
        let
          w = at "pools.${x.k}.${x.p}";
          pr = x.v.provider;
          site = firstSeg pr;
        in
        [
          {
            assertion = builtins.elem x.k providerKinds;
            message = "${w}: pool kind \"${x.k}\" is not a provider kind (${lib.concatStringsSep ", " providerKinds})";
          }
          (h.refAssertion { where = "${w}.provider"; kind = "provider"; ids = ids.provider; } pr)
          {
            assertion = lastSeg pr == x.k;
            message = "${w}: provider \"${pr}\" is not of kind \"${x.k}\"";
          }
        ]
        ++ h.refAssertions { where = "${w}.access"; kind = "grant"; ids = ids.grant; } x.v.access
        ++ map (r: {
          assertion = rangeOk 0 2147483647 r;
          message = "${w}.vmid: bad range ${toString r.from}..${toString r.to} (need from <= to)";
        }) x.v.vmid
        ++ lib.concatMap (
          nn:
          [
            (h.refAssertion { where = "${w}.networks.${nn}"; kind = "network"; ids = ids.network; } (h.qualify site nn))
          ]
          ++ map (r: {
            assertion = rangeOk 1 254 r;
            message = "${w}.networks.${nn}.hosts: bad range ${toString r.from}..${toString r.to} (need 1 <= from <= to <= 254)";
          }) x.v.networks.${nn}.hosts
        ) (lib.attrNames x.v.networks)
      ) (poolsOf e);
    in
    h.optionalRefAssertions { where = at "backend"; kind = "backend"; ids = ids.backend; } e.backend
    ++ map zoneOwner e.domains
    ++ h.refAssertions { where = at "domains"; kind = "zone"; ids = ids.zone; } e.domains
    ++ tailnetAsserts
    ++ envAsserts
    ++ placementAsserts
    ++ poolAsserts;
in
{
  options.fleet.estates = mkOption {
    type = types.attrsOf estateType;
    default = { };
  };

  config.assertions = lib.concatLists (lib.mapAttrsToList estateAssertions config.fleet.estates);
}
