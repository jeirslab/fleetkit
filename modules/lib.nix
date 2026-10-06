# Shared helpers for the fleet schema. This is NOT a module: block modules
# use it as `h = import ./lib.nix { inherit lib; };`.
#
# Owned by the schema architect / integrator. Block owners must not edit it;
# a helper that is missing here is written locally in the block file and
# reported, so the integrator can lift it.
{ lib }:
let
  inherit (lib)
    types
    mkOption
    concatMap
    mapAttrsToList
    attrValues
    concatStringsSep
    ;

  idsOf = attrs: mapAttrsToList (_: v: v.id) attrs;
  # Two-level containers (guests.<estate>.<name>, repos.<estate>.<name>, ...).
  nestedIdsOf = attrs: concatMap idsOf (attrValues attrs);

  # A reference option is a plain string whose value must be the id of an
  # existing thing of one kind. `ids` is the list of known ids of that kind
  # (usually config.fleet.report.ids.<kind>). Every failure renders as
  #   <where>: <kind> reference "<value>" does not exist (known <kind> ids: ...)
  # and negative tests match on that text. Collect these into
  # config.assertions; lib/default.nix refuses to return a fleet whose
  # assertions fail.
  refAssertion =
    {
      where,
      kind,
      ids,
    }:
    value: {
      assertion = builtins.elem value ids;
      message = "${where}: ${kind} reference \"${value}\" does not exist (known ${kind} ids: ${concatStringsSep ", " ids})";
    };
in
{
  inherit idsOf nestedIdsOf;

  # A read-only identifier. Its only definition is this default, so any
  # attempt by config to set `id` fails with "read-only, but it's set
  # multiple times".
  mkId =
    id:
    mkOption {
      type = types.str;
      readOnly = true;
      default = id;
      description = "Derived identifier (${id}). Read-only; never set by config.";
    };

  # "<parent>/<name>": the one qualification rule for every id.
  qualify = parent: name: "${parent}/${name}";

  # Reference validation: see refAssertion in the let block above.
  inherit refAssertion;

  # A sops reference ("sops:<estate>/<file>#<key>") must be one that a
  # declared secrets file derives (ids = config.fleet.report.ids.secretRef);
  # a hand-written literal for an undeclared key fails like any other
  # dangling reference. With `estate`, the ref must also live in that
  # estate's secrets (an option owned by one estate cannot read another's).
  secretRefAssertions =
    {
      where,
      ids,
      estate ? null,
    }:
    value:
    let
      # A value without the sops: prefix may be a pasted secret: never
      # echo it into an assertion message.
      isRef = lib.hasPrefix "sops:" value;
      shown = if isRef then value else "<withheld: not a sops: reference>";
    in
    [
      (
        if isRef then
          refAssertion { inherit where ids; kind = "secret"; } value
        else
          {
            assertion = false;
            message = "${where}: secret reference ${shown} does not exist (a secret reference must start with sops:)";
          }
      )
    ]
    ++ lib.optional (estate != null) {
      assertion = lib.hasPrefix "sops:${estate}/" value;
      message = "${where}: secret reference \"${shown}\" belongs to another estate (expected sops:${estate}/...)";
    };

  # One assertion per element of a list of references.
  refAssertions = spec: map (refAssertion spec);

  # For nullOr str references: no assertion when the value is null.
  optionalRefAssertions = spec: value: lib.optional (value != null) (refAssertion spec value);

  # The id index: every id of every kind, from the evaluated config.fleet.
  # Exposed as config.fleet.report.ids. Only attribute names and `id`
  # options are read, so building it never forces a reference value.
  index =
    fleet:
    let
      sites = attrValues fleet.sites;
      estates = attrValues fleet.estates;
      nodes = concatMap (s: attrValues s.nodes) sites;
      networks = concatMap (s: attrValues s.networks) sites;
    in
    {
      estate = idsOf fleet.estates;
      site = idsOf fleet.sites;
      network = map (n: n.id) networks;
      router = concatMap (n: idsOf n.routers) networks;
      bridge = concatMap (s: idsOf s.bridges) sites;
      node = map (n: n.id) nodes;
      pcie = concatMap (n: idsOf n.pcie) nodes;
      provider = concatMap (s: idsOf s.providers) sites;
      storage = concatMap (s: idsOf s.storage) sites;
      image = concatMap (s: idsOf s.images) sites;
      guest = nestedIdsOf fleet.guests;
      zone = idsOf fleet.zones;
      backend = idsOf fleet.backends;
      account = nestedIdsOf fleet.accounts;
      repo = nestedIdsOf fleet.repos;
      principal = idsOf fleet.operators.principals;
      role = idsOf fleet.operators.roles;
      grant = idsOf fleet.operators.grants;
      tailnet = concatMap (e: idsOf e.tailnets) estates;
      environment = concatMap (e: idsOf e.environments) estates;
      pool = concatMap (e: nestedIdsOf e.pools) estates;
      secretFile = concatMap (e: idsOf e.secrets.files) estates;
      # Every "sops:<key>" declared by any secrets file (kind "secret").
      secretRef = concatMap (e: concatMap (f: attrValues f.ref) (attrValues e.secrets.files)) estates;
    };
}
