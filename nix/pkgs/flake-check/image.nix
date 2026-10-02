# flake-check-image — the smallest image that can run `flake-check`:
# single-user nix (root, no daemon, no sandbox — it only evaluates), git
# for git+ inputs, CA certs, and flake-check itself. Published to
# ghcr.io/<owner>/fleetkit-flake-check by .github/workflows/flake-check-image.yml;
# the flake-check workflow runs its eval step in it on GitHub-hosted runners
# instead of installing nix every run. Self-hosted NixOS runners don't use
# it: their own nix and warm store are faster.
#
# Not a `container:` job image on purpose: Actions mounts its own glibc
# node into job containers for JS actions (checkout), which can't run in a
# non-FHS image. The workflow `docker run`s this for the one eval step.
{ dockerTools, nix, git, cacert, coreutils, bashInteractive, flake-check, writeTextDir }:

dockerTools.buildLayeredImageWithNixDb {
  name = "fleetkit-flake-check";
  tag = "latest";
  contents = [
    flake-check
    nix
    git
    cacert
    coreutils
    bashInteractive
    dockerTools.fakeNss # /etc/passwd + /etc/group with root/nobody
    # The checkout is bind-mounted from the runner and owned by its user, not
    # the container's root; without this libgit2 refuses to open it.
    (writeTextDir "etc/gitconfig" ''
      [safe]
      	directory = *
    '')
    (writeTextDir "etc/nix/nix.conf" ''
      experimental-features = nix-command flakes
      sandbox = false
      build-users-group =
      filter-syscalls = false
      accept-flake-config = false
    '')
  ];
  extraCommands = ''
    mkdir -p tmp root
    chmod 1777 tmp
  '';
  config = {
    Cmd = [ "flake-check" "--help" ];
    WorkingDir = "/work";
    Env = [
      "USER=root"
      "HOME=/root"
      "PATH=/bin"
      "SSL_CERT_FILE=${cacert}/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=${cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
  };
}
