# Acceptance gate for the llm-serving add-on.
#
# A module no host enables is never evaluated by `nix flake check`, so this
# INSTANTIATES the cold store on stub hosts through mkFleet and asserts policy:
#   * the olah package builds (its pythonImportsCheck is what catches a broken
#     pin/relaxed-deps combination) and exposes `olah-cli`
#   * the service ExecStart carries the configured port, mirror netloc and
#     repos path, and NO --cache-size-limit by default (a cold store never
#     evicts); with cacheSizeLimit set, it does
#   * the user is a static system user (no DynamicUser)
#   * the firewall port is closed by default and open with openFirewall
#   * enabling without mirrorNetloc is a hard eval error; a disabled add-on
#     defines no service and an empty clientEnv
#   * clientEnv is exactly HF_ENDPOINT + HF_HUB_DISABLE_XET
# plus a runtime smoke test (see `smoke` below).
{ nixpkgs, mkFleet }:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  inherit (pkgs) lib;

  # The same overlay the module applies, so the package built here is the one
  # a host gets.
  overlaid = import nixpkgs {
    system = "x86_64-linux";
    overlays = [ (import ./overlays.nix) ];
  };
  inherit (overlaid) olah;

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
    notes = "check-suite llm-serving add-on host";
  };

  hosts = {
    # name = [ vm_id ip module ]
    serving-ok = [ 9311 "192.0.2.141" {
      infra.addons.llmServing.coldStore = {
        enable = true;
        port = 8099;
        mirrorNetloc = "cold.example.test:8099";
        dataDir = "/srv/cold";
        prefetch.enable = true;
      };
    } ];
    serving-open = [ 9312 "192.0.2.142" {
      infra.addons.llmServing.coldStore = {
        enable = true;
        mirrorNetloc = "cold.example.test:8090";
        openFirewall = true;
        cacheSizeLimit = "2TB";
      };
    } ];
    serving-no-netloc = [ 9313 "192.0.2.143" {
      infra.addons.llmServing.coldStore.enable = true;
    } ];
    serving-off = [ 9314 "192.0.2.144" { } ];
  };

  fixture = {
    config.fleet.compute = lib.mapAttrs (_: h: compute (builtins.elemAt h 0) (builtins.elemAt h 1)) hosts;
    config.fleet.hostsRegistry = lib.mapAttrs (_: h: { ... }: builtins.elemAt h 2) hosts;
  };

  fleet = mkFleet { modules = [ ../../templates/minimal/fleet fixture ]; };

  cfgOf = n: fleet.nixosConfigurations.${n}.config;
  forced = n: builtins.unsafeDiscardOutputDependency (cfgOf n).system.build.toplevel.drvPath;
  fails = n: !(builtins.tryEval (builtins.deepSeq (cfgOf n).system.build.toplevel.drvPath true)).success;
  execOf = n: (cfgOf n).systemd.services.olah.serviceConfig.ExecStart;
  has = needle: hay: lib.hasInfix needle hay;
  ok = cfgOf "serving-ok";
  open = cfgOf "serving-open";
  off = cfgOf "serving-off";

  summary = builtins.toJSON {
    execHasPort = has "--port 8099" (execOf "serving-ok");
    execHasMirror = has "--mirror-netloc cold.example.test:8099" (execOf "serving-ok");
    execHasLfsMirror = has "--mirror-lfs-netloc cold.example.test:8099" (execOf "serving-ok");
    execHasRepos = has "--repos-path /srv/cold/repos" (execOf "serving-ok");
    execHasBind = has "--host 0.0.0.0" (execOf "serving-ok");
    noEvictionByDefault = !(has "--cache-size-limit" (execOf "serving-ok"));
    evictionWhenAsked = has "--cache-size-limit 2TB" (execOf "serving-open");
    staticUser = ok.users.users.olah.isSystemUser && ok.users.users.olah.group == "olah"
      && !(ok.systemd.services.olah.serviceConfig.DynamicUser or false)
      && ok.systemd.services.olah.serviceConfig.User == "olah";
    tmpfilesOwned = builtins.elem "d /srv/cold/repos 0755 olah olah - -" ok.systemd.tmpfiles.rules;
    firewallClosedByDefault = !(builtins.elem 8099 ok.networking.firewall.allowedTCPPorts);
    firewallOpenWhenAsked = builtins.elem 8090 open.networking.firewall.allowedTCPPorts;
    prefetchInstalled = lib.any (p: (p.name or "") == "llm-prefetch") ok.environment.systemPackages;
    prefetchOffByDefault = !(lib.any (p: (p.name or "") == "llm-prefetch") open.environment.systemPackages);
    clientEnvExact = ok.infra.addons.llmServing.coldStore.clientEnv == {
      HF_ENDPOINT = "http://cold.example.test:8099";
      HF_HUB_DISABLE_XET = "1";
    };
    missingNetlocRefused = fails "serving-no-netloc";
    offHasNoService = !(off.systemd.services ? olah);
    offClientEnvEmpty = off.infra.addons.llmServing.coldStore.clientEnv == { };
  };

  # Runtime smoke test, fully offline. A tiny python server impersonates
  # huggingface.co (via olah-cli's --hf-scheme/--hf-netloc/--hf-lfs-netloc);
  # the real `llm-prefetch` tool pulls a file through Olah (one upstream
  # transfer), then a plain client fetch must return the same bytes with NO
  # further upstream transfer, i.e. from the cache. (Not tested by killing the
  # upstream: Olah 0.5.1 answers 504 for a cached file when upstream is
  # unreachable, which is itself worth knowing.)
  prefetch = import ./prefetch.nix { pkgs = overlaid; port = 18090; };
  smoke = pkgs.runCommand "fleetkit-addon-llm-serving-smoke"
    {
      nativeBuildInputs = [ olah prefetch pkgs.curl pkgs.python3 pkgs.ripgrep ];
      fakeUpstream = ./smoke-upstream.py;
    } ''
    export HOME=$TMPDIR
    mkdir state && cd state

    python3 $fakeUpstream 18081 $PWD/up.log ${./smoke-weights.bin} &
    UP=$!
    olah-cli --host 127.0.0.1 --port 18090 \
      --hf-scheme http --hf-netloc 127.0.0.1:18081 --hf-lfs-netloc 127.0.0.1:18081 \
      --mirror-scheme http --mirror-netloc 127.0.0.1:18090 --mirror-lfs-netloc 127.0.0.1:18090 \
      --repos-path $PWD/repos --log-path $PWD/logs > olah.out 2>&1 &
    OL=$!
    trap 'kill $UP $OL 2>/dev/null || true' EXIT

    for i in $(seq 60); do
      # Not `/`: Olah 0.5.1's index page is broken against this starlette
      # (TemplateResponse signature), which is irrelevant to the mirror API.
      curl -s -o /dev/null http://127.0.0.1:18090/api/models/fake/model && break
      sleep 0.5
    done

    # Upstream byte transfers of the file: each GET of the resolve URL below.
    pulls() { { rg -c '^GET /fake/model/resolve/.*/weights.bin' up.log || true; } | head -n1; }
    dump() { echo "--- olah"; cat olah.out; echo "--- upstream"; cat up.log; }

    # 1. Warm the cache with the real llm-prefetch tool (listing from the fake
    #    upstream, bytes THROUGH the mirror). --include must skip the other file.
    export LLM_COLD_URL=http://127.0.0.1:18090 LLM_PREFETCH_HF_URL=http://127.0.0.1:18081
    llm-prefetch fake/model --include 'weights.*' --dry-run | tee dry.out
    rg -q 'skip/other' dry.out && { echo "--include did not filter"; exit 1; }
    llm-prefetch fake/model --include 'weights.*' || { dump; exit 1; }
    [ "$(pulls)" = 1 ] || { echo "expected 1 upstream pull after prefetch, got $(pulls)"; dump; exit 1; }

    # 2. A client fetch is served from the cache: right bytes, no new upstream pull.
    curl -sSf -L --max-time 60 http://127.0.0.1:18090/fake/model/resolve/main/weights.bin -o got.bin || { dump; exit 1; }
    cmp got.bin ${./smoke-weights.bin} || { echo "served bytes differ from upstream"; exit 1; }
    [ "$(pulls)" = 1 ] || { echo "second fetch hit upstream again (not cached): $(pulls) pulls"; dump; exit 1; }
    touch $out
  '';
in
{
  addon-llm-serving = pkgs.runCommand "fleetkit-addon-llm-serving-check"
    {
      nativeBuildInputs = [ pkgs.jq ];
      # strict env attrs: building/instantiating forces these
      inherit olah;
      okToplevelDrv = forced "serving-ok";
      openToplevelDrv = forced "serving-open";
      offToplevelDrv = forced "serving-off";
      inherit summary smoke;
      passAsFile = [ "summary" ];
    } ''
    jq -e 'to_entries | all(.value == true)' "$summaryPath" >/dev/null \
      || { echo "llm-serving add-on policy check failed:"; jq . "$summaryPath"; exit 1; }
    test -x ${olah}/bin/olah-cli
    touch $out
  '';
}
