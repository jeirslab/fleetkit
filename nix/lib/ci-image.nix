# The CI environment as an OCI image `act` can run jobs in (org engine rework).
#
# Not the NixOS rootfs tarball the `docker` image target produces: `act` and
# `container:` jobs replace the entrypoint (`tail -f /dev/null`, then `sh` /
# `node` per step), so a job image needs a conventional root — tools at /bin,
# a PATH, a passwd, /tmp, nix configured — with nothing to "boot". This is the
# same toolchain the `infra.build.ciEnv` profile installs on a host, laid out
# that way. Load it with `docker load < result`; the profile's `act` wrapper
# names it (`infra.build.ciEnv.actImage`).
{ pkgs
, name ? "nixos-bootstrap"
, tag ? "latest"
, extraPackages ? [ ]
, claudePackage ? pkgs.claude-code
, withClaude ? true
}:
let
  toolchain = with pkgs; [
    bashInteractive coreutils findutils gnugrep gnused gawk diffutils which
    gnutar gzip xz bzip2 curl cacert openssh git gh jq
    nix nodejs python3 opentofu sops age just nettools
  ] ++ pkgs.lib.optional withClaude claudePackage ++ extraPackages;
  root = pkgs.buildEnv {
    name = "ci-image-root";
    paths = toolchain;
    pathsToLink = [ "/bin" "/share" "/etc" "/lib" ];
  };
in
pkgs.dockerTools.buildLayeredImage {
  inherit name tag;
  contents = [ root pkgs.dockerTools.fakeNss pkgs.dockerTools.caCertificates ];
  # `act` mounts the workspace under /root or a given path and expects a
  # writable /tmp; nix wants its own dirs and a daemon-less config.
  extraCommands = ''
    mkdir -p tmp root usr etc/nix nix/var/nix/{db,profiles,gcroots,temproots,userpool}
    chmod 1777 tmp
    ln -s /bin usr/bin
    cat > etc/nix/nix.conf <<'CONF'
    experimental-features = nix-command flakes
    build-users-group =
    sandbox = false
    CONF
  '';
  config = {
    Env = [
      "PATH=/bin:/usr/bin"
      "HOME=/root"
      "USER=root"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "GIT_SSL_CAINFO=/etc/ssl/certs/ca-bundle.crt"
      "CI_CLAUDE_MD=/etc/ci/CLAUDE.md"
    ];
    WorkingDir = "/root";
    Cmd = [ "/bin/bash" ];
  };
}
