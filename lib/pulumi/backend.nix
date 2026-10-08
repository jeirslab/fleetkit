# backendFor: the Pulumi state backend of one stack, from fleet.backends.
# Evaluation only; nothing here is a secret (secrets are named, as sops file
# and key path, for the deploy runner to decrypt).
#
#   import ./backend.nix { lib; fleet; estate; } { id; project; } -> backend | null
#
#   pg       postgres://, the connection string from connRef (it carries the
#            password). A project listed in localStacks keeps local state (the
#            stack that runs the database cannot keep its state in it).
#   s3       s3://<bucket>/<estate>/ with path-style addressing (needed for an
#            IP or LAN endpoint), the access keys from sops into the env:
#            linode: the estate's bucket (buckets.<estate>), its keyPrefix;
#            garage: bucket, the endpoint the garage guest's lan address, port
#            3900, credsRef a subtree with access_key_id and secret_access_key.
#   local    a directory, relative to the runner's state directory.
{
  lib,
  fleet,
  estate,
}:
{ id, project }:
let
  where = "backendFor ${project}: fleet.backends.${id}";
  b = fleet.backends.${id} or (throw "${where}: no such backend");
  sopsRef = import ../sops-ref.nix { inherit fleet estate where; };
  val = ref: { inherit (sopsRef ref) path extract; };
  sub = ref: k: (val ref) // { extract = (val ref).extract ++ [ k ]; };
  s3 = endpoint: region: bucket: prefix: env: {
    type = "s3";
    url = "s3://${bucket}/${prefix}?endpoint=${endpoint}&region=${region}&use_path_style=true";
    inherit env;
  };
  guestAddr =
    gid:
    let
      parts = lib.splitString "/" gid;
      g = fleet.guests.${lib.elemAt parts 0}.${lib.elemAt parts 1};
    in
    g.ipv4.lan or (throw "${where}: garage guest ${gid} has no lan address");
in
if b.type == "local" then
  {
    type = "local";
    path = if b.path == null then "pulumi-state" else b.path;
  }
else if b.type == "pg" then
  if lib.elem project (b.localStacks or [ ]) then
    {
      type = "local";
      path = "pulumi-state";
    }
  else
    {
      type = "postgres";
      urlSecret = val b.connRef;
    }
else if b.provider == "linode" then
  let
    bk = b.buckets.${estate} or (throw "${where}: no bucket for estate ${estate} (buckets.${estate})");
  in
  s3 b.endpoint b.region bk.name bk.keyPrefix {
    AWS_ACCESS_KEY_ID = val b.credentials.accessKeyIdRef;
    AWS_SECRET_ACCESS_KEY = val b.credentials.secretAccessKeyRef;
  }
else if b.provider == "garage" then
  if b.bucket == null then
    throw "${where}: a garage backend needs bucket to hold Pulumi state"
  else
    s3 "http://${guestAddr b.host}:3900" "garage" b.bucket "${estate}/" {
      AWS_ACCESS_KEY_ID = sub b.credsRef "access_key_id";
      AWS_SECRET_ACCESS_KEY = sub b.credsRef "secret_access_key";
    }
else
  throw "${where}: no Pulumi mapping for this backend"
