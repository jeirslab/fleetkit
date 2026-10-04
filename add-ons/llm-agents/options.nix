# infra.addons.llmAgents — options for the numtide/llm-agents.nix add-on.
# The module that acts on them is ./default.nix.
{ lib, ... }:
let
  inherit (lib) mkEnableOption mkOption types;
in
{
  options.infra.addons.llmAgents = {
    enable = mkEnableOption "the llm-agents.nix add-on on this host (needs mkFleet's `addons.llm-agents` input)";

    packages = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "opencode" "codegraph" ];
      description = ''
        Names of llm-agents.nix packages to install on this host, taken from
        the input's `packages.<system>` set (browse it with
        `nix flake show github:numtide/llm-agents.nix`). A name the input does
        not provide fails evaluation, naming it.
      '';
    };

    unfree.enable = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Accept llm-agents.nix packages whose licence is unfree (for example
        claude-code). Off by default: selecting an unfree package then fails
        evaluation and names the package, so an unfree tool is never installed
        by accident. This gates only this add-on's own packages; it does not
        touch the host's nixpkgs allowUnfree settings.
      '';
    };

    mcp = {
      servers = mkOption {
        default = { };
        description = ''
          MCP servers provided by selected llm-agents.nix packages, declared
          here for agent clients to use. This is deliberately independent of
          fleetkit's own in-fleet MCP hosting (infra.integrations.mcp): these
          are tools running next to the person using them, not fleet services.
          Only declarations are produced (see `rendered` and `writeFiles`);
          nothing is started.
        '';
        example = lib.literalExpression ''
          { codegraph = { package = "codegraph"; args = [ "serve" "--mcp" ]; }; }
        '';
        type = types.attrsOf (types.submodule {
          options = {
            package = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = "Name of a package listed in `packages` whose main program is the MCP server. Either this or `command` is required; `command` wins if both are set.";
            };
            command = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = "Explicit executable to run instead of a package's main program.";
            };
            args = mkOption {
              type = types.listOf types.str;
              default = [ ];
              description = "Arguments passed to the server.";
            };
            environment = mkOption {
              type = types.attrsOf types.str;
              default = { };
              description = ''
                Environment variables for the server. These land in the Nix
                store and in world-readable rendered files: never put secrets
                here; reference a variable the client expands (such as
                `$FOO` or `''${FOO}`, per that client) and provide it out of band.
              '';
            };
          };
        });
      };

      writeFiles = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Also write the rendered client configs to /etc/fleetkit/mcp/claude-code.json
          and /etc/fleetkit/mcp/opencode.json on this host, for a client or a
          user-level include to pick up. Off by default so the add-on never
          creates files you did not ask for.
        '';
      };

      rendered = mkOption {
        type = types.attrsOf types.anything;
        readOnly = true;
        description = ''
          The declared MCP servers in each client's own config shape
          (`claudeCode` for a .mcp.json, `opencode` for opencode.json's `mcp`
          key), for other modules to consume. Read-only; empty unless the
          add-on is enabled.
        '';
      };
    };
  };
}
