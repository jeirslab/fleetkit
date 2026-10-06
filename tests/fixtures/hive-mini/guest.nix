# The NixOS module both fixture guests name.
_: {
  services.openssh.enable = true;
  system.stateVersion = "25.05";
}
