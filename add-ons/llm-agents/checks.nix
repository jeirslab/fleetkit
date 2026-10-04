# Acceptance gate for the llm-agents add-on.
#
# A module no host enables is never evaluated by `nix flake check` (that is
# how the Proton Bridge module shipped broken twice), so this INSTANTIATES the
# add-on: a small fleet whose hosts exercise it, built through mkFleet with a
# stub input standing in for numtide/llm-agents.nix. It proves the policy, not
# just that the module evaluates:
#   * selected packages land in the host's systemPackages; unselected don't
#   * an unfree package is refused unless `unfree.enable`, and accepted with it
#   * an unknown package name, an MCP server without a command, and enabling
#     the add-on without handing mkFleet the input are all hard eval errors
#     (tryEval + deepSeq: nothing short of forcing the toplevel trips them)
#   * a disabled add-on installs nothing
#   * the rendered MCP configs are exactly what a client would read
{ nixpkgs, mkFleet }:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  inherit (pkgs) lib;

  # The stub's packages come from a nixpkgs that allows unfree, like the real
  # upstream flake's own package set. That is also why the add-on needs its own
  # `unfree.enable` policy: these derivations never meet the HOST's
  # allowUnfreePredicate, so nothing else would ever stop an unfree install.
  upstreamPkgs = import nixpkgs { system = "x86_64-linux"; config.allowUnfree = true; };
  tool = name: upstreamPkgs.writeShellScriptBin name "echo ${name}";

  # Stand-in for the upstream flake: only `packages.<system>` is read.
  stubInput.packages.x86_64-linux = {
    fake-agent = tool "fake-agent";
    fake-mcp = tool "fake-mcp";
    fake-unfree = (tool "fake-unfree").overrideAttrs (o: {
      meta = (o.meta or { }) // { license = lib.licenses.unfree; mainProgram = "fake-unfree"; };
    });
  };

  compute = id: ip: {
    env = "platform"; stack = "core";
    provider_instance = "proxmox.main";
    kind = "container";
    vm_id = id;
    node = "pve1";
    tags = [ "addon-check" ];
    ip = ""; internal_ip = ip;
    cpu_cores = 1; memory_mb = 512; swap_mb = 0;
    root_disk_datastore = "local-lvm";
    network_mode = "single-internal";
    notes = "check-suite llm-agents add-on host";
  };

  hosts = {
    # name = [ vm_id ip module ]
    addon-ok = [ 9301 "192.0.2.131" {
      infra.addons.llmAgents = {
        enable = true;
        packages = [ "fake-agent" "fake-mcp" ];
        mcp.writeFiles = true;
        mcp.servers.fake = { package = "fake-mcp"; args = [ "serve" ]; };
      };
    } ];
    addon-unfree-ok = [ 9302 "192.0.2.132" {
      infra.addons.llmAgents = { enable = true; packages = [ "fake-unfree" ]; unfree.enable = true; };
    } ];
    addon-unfree-denied = [ 9303 "192.0.2.133" {
      infra.addons.llmAgents = { enable = true; packages = [ "fake-unfree" ]; };
    } ];
    addon-unknown = [ 9304 "192.0.2.134" {
      infra.addons.llmAgents = { enable = true; packages = [ "no-such-tool" ]; };
    } ];
    addon-mcp-unresolved = [ 9305 "192.0.2.135" {
      # package exists upstream but is not in `packages`, and no command given
      infra.addons.llmAgents = { enable = true; packages = [ "fake-agent" ]; mcp.servers.x.package = "fake-mcp"; };
    } ];
    addon-off = [ 9306 "192.0.2.136" {
      infra.addons.llmAgents = { enable = false; packages = [ "fake-agent" ]; };
    } ];
  };

  fixture = {
    config.fleet.compute = lib.mapAttrs (_: h: compute (builtins.elemAt h 0) (builtins.elemAt h 1)) hosts;
    config.fleet.hostsRegistry = lib.mapAttrs (_: h: { ... }: builtins.elemAt h 2) hosts;
  };

  fleet = mkFleet {
    modules = [ ../../templates/minimal/fleet fixture ];
    addons.llm-agents = stubInput;
  };

  # Same hosts, but mkFleet is NOT given the input.
  fleetNoInput = mkFleet { modules = [ ../../templates/minimal/fleet fixture ]; };

  cfgOf = f: n: f.nixosConfigurations.${n}.config;
  forced = f: n: builtins.unsafeDiscardOutputDependency (cfgOf f n).system.build.toplevel.drvPath;
  fails = f: n: !(builtins.tryEval (builtins.deepSeq (cfgOf f n).system.build.toplevel.drvPath true)).success;
  hasPkg = n: name: lib.any (p: (p.name or "") == name) (cfgOf fleet n).environment.systemPackages;

  ok = cfgOf fleet "addon-ok";
  summary = builtins.toJSON {
    installsSelected = hasPkg "addon-ok" "fake-agent" && hasPkg "addon-ok" "fake-mcp";
    skipsUnselected = !(hasPkg "addon-ok" "fake-unfree");
    unfreeAcceptedWhenEnabled = hasPkg "addon-unfree-ok" "fake-unfree";
    unfreeRefused = fails fleet "addon-unfree-denied";
    unknownRefused = fails fleet "addon-unknown";
    unresolvedMcpRefused = fails fleet "addon-mcp-unresolved";
    missingInputRefused = fails fleetNoInput "addon-ok";
    offInstallsNothing = !(hasPkg "addon-off" "fake-agent");
    renderedHasServer = ok.infra.addons.llmAgents.mcp.rendered.claudeCode.mcpServers ? fake;
  };
in
{
  addon-llm-agents = pkgs.runCommand "fleetkit-addon-llm-agents-check"
    {
      nativeBuildInputs = [ pkgs.jq ];
      # strict env attrs: instantiating this derivation forces every host
      okToplevelDrv = forced fleet "addon-ok";
      unfreeToplevelDrv = forced fleet "addon-unfree-ok";
      offToplevelDrv = forced fleet "addon-off";
      inherit summary;
      claudeJson = ok.environment.etc."fleetkit/mcp/claude-code.json".text;
      opencodeJson = ok.environment.etc."fleetkit/mcp/opencode.json".text;
      passAsFile = [ "summary" "claudeJson" "opencodeJson" ];
    } ''
    jq -e 'to_entries | all(.value == true)' "$summaryPath" >/dev/null \
      || { echo "llm-agents add-on policy check failed:"; jq . "$summaryPath"; exit 1; }

    # The rendered files are what a client reads: stdio command resolved to
    # the package's main program, args carried over.
    jq -e '.mcpServers.fake | (.type == "stdio") and (.command | endswith("/bin/fake-mcp")) and (.args == ["serve"])' "$claudeJsonPath" >/dev/null
    jq -e '.mcp.fake | (.type == "local") and (.command[0] | endswith("/bin/fake-mcp")) and (.command[1] == "serve") and .enabled' "$opencodeJsonPath" >/dev/null
    touch $out
  '';
}
