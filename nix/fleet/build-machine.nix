{ lib }:

# One remote build machine (the `nix.buildMachines` shape). Shared by
# fleet.settings.build.machines (estate-wide) and fleet.sites.<site>.build.
# machines (per-site override, INFRA-307 style): a site with its own builder
# offloads to it, never across the WAN to another site's.
lib.types.submodule {
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
      }
