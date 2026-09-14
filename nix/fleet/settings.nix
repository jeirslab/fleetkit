{ lib, ... }:

# fleetkit parameter surface (ADR-092 / INFRA-218).
#
# Every environment-specific value the FRAMEWORK itself needs lives
# here, declared front-and-center with no company defaults. The
# consumer repo sets these in one place (conventionally
# `fleet/settings.nix` next to its manifest); framework modules read
# `config.fleet.settings.*` instead of literals.
#
# Requiredness policy: fleetkit users COMPOSE — some run only one
# provider, no tailnet, no observability stack. Therefore no option
# here is unconditionally required unless the always-on base layer
# consumes it (currently only `adminSshKeys`). Everything else is
# `nullOr` with `default = null` (or a generic default) and is
# enforced by an assertion inside the consuming module, so a
# minimum-viable fleet (one host, one provider, no optional services)
# evaluates with only the settings that fleet actually exercises.
#
# Scope note (ADR-097): this is the ONLY declaration surface — the
# `fleet` CLI reads the same values from `.cache/fleet/catalog.json`,
# a generated projection of this eval (the `fleet-catalog` package),
# regenerated automatically. fleet.toml is gone; nothing is declared
# twice. Operator-machine paths (age key, sysadmin key) are NOT
# settings — they are conventions overridable via FLEET_* env vars.

{
  options.fleet.settings = {
    name = lib.mkOption {
      type = lib.types.str;
      default = "fleet";
      example = "acme";
      description = "Short fleet/org slug. Used for branding and resource-name prefixes (attic cache name, hydra project, step-ca CA name, pgweb bookmarks).";
    };

    domain = {
      base = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "example.dev";
        description = "Public base domain (external DNS zone). null ⇒ no public-name features; required (asserted) by modules that mint public names: caddy devDomain vhosts, coredns split-horizon zone, acme-dns, hydra/grafana mail senders, pve-installer-answers.";
      };
      internal = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "example.pve";
        description = "Internal search/zone domain served by fleet DNS. null ⇒ no internal-FQDN features; required (asserted) by caddy, coredns, host-cert (internal CA), step-ca, hydra, rabbitmq management vhosts.";
      };
      tailnetSuffix = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "hs.example.dev";
        description = "MagicDNS base domain of the fleet tailnet (headscale base_domain). null ⇒ no tailnet serveUI names; required (asserted) when infra.network.tailnet.serveUI entries exist.";
      };
    };

    acmeEmail = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "admin@example.com";
      description = "Email for ACME account registration (internal CA and public Let's Encrypt). null ⇒ no ACME issuance; required (asserted) by infra.ingress and by host-cert when an internal CA is configured.";
    };

    adminSshKeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      example = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIREPLACEMEexamplekeyexamplekeyexample operator@example.com" ];
      description = "SSH public keys authorized for the built-in operator accounts (sysadmin / colmena / dev / root) on every fleet host. REQUIRED BY THE BASE LAYER — every NixOS fleet host creates these accounts, so building any host toplevel forces this option.";
    };

    tailnet = {
      controlUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://vpn.example.dev";
        description = "Login/control server URL of the fleet tailnet (headscale). Used as --login-server by infra.network.tailnet.fleetNode. null ⇒ fleetNode emits no --login-server flag.";
      };
      preauthKeyUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://vpn.example.dev/internal/preauth/fleet-bot";
        description = "HTTPS endpoint returning a tailnet preauth key as raw text (e.g. a source-IP-gated headscale vhost). Used by infra.network.tailnet.fleetNode to auto-fetch enrollment keys. null ⇒ hosts fall back to a SOPS-held auth key.";
      };
    };

    auth = {
      outpostUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://192.0.2.13:9000";
        description = "Base URL of the identity provider's forward-auth outpost (e.g. the Authentik embedded outpost). null ⇒ no forward_auth injection by default.";
      };
      oidcBaseUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://auth.example.dev";
        description = "Base URL of the fleet's OIDC identity provider (e.g. Authentik). Required by modules that enable OIDC login (e.g. infra.observability.stack.oidc).";
      };
    };

    observability = {
      grafanaDomain = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "grafana.example.pve";
        description = "Domain Grafana serves on (server.domain / root_url). null ⇒ no observability stack; required (asserted) when infra.observability.stack is enabled.";
      };
      prometheusRemoteWriteUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://192.0.2.4:9090/api/v1/write";
        description = "Prometheus remote-write endpoint every fleet host's Alloy agent ships metrics to (usually the grafana-stack host). null (together with lokiPushUrl = null) ⇒ Alloy stays disabled by default fleet-wide; required (asserted) when infra.observability.alloy is enabled.";
      };
      lokiPushUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://192.0.2.4:3100/loki/api/v1/push";
        description = "Loki push endpoint every fleet host's Alloy agent ships logs to (usually the grafana-stack host). null (together with prometheusRemoteWriteUrl = null) ⇒ Alloy stays disabled by default fleet-wide; required (asserted) when infra.observability.alloy is enabled.";
      };
      tempoUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://192.0.2.9:3200";
        description = "HTTP URL of the fleet's Tempo trace store. null ⇒ no Tempo datasource is provisioned in Grafana.";
      };
      lokiS3Endpoint = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "http://s3.example.lan:3900";
        description = "S3-compatible endpoint (e.g. in-fleet Garage) Loki writes chunks and index to. null ⇒ no Loki chunk store; required (asserted) when infra.observability.stack is enabled.";
      };
      pveScrapeTargets = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        example = { pve1 = "198.51.100.1"; pve2 = "198.51.100.2"; };
        description = "Proxmox VE hypervisors scraped via prometheus-pve-exporter: instance label → node API address. {} ⇒ no PVE targets.";
      };
      cpuAlertExcludeRegex = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "chain-node-.*|miner-.*";
        description = "Prometheus instance-label regex excluded from the fleet-wide high-CPU alert (hosts that legitimately run hot). \"\" ⇒ no exclusions.";
      };
    };

    network = {
      wanIp = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "203.0.113.10";
        description = "Public WAN IP of the fleet edge (stable pointer for public DNS pins). null ⇒ no public-edge features; required (asserted) by infra.pki.acmeDns (glue/apex A records).";
      };
      lanCidr = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "192.0.2.0/24";
        description = "Fleet LAN CIDR (mirrors fleet.network.internal_cidr for module convenience). null ⇒ modules that default network ACLs from it (e.g. infra.data.postgresql.allowedSubnets) default to an empty list instead.";
      };
      mgmtCidr = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "198.51.100.0/24";
        description = "Hypervisor/management network CIDR, if separate from the LAN.";
      };
      upstreamResolvers = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "1.1.1.1" "9.9.9.9" ];
        example = [ "198.51.100.1" "1.1.1.1" ];
        description = "Upstream DNS servers the fleet DNS forwards non-fleet queries to (e.g. the LAN gateway or public resolvers).";
      };
      staticWanCidrs = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        example = { headscale-router = "198.51.100.7/24"; };
        description = ''
          VM name → WAN-side CIDR for legacy name-dispatch VMs whose
          fleet entry keeps `ip = ""` (so Colmena resolves internal_ip)
          but still needs a pinned WAN address on eth0. Consumed by the
          `headscale-router` branch of nix/lib/tf/proxmox.nix mkVm.
          {} ⇒ no pins; the emitter throws if a VM hits that branch
          without an entry here.
        '';
      };
    };

    providers = {
      proxmox = {
        defaultDatastore = lib.mkOption {
          type = lib.types.str;
          default = "local-storage";
          example = "local-lvm";
          description = ''
            PVE storage used wherever a compute entry does not name one:
            VM root/EFI/data disks, the cloud-init drive, clone targets,
            and the default of fleet.compute.<name>.root_disk_datastore.
            LEGACY DEFAULT "local-storage" is kept so existing fleets
            render unchanged; set it explicitly (PVE's stock thin pool is
            "local-lvm") — the default flips to "local-lvm" in the next
            major release.
          '';
        };
        lxcTemplateDatastore = lib.mkOption {
          type = lib.types.str;
          default = "nix-store";
          example = "local";
          description = ''
            PVE storage (content type vztmpl) that holds the NixOS LXC
            template nixos-bootstrap-lxc-latest.tar.xz every NixOS container
            is created from. LEGACY DEFAULT "nix-store" (a cluster-wide NFS
            SR registered by the ansible proxmox/pve nfs-storage task); a
            single-node fleet uploads the template to "local" instead via a
            `kind = "file"` resource with source = "nixos-lxc-image".
          '';
        };
        hostTweaks = lib.mkOption {
          default = {};
          description = "Hypervisor-side conveniences the community `tools/pve/*.sh` scripts used to apply by hand, now driven by ansible (roles proxmox/base and proxmox/pve) from these values. `fleet ansible inventory` exports them as the `fleet_pve_host_tweaks` variable.";
          type = lib.types.submodule {
            options = {
              microcode = lib.mkOption { type = lib.types.bool; default = false; description = "Install the CPU microcode package for the node's vendor (intel-microcode / amd64-microcode; enables non-free-firmware). Legacy microcode.sh."; };
              kernelClean = lib.mkOption { type = lib.types.bool; default = false; description = "Purge old PVE kernels on each ansible run, keeping the running one and the newest. Legacy kernel-clean.sh."; };
              kernelPin = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; example = "6.14.8-2-pve"; description = "Pin the node to this kernel version with proxmox-boot-tool (null = unpinned). Legacy kernel-pin.sh."; };
              scalingGovernor = lib.mkOption { type = lib.types.nullOr (lib.types.enum [ "performance" "powersave" "ondemand" "conservative" "schedutil" ]); default = null; description = "CPU frequency scaling governor applied at boot (null = leave the kernel default). Legacy scaling-governor.sh."; };
              nicOffloadingFix = lib.mkOption { type = lib.types.bool; default = false; description = "Disable NIC offloading (ethtool) on Intel e1000/e1000e adapters at boot to work around hangs. Legacy nic-offloading-fix.sh."; };
              diskHealth = lib.mkOption { type = lib.types.bool; default = false; description = "Install smartmontools + nvme-cli and run a weekly SMART short self-test on every disk. Legacy disk-health.sh."; };
              ipTag = lib.mkOption { type = lib.types.bool; default = false; description = "Run the IP-Tag service that keeps a `<ip>` tag on every guest in the PVE UI. Legacy add-iptag.sh. PVE nodes only."; };
              monitorAll = lib.mkOption { type = lib.types.bool; default = false; description = "Run the ping-instances service that restarts guests that stop answering. Legacy monitor-all.sh. PVE nodes only."; };
            };
          };
        };
        singleBridgeInstances = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          example = [ "colo" ];
          description = ''
            Proxmox provider-instance names (the `<inst>` in a fleet
            entry's `provider_instance = "proxmox.<inst>"`) whose PVE
            nodes carry the internal LAN directly on vmbr0 (single-NIC
            nodes, e.g. PVE-on-XCP-ng VMs). Containers on these
            instances default their internal bridge to vmbr0 instead
            of vmbr1; a per-host `internal_bridge` override still wins.
          '';
        };
      };
    };

    githubAccessTokens = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Provision a GitHub machine-user token (SOPS integrations/github/machine_user_token) into nix access-tokens on every host — needed when flake inputs fetch private GitHub repos.";
    };

    internalCa = {
      certFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = lib.literalExpression "./certs/fleet-root-ca.crt";
        description = "Root certificate of the fleet-internal CA (step-ca). Trusted on every host and used as the Caddy ACME root when set.";
      };
      acmeDirectory = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://ca.example.lan:9000/acme/acme/directory";
        description = "ACME directory URL of the internal CA. null ⇒ modules default to public Let's Encrypt.";
      };
    };

    # ── CLI-facing settings (ADR-097) ────────────────────────────
    # These existed only in fleet.toml before; the fleet-catalog
    # projection now carries them to the launcher. Snake_case keys in
    # freeform sets are deliberate — the catalog preserves them
    # verbatim, and the CLI's dotted lookup paths predate this module.

    opsEmail = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "ops@example.dev";
      description = "Operations contact. null ⇒ the CLI derives ops@<domain.base>.";
    };

    backend = {
      type = lib.mkOption {
        type = lib.types.enum [ "s3" "local" "pg" ];
        default = "s3";
        description = "Tofu state backend kind. \"local\" keeps terraform.tfstate inside each stack's working dir (.tf/<slug>/) — no bucket, no cloud creds; fine for a homelab, but the state only exists on the machine that ran the apply. \"s3\" (default) is the shared-bucket estate model (ADR-097). \"pg\" is Postgres, the on-prem option that provides REAL locking via advisory locks — S3-compatible stores that cannot do conditional writes (Garage, by design) leave `use_lockfile` silently ineffective, which is unsafe wherever more than one operator or agent applies.";
      };
      bucket = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "acme-tofu";
        description = "Tofu S3 state bucket (type = s3). Shared estate substrate (ADR-097) — fleet separation is the state KEY prefix, not the bucket. When set, mkFleet's `backend` argument may be omitted.";
      };
      region = lib.mkOption {
        type = lib.types.str;
        default = "us-east-1";
        description = "AWS region of the state bucket (type = s3).";
      };
      s3 = {
        credsSopsPath = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = ''["integrations"]["tofu"]["garage"]'';
          description = ''
            SOPS path to the S3 state-backend credentials — a mapping with
            `access_key_id`, `secret_access_key`, and optionally `region` —
            exported as AWS_* at run time. Defaults to `["integrations"]["aws"]`.

            This is a setting rather than a constant because the AWS name is a
            site opinion, not a fact about the backend: a fleet whose state
            lives in Garage or MinIO files those keys under its own tree. When
            the CLI guessed, the extract simply returned non-zero and the
            credentials silently stayed unset — surfacing much later as tofu
            reporting no valid credential sources, with nothing pointing at the
            real cause.
          '';
        };
      };

      pg = {
        schemaPrefix = lib.mkOption {
          type = lib.types.str;
          default = "tf_";
          description = ''
            Schema-name prefix for the `pg` backend. Each stack gets its own
            schema (`<prefix><slug>`), which is how stacks stay isolated in one
            database — the equivalent of the S3 key prefix.
          '';
        };
        connStrSopsPath = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = ''["dbs"]["tofu-db"]["tofu"]["conn_str"]'';
          description = ''
            SOPS path to the libpq connection string, exported as PG_CONN_STR
            at run time.

            The connection string is DELIBERATELY not emitted into the backend
            block: config.tf.json is built by Nix and therefore lands in
            /nix/store, which is world-readable. A password baked in there is
            readable by every user on every machine that builds the stack, and
            no amount of file permissions afterwards takes it back. OpenTofu
            reads PG_CONN_STR from the environment for exactly this reason.
          '';
        };
      };

      perStack = lib.mkOption {
        type = lib.types.attrsOf (lib.types.attrsOf lib.types.raw);
        default = { };
        example = lib.literalExpression ''{ "platform-mcp" = { type = "local"; }; }'';
        description = ''
          Per-stack backend overrides, keyed by stack SLUG (the dot-path with
          "." replaced by "-", e.g. "platform.core" -> "platform-core"). Each
          value is merged over the fleet-wide backend, so an override may set
          only what differs (usually just `type`).

          The backend block is already emitted per stack, so this costs nothing
          structurally. Two uses it exists for:

            * Keep working when the shared bucket is unreachable — provision a
              NEW stack on `type = "local"` while every existing stack stays
              pointed at the remote it already lives in.
            * Break a bootstrap cycle — a stack that provisions the fleet's own
              object storage should not keep its state inside that storage.

          DELIBERATE ACT, NOT A FALLBACK. Pointing an EXISTING stack at an empty
          backend makes tofu read its entire inventory as "not created yet", and
          an apply from there would recreate the fleet. Override a stack that has
          no remote state yet, or migrate the state first and record that you did.
        '';
      };
    };

    cli.extensionsDir = lib.mkOption {
      type = lib.types.str;
      default = "cli-ext";
      description = "Repo-relative directory of consumer CLI extension modules (ADR-095 COMMANDS/ATTACH files).";
    };

    sopsFiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = lib.literalExpression ''{ integrations = "nix/secrets/integrations.yaml"; }'';
      description = ''
        Which SOPS file owns which TOP-LEVEL key tree, for fleets that split
        their store by resource group. Keys are tree names ("integrations",
        "services", "dbs", …); values are repo-relative paths. Anything not
        listed falls back to `sopsSecretsFile`.

        Splitting a store and leaving the original populated is the trap this
        exists to close: a consumer aimed at the old file ERRORS when the key
        is gone, but returns a diverged old value when a stale duplicate
        survives — and that case never fails. Declaring routes once means a
        consumer cannot hold a private, wrong opinion about where a tree lives.
      '';
    };

    tfSopsFile = lib.mkOption {
      type = lib.types.str;
      default = "nix/secrets/secrets.yaml";
      example = "nix/secrets/integrations.yaml";
      description = ''
        Repo-relative SOPS file the TERRANIX layer reads at `tofu apply` time
        (the `data.sops_file.secrets` source). These are provider credentials —
        `integrations.*` — which need not live in the same file NixOS hosts
        default to. A fleet that splits its SOPS store per resource group must
        point this at whichever file holds the integrations tree, or every
        `tofu plan` fails with "The given key does not identify an element in
        this collection value".
      '';
    };

    sopsSecretsFile = lib.mkOption {
      type = lib.types.str;
      default = "nix/secrets/secrets.yaml";
      description = "Repo-relative path of the default sops file the CLI's secrets commands operate on.";
    };

    pki.acmeDnsApiBase = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://192.0.2.100:8081";
      description = "acme-dns registration API on the fleet's DNS edge. Consumed by `fleet pki` (required, asserted there).";
    };

    pveInstall = lib.mkOption {
      type = lib.types.attrsOf lib.types.raw;
      default = { };
      example = { serve_host = "192.0.2.91"; iso_sr_name = "NFS ISO Library"; };
      description = "Unattended-PVE-install constants for `fleet pve install` (serve_host, iso_sr_uuid, iso_sr_name, main_sr_name, network_name, installer_iso, presets). Freeform: substrate constants whose long-term home is the typed provider nodes (ADR-096); keys pass to the catalog verbatim.";
    };

    mcp.grafanaTokenSopsPath = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "services/grafana/mcp_token";
      description = "Sops key path of the read-only Grafana service-account token used by `fleet mcp config`.";
    };

    cache = {
      substituters = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        example = [ "http://192.0.2.101:5000" ];
        description = "In-fleet nix binary caches trusted by fleet hosts (harmonia/attic/...).";
      };
      trustedPublicKeys = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [];
        example = [ "cache.example.dev:MExampleExampleExampleExampleExampleExampleExa=" ];
        description = "Public keys matching `substituters`.";
      };
    };

    build.machines = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          hostName = lib.mkOption {
            type = lib.types.str;
            example = "192.0.2.101";
            description = "Address the offloading host connects to over SSH. An in-fleet IP, not a public name.";
          };
          sshUser = lib.mkOption {
            type = lib.types.str;
            default = "root";
            description = "User to connect as. Must appear in the builder's `infra.build.builder.trustedUsers`, or its daemon refuses the store operations an offloaded build needs.";
          };
          systems = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ "x86_64-linux" ];
            description = "Platforms this machine will build for.";
          };
          maxJobs = lib.mkOption {
            type = lib.types.int;
            default = 4;
            description = "Jobs the offloading host may run on this machine concurrently.";
          };
          speedFactor = lib.mkOption {
            type = lib.types.int;
            default = 1;
            description = "Relative speed weight. Only meaningful when more than one machine can serve the same system.";
          };
          supportedFeatures = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ "nixos-test" "benchmark" "big-parallel" ];
            example = [ "nixos-test" "benchmark" "big-parallel" "kvm" ];
            description = "Features this machine advertises. A derivation requiring a feature absent here is never sent to it. Do not advertise kvm on an unprivileged LXC builder.";
          };
          publicHostKey = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "c3NoLWVkMjU1MTkgQUFBQUV4YW1wbGVFeGFtcGxlRXhhbXBsZQ==";
            description = "Base64 of the machine's SSH host key line, produced by `base64 -w0 < /etc/ssh/ssh_host_ed25519_key.pub`. Leaving this null makes the offload depend on the client's known_hosts, which nothing in the fleet populates — the first build then hangs on host-key verification rather than failing.";
          };
        };
      });
      default = [];
      description = "Remote build machines available to the fleet. Listing one here does NOT make any host use it: a host opts in via `infra.build.remote.enable`, because offloading means shipping that host an SSH private key.";
    };
  };
}
