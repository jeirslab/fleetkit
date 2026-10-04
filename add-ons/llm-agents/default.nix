# infra.addons.llmAgents — install packages from numtide/llm-agents.nix per
# host, and declare the MCP servers some of them provide.
#
# Inert unless `infra.addons.llmAgents.enable`. Packages come from the flake
# input the consumer passed as mkFleet's `addons.llm-agents` (this module never
# fetches anything itself), straight from its `packages.<system>` set. Those
# derivations are already evaluated by the upstream flake, so they do not pass
# through the host's nixpkgs allowUnfreePredicate: the policy for unfree tools
# is this add-on's own `unfree.enable`, applied below.
#
# Options live in ./options.nix.
{ config, lib, pkgs, ... }:
let
  inherit (lib) mkIf;
  cfg = config.infra.addons.llmAgents;

  input = config.infra.addons.inputs.llm-agents or null;
  system = pkgs.stdenv.hostPlatform.system;
  upstream = if input == null then { } else (input.packages.${system} or { });

  wanted = lib.unique cfg.packages;
  known = lib.filter (n: upstream ? ${n}) wanted;
  unknown = lib.filter (n: !(upstream ? ${n})) wanted;

  # Unfree = any licence entry with free = false. A package with no licence
  # metadata is treated as free (nothing says otherwise).
  isUnfree = p: lib.any (l: !(l.free or true)) (lib.toList (p.meta.license or [ ]));
  unfreePicked = lib.filter (n: isUnfree upstream.${n}) known;
  installable = if cfg.unfree.enable then known else lib.filter (n: !(isUnfree upstream.${n})) known;

  # ── MCP server declarations ──
  servers = cfg.mcp.servers;
  resolvable = s: s.command != null || (s.package != null && builtins.elem s.package known);
  commandOf = s: if s.command != null then s.command else lib.getExe upstream.${s.package};
  usable = lib.filterAttrs (_: resolvable) servers;
  unresolved = lib.attrNames (lib.filterAttrs (_: s: !(resolvable s)) servers);

  rendered = {
    # .mcp.json shape (Claude Code project/user scope).
    claudeCode.mcpServers = lib.mapAttrs
      (_: s: { type = "stdio"; command = commandOf s; inherit (s) args; env = s.environment; })
      usable;
    # opencode.json `mcp` key.
    opencode.mcp = lib.mapAttrs
      (_: s: { type = "local"; command = [ (commandOf s) ] ++ s.args; environment = s.environment; enabled = true; })
      usable;
  };
in
{
  config = lib.mkMerge [
    # Always defined (a read-only option takes exactly one definition); empty when off.
    { infra.addons.llmAgents.mcp.rendered = if cfg.enable then rendered else { }; }

    (mkIf cfg.enable {
    assertions = [
      {
        assertion = input != null;
        message = "infra.addons.llmAgents.enable is set, but mkFleet was not given the input: pass `addons.llm-agents = inputs.llm-agents;` (a flake input on github:numtide/llm-agents.nix) to fleetkit.lib.mkFleet.";
      }
      {
        assertion = input == null || unknown == [ ];
        message = "infra.addons.llmAgents.packages names packages that llm-agents.nix does not provide for ${system}: ${lib.concatStringsSep ", " unknown}. List them with `nix flake show github:numtide/llm-agents.nix`.";
      }
      {
        assertion = cfg.unfree.enable || unfreePicked == [ ];
        message = "infra.addons.llmAgents.packages includes unfree packages (${lib.concatStringsSep ", " unfreePicked}). Set infra.addons.llmAgents.unfree.enable = true to accept their licences, or drop them.";
      }
      {
        assertion = unresolved == [ ];
        message = "infra.addons.llmAgents.mcp.servers entries with no usable command: ${lib.concatStringsSep ", " unresolved}. Each needs `command`, or a `package` that is also listed in infra.addons.llmAgents.packages.";
      }
    ];

    environment.systemPackages = map (n: upstream.${n}) installable;

    environment.etc = mkIf cfg.mcp.writeFiles {
      "fleetkit/mcp/claude-code.json".text = builtins.toJSON rendered.claudeCode;
      "fleetkit/mcp/opencode.json".text = builtins.toJSON rendered.opencode;
    };
    })
  ];
}
