# The `llm-prefetch` command. Kept as its own file so the gate can run the very
# same script against a fake upstream.
{ pkgs, port }:
pkgs.writers.writePython3Bin "llm-prefetch"
  { flakeIgnore = [ "E501" "E302" "E305" ]; }
  (builtins.replaceStrings [ "@port@" ] [ (toString port) ] (builtins.readFile ./prefetch.py))
