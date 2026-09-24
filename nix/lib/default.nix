{ nixpkgs }:
# Shared helpers for building nixosConfigurations and colmena nodes.
let
  # ── Runtime data accessors ──
  # hosts.json supports two formats:
  #   Old (flat):  { hostname, vmid, ip, internal_ip, mac, tags }
  #   New (rich):  { type, name, properties: { nodeName, vmId, ipv4, tags, ... } }
  # These helpers abstract the format so all consumers just call them.

  _isRich = h: h ? properties;

  # Extract the eth0 routable IP (for Colmena targetHost).
  _ip = h:
    if _isRich h then
      let ipv4 = (h.properties.ipv4 or {}); in
      ipv4.eth0 or ""
    else
      h.ip or "";

  # Extract the eth1 internal service IP (for NixOS networking + DNS).
  _internalIp = h:
    if _isRich h then
      let ipv4 = (h.properties.ipv4 or {}); in
      ipv4.eth1 or ""
    else
      h.internal_ip or "";

  # Extract hostname.
  _hostname = h: name:
    if _isRich h then
      (h.properties.initialization or {}).hostname or h.name or name
    else
      h.hostname or name;

  # Extract tags list.
  _tags = h:
    if _isRich h then
      h.properties.tags or []
    else
      h.tags or [];

  # Extract cluster metadata.
  _cluster = h:
    if _isRich h then h.cluster or "" else h.cluster or "";
  _nodeIndex = h:
    if _isRich h then h.nodeIndex or 0 else h.node_index or 0;
in
{
  # Merge host/cluster definitions with the fleet-runtime manifest.
  #
  # hosts:    { name -> config-attrset | module-function }  — from
  #                                                            fleetEval.hostsRegistry.
  #                                                            Functions accept (config,
  #                                                            lib, pkgs, helpers, ...).
  # clusters: { clusterName -> (h -> config-attrset) }      — optional; was nix/clusters.nix, removed when no clusters were active
  # runtime:  { name -> flat-or-rich-entry }                 — from
  #                                                            fleetEval.hostsJson
  # helpers:  { dnsRecords, publicDnsRecords,               — from fleetEval (DNS-derived).
  #             dnsRecordsByProvider }                        The last is dnsRecords split by
  #                                                            provider instance, for a resolver
  #                                                            that must not answer with
  #                                                            addresses its clients cannot route.
  #                                                            sshGroupsOf is added below
  #                                                            (runtime-driven, hence here).
  #
  # Returns: { name -> { hostname, modules, tags, targetHost? } }
  #
  # Standalone hosts always appear (even without runtime data).
  # Cluster nodes appear only when runtime data includes them.
  mkHosts = { hosts, clusters ? {}, runtime ? {}, helpers ? {} }:
    let
      # sshGroupsOf reads runtime.<name>.ssh_groups (populated by
      # fleet.hostsJson) — wired here because it depends on runtime,
      # which isn't visible to the fleet module.
      fullHelpers = helpers // {
        sshGroupsOf = name: (runtime.${name} or {}).ssh_groups or [ "platform-admins" ];
        sudoGroupsOf = name: (runtime.${name} or {}).sudo_groups or [];
      };
      helpersModule = { ... }: { _module.args.helpers = fullHelpers; };

      standaloneHosts = builtins.mapAttrs (name: hostConfig:
        let
          rt = runtime.${name} or {};
          internalIp = _internalIp rt;
          ip = _ip rt;
          pveType = rt.pve_type or null;
          providerInstance = rt.provider_instance or "";
        in {
          hostname = _hostname rt name;
          modules = [
            helpersModule
            # A registry entry may be a module FUNCTION, a PATH/string to
            # a module file (v2 `nixos = ./cfg/host.nix` facets), or a
            # plain attrset. Paths must pass through untouched — wrapping
            # one as ({...}: path) hands the module system a function
            # returning a path, which it rejects far away from here.
            (if builtins.isFunction hostConfig
                || builtins.isPath hostConfig
                || builtins.isString hostConfig
             then hostConfig
             else ({ ... }: hostConfig))
          ] ++ nixpkgs.lib.optional (internalIp != "") ({ ... }: {
            infra.networking.internalIp = internalIp;
          }) ++ nixpkgs.lib.optional ((internalIp != "") != (ip != "")) ({ ... }: {
            # Single-NIC host (exactly one of ip/internal_ip set).
            # Covers both the internal-only common case and ip-only
            # vmbr0-attached hosts.
            infra.networking.singleInterface = true;
          }) ++ nixpkgs.lib.optional (ip != "" && internalIp == "") ({ ... }: {
            # vmbr0-only single-NIC host: feed the LAN IP into core.nix
            # so the eth0 static-on-vmbr0 branch picks it up. Paired with
            # the singleInterface wire above; both have to be set for
            # the external-only systemd-networkd path to fire.
            infra.networking.externalIp = ip;
          })
          # Auto-wire infra.platform.type from the fleet's kind +
          # provider_instance (substrate-aware: pve.lxc / pve.qemu /
          # xcpng.vm) so host configs don't have to remember to set
          # this manually. Discovered during headscale-router recovery
          # 2026-05-19 — without it, VM hosts defaulted to pve.lxc and
          # skipped the GRUB-install branch, leaving freshly-deployed
          # VMs unable to boot the new closure.
          ++ nixpkgs.lib.optional (pveType != null) ({ ... }: {
            infra.platform.type = pveType;
          })
          # Tell the host which provider instance provisions it, so
          # nix/fleet/sites.nix can resolve its site and hand back
          # site-corrected settings (INFRA-307). The fleet module system
          # has no other way to know: fleet.hostsJson holds the whole
          # manifest, but a host cannot look itself up in it without
          # already knowing its own key — which is exactly what this
          # injection supplies. Same mechanism as internalIp above.
          ++ nixpkgs.lib.optional (providerInstance != "") ({ ... }: {
            fleet.self.providerInstance = providerInstance;
          })
          # The declared size, for modules that scale with the guest they run
          # on (the runner's `count = "auto"`). Same mechanism as above.
          ++ nixpkgs.lib.optional ((rt.cpu_cores or null) != null) ({ ... }: {
            fleet.self.cpuCores = rt.cpu_cores;
          })
          ++ nixpkgs.lib.optional ((rt.memory_mb or null) != null) ({ ... }: {
            fleet.self.memoryMb = rt.memory_mb;
          });
          tags = _tags rt;
        } // (
          if ip != "" then { targetHost = ip; }
          else if internalIp != "" then { targetHost = internalIp; }
          else {}
        )
      ) hosts;

      clusterHosts = builtins.listToAttrs (
        builtins.concatLists (
          builtins.attrValues (
            builtins.mapAttrs (name: h:
              let
                internalIp = _internalIp h;
                ip = _ip h;
                cluster = _cluster h;
              in
              if cluster != "" && (clusters ? ${cluster})
              then [{
                inherit name;
                value = {
                  hostname = _hostname h name;
                  modules = [
                    ({ ... }: clusters.${cluster} h)
                  ] ++ nixpkgs.lib.optional (internalIp != "") ({ ... }: {
                    infra.networking.internalIp = internalIp;
                  });
                  tags = _tags h;
                } // (
                  if ip != "" then { targetHost = ip; }
                  else if internalIp != "" then { targetHost = internalIp; }
                  else {}
                );
              }]
              else []
            ) runtime
          )
        )
      );
    in
    standaloneHosts // clusterHosts;

  # Build nixosConfigurations from the hosts attrset.
  mkNixosConfigurations = { hosts, globalModules ? [], specialArgs ? {} }:
    builtins.mapAttrs (_name: h:
      nixpkgs.lib.nixosSystem {
        inherit specialArgs;
        modules = globalModules ++ h.modules ++ [
          ({ ... }: {
            # Colmena declares `deployment.*`; accept (and discard) it
            # here so host modules may set colmena-only knobs
            # (buildOnTarget, keys, …) without breaking the
            # plain-nixosSystem eval path.
            options.deployment = nixpkgs.lib.mkOption {
              type = nixpkgs.lib.types.attrsOf nixpkgs.lib.types.raw;
              default = {};
              internal = true;
              description = "Colmena deployment options — inert in plain nixosConfigurations.";
            };
            config = {
              nixpkgs.hostPlatform = "x86_64-linux";
              networking.hostName = h.hostname;
              # Colmena injects `nodes` (the whole hive) via specialArgs;
              # this plain-nixosSystem path has no hive, so give modules
              # that consume `nodes` (e.g. nix/modules/infra/data/pgweb) an
              # empty one. Colmena's eval never sees this module, so there
              # is no conflict with the real value.
              _module.args.nodes = {};
            };
          })
        ];
      }
    ) hosts;

  # Generate hostname → internal_ip DNS records from hosts.json runtime data.
  # Used by CoreDNS to auto-populate A records for all hosts.
  # Supports both old (flat) and new (rich) hosts.json formats.
  mkDnsRecords = { runtime }:
    let
      inherit (nixpkgs.lib) filterAttrs mapAttrs;
      withIp = filterAttrs (_: h:
        (_internalIp h) != "" || (_ip h) != ""
      ) runtime;
    in
    mapAttrs (_: h:
      let iip = _internalIp h; in
      if iip != "" then iip else _ip h
    ) withIp;

  # Build colmena node definitions from deployable hosts.
  # sopsAgeKeyFile: path to the age private key file (pushed via deployment.keys
  # so sops-nix can decrypt secrets during activation — no manual provisioning).
  mkColmenaNodes = { hosts, globalModules ? [], sopsAgeKeyCommand ? null }:
    builtins.mapAttrs (_name: h: { name, ... }: {
      imports = globalModules ++ h.modules;
      networking.hostName = name;
      deployment = {
        targetHost = h.targetHost;
        targetUser = "root";
        tags = h.tags or [];
        # Allow first deploy onto a fresh bootstrap-template VM whose
        # active profile is `-unnamed-` (Colmena would otherwise refuse
        # to replace a profile it didn't build). No-op once the host
        # has a real per-host profile, so safe to leave on globally.
        # Without this the `--force-replace-unknown-profiles` CLI flag
        # has no effect — it only flips this option from false to true.
        replaceUnknownProfiles = true;
        keys = nixpkgs.lib.optionalAttrs (sopsAgeKeyCommand != null) {
          "sops-age-key" = {
            keyCommand = sopsAgeKeyCommand;
            destDir = "/var/lib/sops-nix";
            name = "key.txt";
            user = "root";
            group = "root";
            permissions = "0600";
            uploadAt = "pre-activation";
          };
        };
      };
    }) hosts;
}
