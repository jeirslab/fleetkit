#!/usr/bin/env python3
"""Tests tools/github-app-bootstrap against a fake GitHub API and a fake sops.
No network beyond 127.0.0.1, no real sops, no secrets. The fake sops keeps
the "encrypted" file as plain JSON, so a stand-in secret inside the --sops-file
itself is expected; anywhere else on disk it is a failure. Prints one JSON line
{"github_app_bootstrap":"pass"|"fail"}; exit 0 iff pass."""
import base64
import hashlib
import html
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "github-app-bootstrap")
PEM = "FAKE-PEM-LINE-ONE\nTESTKEYMATERIAL\nFAKE-PEM-LAST-LINE\n"  # stand-in; the tool does not parse the key
fails = []

FAKE_SOPS = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
a = sys.argv[1:]
secrets = ("TESTKEYMATERIAL", "WHSECRET", "CLSECRET")
def violate(what):
    open(os.environ["FAKE_SOPS_LOG"], "a").write(what + "\\n")
if any(s in x for x in a for s in secrets) or any(s in v for v in os.environ.values() for s in secrets):
    violate("secret in argv/env")
open(os.environ["FAKE_SOPS_CALLS"], "a").write(json.dumps(a) + "\n")
if a[:2] == ["set", "--value-stdin"]:
    f, path = a[2], a[3]
    value = sys.stdin.read()
    if not os.path.isfile(f):
        sys.exit(1)
    if os.environ.get("FAKE_SOPS_FAIL_SET") and path.endswith('["%s"]' % os.environ["FAKE_SOPS_FAIL_SET"]):
        # A hostile stderr: the tool must not pass a value through.
        sys.stderr.write("fake sops: cannot set, value was " + value + "\n")
        sys.exit(1)
    d = json.load(open(f))
    d[path] = json.loads(value)
    json.dump(d, open(f, "w"))
elif a[:1] == ["encrypt"]:
    # sops encrypt --filename-override PATH --input-type json --output-type yaml /dev/stdin
    plain = sys.stdin.read()
    if len(a) != 8 or a[1] != "--filename-override" or a[3:] != ["--input-type", "json", "--output-type", "yaml", "/dev/stdin"]:
        sys.exit(9)
    if os.environ.get("FAKE_SOPS_FAIL_ENCRYPT"):
        # Hostile again: the plaintext on stderr.
        sys.stderr.write("fake sops: error loading config: no matching creation rules found; input was " + plain + "\n")
        sys.exit(1)
    try:
        doc = json.loads(plain)
    except ValueError:
        sys.exit(2)
    # Recognisable, derived from the plaintext, holding none of its values.
    sys.stdout.write("FAKE-SOPS-CIPHERTEXT\nkeys: %s\nsha256: %s\nsops:\n    fake: true\n"
                     % (json.dumps(sorted(doc)), hashlib.sha256(plain.encode()).hexdigest()))
elif a[:2] == ["decrypt", "--extract"]:
    sys.stdout.write(json.load(open(a[3]))[a[2]])
elif a[:1] == ["decrypt"] and len(a) == 2:
    # Like real sops: 128 when no key opens the file.
    if os.environ.get("FAKE_SOPS_FAIL_DECRYPT"):
        sys.stderr.write("fake sops: Failed to get the data key required to decrypt the SOPS file.\n")
        sys.exit(128)
    sys.stdout.write(open(a[1]).read())
else:
    sys.exit(9)
'''
FAKE_OPENSSL = r'''#!/usr/bin/env python3
import sys
if sys.argv[1:4] != ["dgst", "-sha256", "-sign"] or sys.argv[4] != "/dev/stdin":
    sys.exit(9)
if sys.stdin.read() != @PEM@:
    sys.exit(1)
sys.stdout.buffer.write(b"SIG")
'''


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"FAIL: {msg}", file=sys.stderr)


class Api(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Api.seen.append(self.path)
        auth = self.headers.get("Authorization", "")
        ok = False
        if self.path.startswith("/app/installations") and auth.startswith("Bearer "):
            parts = auth[7:].split(".")
            pad = lambda x: x + "=" * (-len(x) % 4)
            try:
                head = json.loads(base64.urlsafe_b64decode(pad(parts[0])))
                pay = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
                ok = (len(parts) == 3 and head["alg"] == "RS256" and pay["iss"] == "4242"
                      and pay["exp"] - pay["iat"] <= 600 and base64.urlsafe_b64decode(pad(parts[2])) == b"SIG")
            except Exception:
                ok = False
        if not ok:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = self.path.split("page=")[-1]
        data = [{"id": 1, "account": {"login": "other"}}, {"id": 777, "account": {"login": "Acme"}}] if page == "1" else []
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Api.seen.append(self.path)
        m = re.fullmatch(r"/app-manifests/([^/]+)/conversions", self.path)
        good = json.dumps({"id": 4242, "slug": "fleetkit-test", "pem": PEM, "client_id": "Iv1.cid",
                           "html_url": "https://github.com/apps/fleetkit-test",
                           "webhook_secret": "WHSECRET", "client_secret": "CLSECRET"}).encode()  # GOOD in main()
        if m and m.group(1) == "goodcode":
            body = good
            self.send_response(201)
        elif m and m.group(1) == "nopem":
            body = json.dumps({"id": 4242, "slug": "fleetkit-test", "client_secret": "CLSECRET"}).encode()
            self.send_response(201)
        elif m and m.group(1) == "stall":
            # Headers and part of the body, then nothing: the tool times out mid-response.
            self.send_response(201)
            self.send_header("Content-Length", str(len(good)))
            self.end_headers()
            self.wfile.write(good[:-10])
            self.wfile.flush()
            time.sleep(4)
            return
        elif m and m.group(1) == "silent":
            time.sleep(4)  # no response at all within the tool's timeout
            return
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(tmp, manifest, sops_file, code="goodcode", bad_state=False, api_port=0, preexisting="{}", who=("--org", "acme"),
        prefix="github.app", extra=(), env=None):
    mf = os.path.join(tmp, "m.json")
    with open(mf, "w") as f:
        json.dump(manifest, f)
    if preexisting is not None:
        with open(sops_file, "w") as f:
            f.write(preexisting)
    p = subprocess.Popen(
        [sys.executable, TOOL, *who, "--manifest", mf, "--sops-file", sops_file, "--key-prefix", prefix,
         "--sops", os.path.join(tmp, "sops"), "--api", f"http://127.0.0.1:{api_port}",
         "--no-browser", "--timeout", "20", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ, **(env or {})))
    first = ""
    while not (found := re.search(r"Open (\S+) ", first)):  # a preflight warning may come first
        line = p.stderr.readline()
        if not line:
            raise RuntimeError(f"tool exited before serving the form: {first}")
        first += line
    url = found.group(1)
    page = urllib.request.urlopen(url).read().decode()
    form_action = html.unescape(re.search(r'action="([^"]+)"', page).group(1))
    posted = json.loads(html.unescape(re.search(r'name=manifest value="([^"]*)"', page).group(1)))
    state = re.search(r"state=([^&]+)$", form_action).group(1)
    if who[0] == "--org":
        check(form_action.startswith("https://github.com/organizations/acme/settings/apps/new?state="),
              "form targets the org registration URL")
    else:
        check(form_action.startswith("https://github.com/settings/apps/new?state="),
              "form targets the personal registration URL")
    check(posted == dict(manifest, redirect_url=url + "callback"), "posted manifest equals input plus redirect_url")
    check(posted["redirect_url"] == url + "callback", "redirect_url points at the local callback")
    cb = url + f"callback?code={code}&state={'wrong' if bad_state else state}"
    try:
        urllib.request.urlopen(cb)
        if bad_state:
            check(False, "wrong state must be rejected")
    except urllib.error.HTTPError as e:
        check(bad_state and e.code == 400, "wrong state gives 400")
    if bad_state:
        urllib.request.urlopen(url + f"callback?code={code}&state={state}")
    out, err = p.communicate(timeout=30)
    return p.returncode, first + err, out


def main():
    manifest = {"name": "fleetkit-acme", "url": "https://example.com",
                "default_permissions": {"contents": "read", "administration": "write"},
                "default_events": [], "public": False}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    with tempfile.TemporaryDirectory() as tmp:
        sops = os.path.join(tmp, "sops")
        with open(sops, "w") as f:
            f.write(FAKE_SOPS)
        os.chmod(sops, stat.S_IRWXU)
        openssl = os.path.join(tmp, "openssl")
        with open(openssl, "w") as f:
            f.write(FAKE_OPENSSL.replace("@PEM@", repr(PEM)))
        os.chmod(openssl, stat.S_IRWXU)
        os.environ["FAKE_SOPS_LOG"] = os.path.join(tmp, "violations")
        os.environ["FAKE_SOPS_CALLS"] = os.path.join(tmp, "calls")
        # Nothing may land in the system temp directory.
        os.environ["TMPDIR"] = os.path.join(tmp, "systmp")
        os.mkdir(os.environ["TMPDIR"])
        SECRETS = ("TESTKEYMATERIAL", "WHSECRET", "CLSECRET")
        GOOD = {"id": 4242, "slug": "fleetkit-test", "pem": PEM, "client_id": "Iv1.cid",
                "html_url": "https://github.com/apps/fleetkit-test",
                "webhook_secret": "WHSECRET", "client_secret": "CLSECRET"}

        def rescue_files(d):
            return sorted(os.path.join(d, n) for n in os.listdir(d) if ".rescue-" in n)

        def tree(d=tmp):
            logs = {os.environ["FAKE_SOPS_CALLS"], os.environ["FAKE_SOPS_LOG"]}
            return {os.path.join(r, n) for r, _, names in os.walk(d) for n in names} - logs

        def secret_files(paths):
            """Those of `paths` that hold a stand-in secret (or a pem line)."""
            hit = set()
            for p in paths:
                with open(p, "rb") as f:
                    data = f.read()
                if any(x.encode() in data for x in SECRETS + ("FAKE-PEM",)):
                    hit.add(p)
            return hit

        def check_rescue(path, sops_file, doc, err, label):
            """`path` is a fake-encrypted rescue of `doc` next to `sops_file`."""
            want_name = re.escape(os.path.splitext(os.path.basename(sops_file))[0]) + r"\.rescue-\d{8}T\d{6}(-[0-9a-f]{6})?\.yaml"
            check(os.path.dirname(path) == os.path.dirname(sops_file) and re.fullmatch(want_name, os.path.basename(path)),
                  f"{label}: rescue file is next to the sops file and named after it: {path}")
            check(stat.S_IMODE(os.stat(path).st_mode) == 0o600, f"{label}: rescue file is mode 0600")
            content = open(path).read()
            check(content.startswith("FAKE-SOPS-CIPHERTEXT\n"), f"{label}: rescue file is what sops encrypt printed")
            check(f"sha256: {hashlib.sha256(json.dumps(doc).encode()).hexdigest()}\n" in content,
                  f"{label}: sops encrypt was given the exchange response on stdin")
            check(not secret_files([path]), f"{label}: rescue file holds no secret value")
            enc = [c for c in sops_calls() if c[:1] == ["encrypt"] and "preflight" not in c[2]]
            check(enc == [["encrypt", "--filename-override", path, "--input-type", "json", "--output-type", "yaml", "/dev/stdin"]],
                  f"{label}: one sops encrypt, --filename-override is the path written: {enc}")
            check(path in err and "sops-encrypted" in err and f"Keys in it: {', '.join(sorted(doc))}" in err,
                  f"{label}: rescue path and its key names reported: {err}")

        def sops_calls():
            with open(os.environ["FAKE_SOPS_CALLS"]) as f:
                return [json.loads(line) for line in f]

        def conversions():
            return [p for p in Api.seen if p.startswith("/app-manifests/")]

        def stored(path):
            return json.load(open(path))

        # existing file, all six keys under the prefix, other content kept
        sf = os.path.join(tmp, "new.json")
        rc, err, out = run(tmp, manifest, sf, api_port=port, preexisting='{"other": "keep"}')
        check(rc == 0, f"run exits 0 (got {rc}): {err}")
        d = stored(sf)
        want = {"app_id": "4242", "app_slug": "fleetkit-test", "app_pem": PEM, "client_id": "Iv1.cid",
                "client_secret": "CLSECRET", "webhook_secret": "WHSECRET"}
        for k, v in want.items():
            check(d.get(f'["github"]["app"]["{k}"]') == v, f"{k} stored under prefix")
        check(d.get("other") == "keep", "other content kept")
        for leak in ("TESTKEYMATERIAL", "WHSECRET", "CLSECRET"):
            check(leak not in out and leak not in err, f"{leak} not printed")
        for shown in ("fleetkit-test", "4242", "https://github.com/apps/fleetkit-test",
                      "https://github.com/apps/fleetkit-test/installations/new"):
            check(shown in out, f"{shown} printed")
        check(not os.path.exists(os.environ["FAKE_SOPS_LOG"]), "secrets never in sops argv or environment")

        # slashed prefix, --user
        sfu = os.path.join(tmp, "user.json")
        rc, err, _ = run(tmp, manifest, sfu, api_port=port, who=("--user", "acme"), prefix="a/b")
        check(rc == 0 and stored(sfu).get('["a"]["b"]["app_id"]') == "4242", f"--user with slashed prefix: {err}")

        # missing sops file is refused
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", os.path.join(tmp, "m.json"),
                            "--sops-file", os.path.join(tmp, "nope.yaml"), "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True, timeout=60)
        check(r.returncode != 0 and "does not exist" in r.stderr, "missing sops file refused")

        # (a) preflight: a file the caller's key cannot open stops the tool
        # before any form is served and before anything is exchanged.
        sfp = os.path.join(tmp, "locked.json")
        with open(sfp, "w") as f:
            f.write('{"other": "keep"}')
        os.remove(os.environ["FAKE_SOPS_CALLS"])
        before = len(conversions())
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", os.path.join(tmp, "m.json"),
                            "--sops-file", sfp, "--key-prefix", "github.app", "--sops", sops, "--no-browser",
                            "--api", f"http://127.0.0.1:{port}", "--timeout", "5"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, FAKE_SOPS_FAIL_DECRYPT="1"))
        check(r.returncode != 0, "preflight failure exits non-zero")
        check("sops decrypt" in r.stderr and "128" in r.stderr and "Nothing was sent to GitHub" in r.stderr,
              f"preflight failure says why: {r.stderr}")
        check("Open http" not in r.stderr and "127.0.0.1:" not in r.stderr.replace(f"127.0.0.1:{port}", ""),
              "preflight failure serves no form")
        check(len(conversions()) == before, "preflight failure never POSTs to /app-manifests")
        check(sops_calls() == [["decrypt", sfp]], "preflight is one sops decrypt of the file, and no set")
        check(open(sfp).read() == '{"other": "keep"}', "preflight failure leaves the file alone")
        # The preflight comes before the listener in a good run too.
        os.remove(os.environ["FAKE_SOPS_CALLS"])
        rc, err, _ = run(tmp, manifest, sfp, api_port=port, preexisting='{"other": "keep"}')
        calls = sops_calls()
        check(rc == 0 and calls[0] == ["decrypt", sfp] and calls[1][:2] == ["encrypt", "--filename-override"]
              and "locked.rescue-preflight.yaml" in calls[1][2] and [c[0] for c in calls[2:]] == ["set"] * 6,
              f"good run: decrypt, a dummy encrypt for the rescue path, then six sets: {err}")
        check("warning" not in err and not rescue_files(tmp), "good run: no warning, no rescue file")
        # the plaintext rescue directory is gone, as an option too
        before = len(conversions())
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", os.path.join(tmp, "m.json"),
                            "--sops-file", sfp, "--key-prefix", "x", "--sops", sops, "--no-browser",
                            "--rescue-dir", tmp], capture_output=True, text=True, timeout=30)
        check(r.returncode != 0 and "unrecognized arguments" in r.stderr and len(conversions()) == before,
              "--rescue-dir is not an option any more")
        # (c) no code path writes the response in plaintext: the removed names stay removed
        src = open(TOOL).read()
        for gone in ("write_rescue", "die_rescued", "gettempdir", "rescue-dir", "rescue_dir", ".decrypted"):
            check(gone not in src, f"tool source no longer mentions {gone}")
        check(len(re.findall(r"\bos\.open\(|(?<![.\w])open\(", src)) == 2 and src.count("NamedTemporaryFile") == 1,
              "tool opens only the manifest, the rescue ciphertext and the JWT signing input")

        # (a) one set fails after the exchange: every other key is still
        # attempted and stored, the response is kept ENCRYPTED, nothing leaks.
        pdir = os.path.join(tmp, "partial")
        os.mkdir(pdir)
        sfb = os.path.join(pdir, "github.json")
        os.remove(os.environ["FAKE_SOPS_CALLS"])
        files_before = tree()
        rc, err, out = run(tmp, manifest, sfb, api_port=port, env={"FAKE_SOPS_FAIL_SET": "app_pem"})
        check(rc != 0, "a failed set exits non-zero")
        sets = [c[3] for c in sops_calls() if c[:2] == ["set", "--value-stdin"]]
        check(sets == [f'["github"]["app"]["{k}"]' for k in want], f"every key attempted despite a failure: {sets}")
        d = stored(sfb)
        for k, v in want.items():
            if k == "app_pem":
                check(f'["github"]["app"]["{k}"]' not in d, "failed key not stored")
            else:
                check(d.get(f'["github"]["app"]["{k}"]') == v, f"{k} stored although app_pem failed")
        found = rescue_files(pdir)
        new = tree() - files_before
        check(len(found) == 1 and new == {sfb, *found}, f"exactly one new file besides the sops file: {sorted(new)}")
        # The fake sops file is plain JSON, so sfb holds what was stored; no other new file may hold a secret.
        check(secret_files(new) <= {sfb}, f"no new file holds a secret value: {sorted(secret_files(new) - {sfb})}")
        check(not os.listdir(os.environ["TMPDIR"]), "nothing written to the temp directory")
        if found:
            check_rescue(found[0], sfb, GOOD, err, "failed set")
            check(f"decrypt --extract '[\"pem\"]' {found[0]} | jq -Rs . | " in err
                  and f"set --value-stdin {sfb} '[\"github\"][\"app\"][\"app_pem\"]'" in err,
                  f"import pipeline for the missing key printed: {err}")
            check(err.count("decrypt --extract") == 1, "import pipeline only for the key that is missing")
        for leak in SECRETS:
            check(leak not in out and leak not in err, f"{leak} not printed when a set fails")
        check("FAKE-PEM" not in out + err and "PLAINTEXT" not in err and "shred" not in err,
              "no pem line and no plaintext-file instructions printed when a set fails")
        check(re.search(r"stored under github\.app in \S+: app_id, app_slug, client_id, client_secret, webhook_secret", err) is not None
              and re.search(r"NOT stored:\s+app_pem:", err) is not None, f"stored and missing key names reported: {err}")
        check("install:" not in out, "no success summary when a set fails")

        # app_id missing: its pipeline turns the number into the stored string
        idir = os.path.join(tmp, "idfail")
        os.mkdir(idir)
        rc, err, out = run(tmp, manifest, os.path.join(idir, "s.json"), api_port=port, env={"FAKE_SOPS_FAIL_SET": "app_id"})
        check(rc != 0 and len(rescue_files(idir)) == 1 and "decrypt --extract '[\"id\"]' " in err and "| jq tostring | " in err,
              f"import pipeline for app_id: {err}")

        # (b) the set fails and sops encrypt fails too: nothing is written,
        # the loss is stated with the App's html_url, nothing leaks.
        ldir = os.path.join(tmp, "lost")
        os.mkdir(ldir)
        sfl = os.path.join(ldir, "s.json")
        files_before = tree()
        rc, err, out = run(tmp, manifest, sfl, api_port=port,
                           env={"FAKE_SOPS_FAIL_SET": "client_secret", "FAKE_SOPS_FAIL_ENCRYPT": "1"})
        new = tree() - files_before
        check(rc != 0, "failed set and failed encrypt exit non-zero")
        check(new == {sfl} and not rescue_files(ldir), f"no rescue file when sops encrypt fails: {sorted(new)}")
        check("client_secret" not in stored(sfl).get("x", "") and '["github"]["app"]["client_secret"]' not in stored(sfl)
              and stored(sfl).get('["github"]["app"]["app_pem"]') == PEM, "the other keys are still stored")
        check("LOST" in err and "Delete it: https://github.com/apps/fleetkit-test" in err and "no installation" in err
              and "run this again" in err, f"lost credentials stated with the App's html_url: {err}")
        check(re.search(r"NOT stored:\s+client_secret:", err) is not None and "sops-encrypted" not in err,
              f"lost run names the missing key and claims no rescue: {err}")
        check("warning: sops cannot encrypt" in err and "nothing was sent to GitHub yet" in err,
              f"preflight warned that the rescue would not work: {err}")
        for leak in SECRETS + ("FAKE-PEM",):
            check(leak not in out and leak not in err, f"{leak} not printed when the rescue fails too")
        check("Traceback" not in err and "install:" not in out, "lost run: no traceback, no success summary")

        # response without a pem: encrypted next to the sops file, nothing stored
        ddir = os.path.join(tmp, "default-rescue")
        os.mkdir(ddir)
        sfn = os.path.join(ddir, "s.json")
        os.remove(os.environ["FAKE_SOPS_CALLS"])
        rc, err, out = run(tmp, manifest, sfn, code="nopem", api_port=port)
        found = rescue_files(ddir)
        check(rc != 0 and len(found) == 1 and open(sfn).read() == "{}",
              f"unusable response kept encrypted in the sops file's directory, nothing stored: {err}")
        if found:
            check_rescue(found[0], sfn, {"id": 4242, "slug": "fleetkit-test", "client_secret": "CLSECRET"}, err, "no pem")
        check("CLSECRET" not in out + err and not secret_files(tree(ddir)), "unusable response neither printed nor on disk")

        # the response stalls part way: what arrived is encrypted, not printed
        tdir = os.path.join(tmp, "stall")
        os.mkdir(tdir)
        sft = os.path.join(tdir, "s.json")
        os.remove(os.environ["FAKE_SOPS_CALLS"])
        rc, err, out = run(tmp, manifest, sft, code="stall", api_port=port, extra=("--http-timeout", "1"))
        found = rescue_files(tdir)
        check(rc != 0 and len(found) == 1, f"timeout mid-response goes to an encrypted rescue file: {err}")
        if found:
            check_rescue(found[0], sft, {"raw_response": json.dumps(GOOD)[:-10]}, err, "stall")
        check(not secret_files(tree(tdir)), "partial response is nowhere on disk in plaintext")
        for leak in SECRETS:
            check(leak not in out + err, f"{leak} not printed on a mid-response timeout")
        # ... and if it cannot be encrypted it is lost, with the page to look at
        rc, err, out = run(tmp, manifest, sft, code="stall", api_port=port, extra=("--http-timeout", "1"),
                           env={"FAKE_SOPS_FAIL_ENCRYPT": "1"})
        check(rc != 0 and len(rescue_files(tdir)) == 1 and "LOST" in err
              and "https://github.com/organizations/acme/settings/apps" in err and not secret_files(tree(tdir)),
              f"partial response that cannot be encrypted is lost, not written: {err}")
        for leak in SECRETS + ("FAKE-PEM",):
            check(leak not in out + err, f"{leak} not printed when a partial response is lost")

        # no response at all: no rescue file, and the tool says the code's fate is unknown
        qdir = os.path.join(tmp, "silent")
        os.mkdir(qdir)
        rc, err, out = run(tmp, manifest, os.path.join(qdir, "s.json"), code="silent", api_port=port,
                           extra=("--http-timeout", "1"))
        check(rc != 0 and "may or may not have been spent" in err and "Traceback" not in err and not rescue_files(qdir),
              f"timeout before any response is reported plainly: {err}")

        # the API is unreachable: the tool's own error, no traceback
        rc, err, out = run(tmp, manifest, os.path.join(qdir, "s.json"), api_port=1)
        check(rc != 0 and "code exchange failed" in err and "Traceback" not in err, f"unreachable API: {err}")
        check(not os.listdir(os.environ["TMPDIR"]), "nothing strays into the temp directory")

        # record installation
        env = dict(os.environ)
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--sops-file", sf, "--key-prefix", "github.app",
                            "--record-installation", "--sops", sops, "--openssl", openssl,
                            "--api", f"http://127.0.0.1:{port}"], capture_output=True, text=True, env=env)
        check(r.returncode == 0, f"record-installation exits 0: {r.stderr}")
        check(stored(sf).get('["github"]["app"]["installation_id"]') == "777", "installation_id of the org stored")
        check("TESTKEYMATERIAL" not in r.stdout + r.stderr, "record-installation prints no key")
        r = subprocess.run([sys.executable, TOOL, "--org", "nobody", "--sops-file", sf, "--key-prefix", "github.app",
                            "--record-installation", "--sops", sops, "--openssl", openssl,
                            "--api", f"http://127.0.0.1:{port}"], capture_output=True, text=True, env=env)
        check(r.returncode != 0 and "not installed" in r.stderr, "no installation for the org fails")
        base = [sys.executable, TOOL, "--org", "acme", "--sops-file", sf, "--key-prefix", "github.app",
                "--record-installation", "--sops", sops]
        r = subprocess.run(base + ["--openssl", os.path.join(tmp, "no-openssl"), "--api", f"http://127.0.0.1:{port}"],
                           capture_output=True, text=True, env=env)
        check(r.returncode != 0 and "not found" in r.stderr and "Traceback" not in r.stderr, "missing openssl refused")
        r = subprocess.run(base + ["--openssl", openssl, "--api", "http://127.0.0.1:1"],
                           capture_output=True, text=True, env=env)
        check(r.returncode != 0 and "listing installations failed" in r.stderr and "Traceback" not in r.stderr,
              f"record-installation with the API unreachable: {r.stderr}")
        r = subprocess.run(base + ["--openssl", openssl, "--api", f"http://127.0.0.1:{port}"],
                           capture_output=True, text=True, env=dict(env, FAKE_SOPS_FAIL_SET="installation_id"))
        check(r.returncode != 0 and "not stored" in r.stderr and "Traceback" not in r.stderr,
              "record-installation reports a failed set")
        notexec = os.path.join(tmp, "notexec")
        with open(notexec, "w") as f:
            f.write("not a program")
        os.chmod(notexec, stat.S_IRWXU)
        r = subprocess.run(base + ["--openssl", notexec, "--api", f"http://127.0.0.1:{port}"],
                           capture_output=True, text=True, env=env)
        check(r.returncode != 0 and "Traceback" not in r.stderr and "TESTKEYMATERIAL" not in r.stderr,
              f"openssl that cannot be executed: {r.stderr}")

        # state mismatch is rejected but the real callback still works
        sf4 = os.path.join(tmp, "s.yaml")
        rc, err, _ = run(tmp, manifest, sf4, bad_state=True, api_port=port)
        check(rc == 0, f"state mismatch then good callback: {err}")

        # bad code: nonzero, nothing written
        sf5 = os.path.join(tmp, "b.yaml")
        rc, err, _ = run(tmp, manifest, sf5, code="expired", api_port=port)
        check(rc != 0 and open(sf5).read() == "{}", "bad code fails and writes nothing")

        # argument validation
        mf = os.path.join(tmp, "bad.json")
        json.dump({"name": "x", "url": "u", "default_permissions": {"contents": "owner"}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf4, "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid level" in r.stderr, "bad permission level refused")
        json.dump({"name": "x", "url": "u", "default_permissions": {}, "hook_attributes": {"url": "u", "active": True}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf5, "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0, "manifest with empty permissions refused")
        r = subprocess.run([sys.executable, TOOL, "--org", "-bad org", "--manifest", mf, "--sops-file", sf5, "--key-prefix", "x"],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid account name" in r.stderr, "bad org refused")
    srv.shutdown()
    status = "fail" if fails else "pass"
    print(json.dumps({"github_app_bootstrap": status}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
