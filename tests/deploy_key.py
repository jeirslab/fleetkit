#!/usr/bin/env python3
"""Tests tools/deploy-key with a fake sops (the "encrypted" file is plain
JSON) and the real ssh-keygen. No network, no real sops. A private key inside
the --sops-file itself is expected; anywhere else (output, argv, the
environment, a file left behind) it is a failure. Prints one JSON line
{"deploy_key":"pass"|"fail"}; exit 0 iff pass."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "deploy-key")
fails = []

FAKE_SOPS = r'''#!/usr/bin/env python3
import json, os, re, sys
a = sys.argv[1:]
open(os.environ["FAKE_SOPS_CALLS"], "a").write(json.dumps({"argv": a, "env": "PRIVATE KEY" in json.dumps(dict(os.environ))}) + "\n")
def keys(p): return re.findall(r'\["([^"]+)"\]', p)
def load(f):
    if not os.path.isfile(f): sys.exit(1)
    try: return json.load(open(f))
    except ValueError: sys.stderr.write("not a sops file\n"); sys.exit(1)
if a[:2] == ["set", "--value-stdin"]:
    if os.environ.get("FAKE_SOPS_FAIL_SET"): sys.stderr.write("cannot encrypt\n"); sys.exit(3)
    d = load(a[2]); cur = d; ks = keys(a[3])
    for k in ks[:-1]: cur = cur.setdefault(k, {})
    cur[ks[-1]] = json.loads(sys.stdin.read()) if not os.environ.get("FAKE_SOPS_CORRUPT") else "not the key"
    json.dump(d, open(a[2], "w")); sys.exit(0)
if a[:2] == ["decrypt", "--extract"]:
    cur = load(a[3])
    for k in keys(a[2]):
        if not isinstance(cur, dict) or k not in cur: sys.exit(1)
        cur = cur[k]
    sys.stdout.write(cur if isinstance(cur, str) else json.dumps(cur)); sys.exit(0)
if a[:1] == ["decrypt"]:
    load(a[1]); sys.exit(0)
sys.exit(2)
'''


def check(cond, what):
    if not cond:
        fails.append(what)
        print("deploy_key: FAIL " + what, file=sys.stderr)


def main():
    if not shutil.which("ssh-keygen"):
        print("deploy_key: ssh-keygen is not on the PATH", file=sys.stderr)
        print(json.dumps({"deploy_key": "fail"}))
        return 1
    tmp = tempfile.mkdtemp(prefix="deploy-key-test-")
    sops = os.path.join(tmp, "sops")
    open(sops, "w").write(FAKE_SOPS)
    os.chmod(sops, 0o755)
    calls = os.path.join(tmp, "calls")
    f = os.path.join(tmp, "secrets.json")
    env = dict(os.environ, FAKE_SOPS_CALLS=calls)
    before = set(glob.glob("/dev/shm/deploy-key-*") + glob.glob(os.path.join(tempfile.gettempdir(), "deploy-key-[!t]*")))

    def tool(*args, **extra):
        p = subprocess.run([sys.executable, TOOL, *args, "--sops", sops], env=dict(env, **extra),
                           capture_output=True, text=True)
        check("PRIVATE KEY" not in p.stdout + p.stderr, f"private key in the output of {args[0]}")
        return p

    base = ["--sops-file", f, "--key-path", "deploy/ssh_key"]

    p = tool("create", "--name", "fleet-deploy", *base)
    check(p.returncode != 0 and "does not exist" in p.stderr, "a missing sops file is refused")

    open(f, "w").write("this is not encrypted")
    p = tool("create", "--name", "fleet-deploy", *base)
    check(p.returncode != 0 and "sops decrypt" in p.stderr, "a file sops cannot open is refused")

    json.dump({"other": "kept"}, open(f, "w"))
    p = tool("create", "--name", "Fleet Deploy", *base)
    check(p.returncode != 0, "a bad name is refused")
    p = tool("create", "--name", "fleet-deploy", "--sops-file", f, "--key-path", "a/b c")
    check(p.returncode != 0, "a bad key path is refused")
    check(json.load(open(f)) == {"other": "kept"}, "refusals store nothing")

    p = tool("create", "--name", "fleet-deploy", *base, FAKE_SOPS_FAIL_SET="1")
    check(p.returncode != 0 and "not stored" in p.stderr, "a failing sops set is an error")
    check(not p.stdout.strip(), "no public key is printed when storing failed")

    p = tool("create", "--name", "fleet-deploy", *base, FAKE_SOPS_CORRUPT="1")
    check(p.returncode != 0 and "reads back" in p.stderr and not p.stdout.strip(),
          "a key that does not read back is an error and prints no public key")
    json.dump({"other": "kept"}, open(f, "w"))

    p = tool("create", "--name", "fleet-deploy", *base)
    check(p.returncode == 0, "create succeeds: " + p.stderr[-200:])
    d = json.load(open(f))
    private = d.get("deploy", {}).get("ssh_key", "")
    check(("BEGIN OPENSSH " + "PRIVATE KEY") in private and d.get("other") == "kept", "the private key is stored, the rest is kept")
    pub1 = p.stdout.splitlines()[0] if p.stdout else ""
    check(pub1.startswith("ssh-ed25519 ") and pub1.endswith("fleet-deploy (deploy key)"), "the public key is printed first")
    check('kind = "service"' in p.stdout and 'role = "deploy"' in p.stdout and "where =" not in p.stdout
          and "principals.fleet-deploy.id" in p.stdout, "the snippet: principal and an unscoped grant")
    derived = subprocess.run(["ssh-keygen", "-y", "-f", "/dev/stdin"], input=private, capture_output=True, text=True).stdout
    check(" ".join(derived.split()[:2]) == " ".join(pub1.split()[:2]), "the printed public key is the stored key's")

    q = tool("public", *base)
    check(q.returncode == 0 and q.stdout.strip() == " ".join(pub1.split()[:2]), "public prints the stored key's public half")

    p = tool("create", "--name", "fleet-deploy", *base)
    check(p.returncode != 0 and "--rotate" in p.stderr, "an existing key is not replaced without --rotate")
    check(json.load(open(f))["deploy"]["ssh_key"] == private, "and it is left as it was")

    p = tool("create", "--name", "xg-deploy", *base, "--rotate", "--estate", "xgcs", "--region", "us-ca", "--role", "ship",
             "--comment", "xg deploys")
    pub2 = p.stdout.splitlines()[0] if p.stdout else ""
    check(p.returncode == 0 and pub2 and pub2.split()[1] != pub1.split()[1], "--rotate stores a new key")
    check('where = { regions = [ "us-ca" ]; estates = [ "xgcs" ]; }' in p.stdout and 'role = "ship"' in p.stdout
          and pub2.endswith("xg deploys"), "the snippet carries the scope, the role and the comment")

    q = tool("public", "--sops-file", f, "--key-path", "nothing/here")
    check(q.returncode != 0, "public of a path with no value is an error")

    for line in open(calls):
        c = json.loads(line)
        check(not any("PRIVATE KEY" in x for x in c["argv"]), "no private key in sops' arguments")
        check(not c["env"], "no private key in sops' environment")
    after = set(glob.glob("/dev/shm/deploy-key-*") + glob.glob(os.path.join(tempfile.gettempdir(), "deploy-key-[!t]*")))
    check(after <= before, "no scratch directory is left behind: %s" % sorted(after - before))
    hits = subprocess.run(["grep", "-rl", "PRIVATE KEY", tmp], capture_output=True, text=True).stdout.split()
    check([h for h in hits if h != sops] == [f], "the private key is in the sops file and nowhere else: %s" % hits)
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({"deploy_key": "fail" if fails else "pass"}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
