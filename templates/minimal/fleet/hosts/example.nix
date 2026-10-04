{ ... }:

# One file per host: the provisioning entry (fleet.compute.<name>) and,
# for NixOS guests, the OS config (fleet.hostsRegistry.<name>). Editing
# a host is a single-file change.
#
# Opt-in add-ons are enabled per host in its hostsRegistry config, e.g. with
# mkFleet { addons.llm-agents = inputs.llm-agents; } in your flake:
#   infra.addons.llmAgents = {
#     enable = true;
#     packages = [ "opencode" "codegraph" ];
#     # unfree.enable = true;   # needed for unfree tools such as claude-code
#   };

{
  config.fleet.compute.example = {
    env = "platform"; stack = "core";
    provider_instance = "proxmox.main";
    kind = "container";
    vm_id = 101;
    node = "pve1";
    tags = [ "example" ];
    ip = ""; internal_ip = "192.0.2.101";
    cpu_cores = 2; memory_mb = 1024; swap_mb = 512;
    root_disk_datastore = "local-lvm";
    network_mode = "single-internal";
    features = { nesting = true; fuse = false; keyctl = false; };
    notes = "Example NixOS LXC — replace me.";
    # Any NIC layout instead of the fixed single-internal shape:
    # network_mode = "declared";
    # interfaces = [
    #   { bridge = "vmbr1"; ipv4 = "192.0.2.101/24"; gateway = "192.0.2.1"; }
    #   { bridge = "vmbr0"; ipv4 = "dhcp"; vlan = 42; ipv6.method = "auto"; }
    # ];
    # Device passthrough (GPU / TUN / Coral) and PVE lifecycle flags:
    # devices = [ { path = "/dev/dri/renderD128"; gid = 44; } { path = "/dev/net/tun"; } ];
    # protection = true; startup = { order = 10; };
  };

  config.fleet.hostsRegistry.example = { helpers, ... }: {
    infra.networking.singleInterface = true;

    # Fleet services are enabled per host under the strata namespace,
    # infra.<stratum>.<module> — e.g.:
    #   infra.data.postgresql.enable = true;
    #   infra.ingress.enable = true;
    #   infra.observability.stack.enable = true;
    # See the "Infra modules" chapter of the docs for the full catalog.
  };
}
