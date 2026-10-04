# infra.addons.llmServing — options for the LLM-serving add-on (currently: the
# model cold store). The module that acts on them is ./default.nix; the olah
# package comes from ./overlays.nix.
{ lib, ... }:
let
  inherit (lib) mkEnableOption mkOption types;
in
{
  options.infra.addons.llmServing.coldStore = {
    enable = mkEnableOption "the model cold store on this host: Olah, a Hugging Face pull-through cache. Models are downloaded from Hugging Face once, onto this always-on host, and every other host pulls them over the LAN";

    port = mkOption {
      type = types.port;
      default = 8090;
      description = "TCP port Olah listens on.";
    };

    listenAddress = mkOption {
      type = types.str;
      default = "0.0.0.0";
      description = "Address Olah binds to. Narrow it (for example to a LAN address) to keep the cache off other interfaces.";
    };

    mirrorNetloc = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "cold-store.lan:8090";
      description = ''
        The `host:port` clients use to reach this cache, as they would type it
        into HF_ENDPOINT. Required when the cold store is enabled (evaluation
        fails otherwise): Olah rewrites the redirects it hands back with this
        value, so a wrong or missing one sends clients to an address that does
        not work.
      '';
    };

    dataDir = mkOption {
      type = types.path;
      default = "/var/lib/olah";
      description = ''
        State directory: the cache lives in `repos/` and the logs in `logs/`
        beneath it. Owned by the static `olah` system user. Put a large volume
        here; a mount at this path is the usual way.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open `port` in the host firewall. Off by default so enabling the cache never exposes it by accident.";
    };

    cacheSizeLimit = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "2TB";
      description = ''
        Olah's `--cache-size-limit` (for example `500GB`). Null, the default,
        passes no limit and nothing is ever evicted: this is a COLD store, whose
        job is to keep what was downloaded. Set a
        limit only if you want it to behave as an ordinary evicting cache.
      '';
    };

    prefetch.enable = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Install the `llm-prefetch` command. It lists a Hugging Face repo's files
        from huggingface.co, then streams each wanted file through this mirror
        and discards the bytes, so Olah caches them without the host's disk ever
        staging a model separately. Usage:
        `llm-prefetch REPO [--include GLOB]... [--revision REV] [--rate-mib N] [--dry-run]`.
        It checks every file's length (a short read is retried) and gives up on
        a file after five attempts. The mirror URL defaults to this host's own
        port and can be overridden with the LLM_COLD_URL environment variable.
      '';
    };

    clientEnv = mkOption {
      type = types.attrsOf types.str;
      readOnly = true;
      description = ''
        Environment a client needs to use this cache: HF_ENDPOINT pointing at
        `mirrorNetloc`, and HF_HUB_DISABLE_XET=1. Read-only, for other modules
        to reuse (for example a service's `environment`); empty until the cold
        store is enabled and `mirrorNetloc` is set.

        Tested limits. The mirror does not speak Hugging Face's Xet protocol,
        hence the HF_HUB_DISABLE_XET=1 clients must carry. llama.cpp's
        `-hf repo:quant` shorthand fails with a 404 through it (its
        /v2/<repo>/manifests lookup is not implemented), so GGUF models must be
        fetched by explicit filename (`hf download`, or `llm-prefetch`) and run
        with `-m`. A cache does not make a download faster than the link it
        crosses: the gain is that each model is fetched from the internet once. Also
        tested: with the upstream unreachable, Olah 0.5.1 answers 504 even for
        a file it has cached, so do not rely on it as an offline archive.
      '';
    };
  };
}
