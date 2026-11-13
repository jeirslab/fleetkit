# mkWorkflowCaller: the text of a caller workflow file that runs one of the
# kit's reusable workflows (.github/workflows/<workflow>.yml) pinned to a
# commit. Pure; nothing here talks to GitHub.
#
#   mkWorkflowCaller {
#     name;                       # the caller workflow's `name`
#     workflow;                   # file stem under .github/workflows (e.g. "check-flake")
#     ref;                        # 40 lowercase hex characters: a commit, never a branch or tag
#     on;                         # attrset: the triggers
#     with ? { };                 # inputs passed to the workflow
#     secrets ? { };              # NAME = "${{ secrets.X }}" (expression strings)
#     permissions ? { };          # top-level permissions; omitted when empty
#     kitRepo ? "jeirslab/fleetkit";
#   } -> string
#
# The output is deterministic: keys sorted, block mappings, every scalar and
# list written as JSON (a valid YAML flow scalar/sequence). It is a subset of
# YAML that tests/workflow_caller.sh parses strictly.
{ lib, ... }@args:
let
  known = [
    "lib"
    "name"
    "workflow"
    "ref"
    "on"
    "with"
    "secrets"
    "permissions"
    "kitRepo"
  ];
  unknown = lib.filter (k: !(lib.elem k known)) (lib.attrNames args);
  need =
    k:
    args.${k} or (throw "mkWorkflowCaller: option `${k}` is required");
  name = need "name";
  workflow = need "workflow";
  ref = need "ref";
  on = need "on";
  inputs = args."with" or { };
  secrets = args.secrets or { };
  permissions = args.permissions or { };
  kitRepo = args.kitRepo or "jeirslab/fleetkit";

  json = builtins.toJSON;
  isStr = builtins.isString;

  # Block mapping; attrsets nest, everything else is JSON. Empty attrset: {}.
  block =
    indent: set:
    lib.concatMapStringsSep "\n" (
      k:
      let
        v = set.${k};
        key = if k == "on" then k else json k;
      in
      if builtins.isAttrs v && v != { } then
        "${indent}${key}:\n${block "${indent}  " v}"
      else
        "${indent}${key}: ${json v}"
    ) (lib.attrNames set);

  section =
    indent: key: set:
    lib.optional (set != { }) "${indent}${key}:\n${block "${indent}  " set}";
  parts = [
    "name: ${json name}"
    "on:\n${block "  " on}"
  ]
  ++ section "" "permissions" permissions
  ++ [
    "jobs:\n  call:\n    uses: ${json "${kitRepo}/.github/workflows/${workflow}.yml@${ref}"}"
  ]
  ++ section "    " "with" inputs
  ++ section "    " "secrets" secrets;
in
assert lib.assertMsg (unknown == [ ]) "mkWorkflowCaller: unknown option(s): ${lib.concatStringsSep ", " unknown}";
assert lib.assertMsg (
  isStr name && name != ""
) "mkWorkflowCaller: option `name` must be a non-empty string";
assert lib.assertMsg (
  isStr workflow && builtins.match "[A-Za-z0-9][A-Za-z0-9._-]*" workflow != null
) "mkWorkflowCaller: option `workflow` must be a file stem such as check-flake";
assert lib.assertMsg (
  isStr ref && builtins.match "[0-9a-f]{40}" ref != null
) "mkWorkflowCaller: option `ref` must be a 40 character lowercase hex commit SHA (a branch, tag or short SHA is refused)";
assert lib.assertMsg (
  isStr kitRepo && builtins.match "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" kitRepo != null
) "mkWorkflowCaller: option `kitRepo` must be <owner>/<repo>";
assert lib.assertMsg (
  builtins.isAttrs on && on != { }
) "mkWorkflowCaller: option `on` must be a non-empty attrset";
assert lib.assertMsg (builtins.isAttrs inputs) "mkWorkflowCaller: option `with` must be an attrset";
assert lib.assertMsg (
  builtins.isAttrs secrets && lib.all isStr (lib.attrValues secrets)
) "mkWorkflowCaller: option `secrets` must be an attrset of expression strings";
assert lib.assertMsg (
  builtins.isAttrs permissions && lib.all isStr (lib.attrValues permissions)
) "mkWorkflowCaller: option `permissions` must be an attrset of strings";
lib.concatStringsSep "\n" parts + "\n"
