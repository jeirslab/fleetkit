# Deploy keys

`tools/deploy-key` makes the SSH identity a deploy server (or any other
automation) logs in to a fleet's hosts with, and keeps its private half in a
sops file. It is the tool for every estate and site that gets a deploy
server: nobody copies an operator's key onto a machine.

```sh
tools/deploy-key create --name fleet-deploy \
    --sops-file secrets/hosts/deploy.yaml --key-path deploy_ssh_key
tools/deploy-key public --sops-file secrets/hosts/deploy.yaml --key-path deploy_ssh_key
```

`create`:

1. checks that the sops file exists and that your key decrypts it (it does
   not create one: make it with `sops` so the right creation rule applies),
   and that nothing is stored at the key path yet;
2. generates an ed25519 key pair in a 0700 directory on a memory file system,
   and removes the directory;
3. stores the private key with `sops set --value-stdin` (stdin only: never
   an argument, never the environment, never printed);
4. reads it back and checks that it is the key it made;
5. prints the public key, then the lines for the estate's model.

`public` prints the public key of what is stored, derived from the private
half: what to compare a host's `authorized_keys` with.

## Scope: one key per boundary

A key is as wide as the grant that names it. The printed lines are a service
principal and a grant of a role (default `deploy`; `nixos/base.nix` lets
principals who may deploy log in as root):

```nix
# fleet.operators.principals
fleet-deploy = {
  kind = "service";
  keys.ssh.main = { public = "ssh-ed25519 AAAA... fleet-deploy (deploy key)"; };
};
# fleet.operators.grants
fleet-deploy = { principals = [ principals.fleet-deploy.id ]; role = "deploy"; };
```

`--estate E` and `--region R` (repeatable) put a `where` on the grant, so
the hosts outside it never get the key:

```sh
tools/deploy-key create --name xg-deploy --estate xgcs      ...   # one estate
tools/deploy-key create --name lab-ca-deploy --region us-ca ...   # one site's region
```

A deploy server for one tenant gets a key granted in that estate only; a
site's server one for its region; several keys can be stored in one sops file
under different paths. The model is what decides where a key is accepted;
the tool only prints the lines.

## Rotation

`--rotate` replaces the stored key and prints the new public key. Put it in
the model and deploy the hosts with a key they still accept (the old deploy
key works until a host is deployed, or an operator's key); the old key stops
working host by host as they are deployed.

## What it does not do

It does not edit the model, deploy anything, or create the sops file. The
sops file's recipients decide who can use the key: normally the estate's
operators and the one host the deploy server runs on.
