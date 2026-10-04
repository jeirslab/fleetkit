import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ap = argparse.ArgumentParser(description="Warm the model cold store")
ap.add_argument("repo")
ap.add_argument("--include", action="append", default=[], help="glob on the file path (repeatable); default: everything")
ap.add_argument("--revision", default="main")
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--rate-mib", type=float, default=0, help="cap average throughput in MiB/s (0 = unlimited); use it on a shared link")
a = ap.parse_args()

mirror = os.environ.get("LLM_COLD_URL", "http://127.0.0.1:@port@")
hf = os.environ.get("LLM_PREFETCH_HF_URL", "https://huggingface.co")
api = f"{hf}/api/models/{a.repo}/tree/{a.revision}?recursive=true"
files = []
while api:
    req = urllib.request.Request(api)
    with urllib.request.urlopen(req, timeout=60) as r:
        files += [f for f in json.load(r) if f["type"] == "file"]
        link = r.headers.get("Link", "")
    api = None
    for part in link.split(","):
        if 'rel="next"' in part:
            api = part[part.index("<") + 1:part.index(">")]


def wanted(p):
    return not a.include or any(fnmatch.fnmatch(p, g) for g in a.include)


todo = [f for f in files if wanted(f["path"])]
total = sum((f.get("lfs") or {}).get("size", f.get("size", 0)) for f in todo)
print(f"{a.repo}@{a.revision}: {len(todo)} files, {total / 2**30:.1f} GiB")
if a.dry_run:
    for f in todo:
        print("  ", f["path"], (f.get("lfs") or {}).get("size", f.get("size", 0)))
    sys.exit(0)

done = 0
t0 = time.time()
for f in todo:
    size = (f.get("lfs") or {}).get("size", f.get("size", 0))
    url = f"{mirror}/{a.repo}/resolve/{a.revision}/{urllib.parse.quote(f['path'])}"
    for attempt in range(1, 6):
        try:
            got = 0
            t_file = time.time()
            with urllib.request.urlopen(url, timeout=120) as r:
                while True:
                    b = r.read(8 * 1024 * 1024)
                    if not b:
                        break
                    got += len(b)
                    if a.rate_mib:
                        ahead = got / (a.rate_mib * 2**20) - (time.time() - t_file)
                        if ahead > 0:
                            time.sleep(ahead)
            if got != size:
                raise OSError(f"short read: {got} of {size} bytes")
            break
        except Exception as e:  # retries resume cheaply: Olah keeps the chunks it already has
            print(f"  retry {attempt}/5 {f['path']}: {e}", file=sys.stderr)
            time.sleep(5 * attempt)
    else:
        sys.exit(f"giving up on {f['path']}")
    done += size
    print(f"  ok {f['path']} {size / 2**30:.2f} GiB  ({done / 2**30:.1f}/{total / 2**30:.1f} GiB, {done / (time.time() - t0) / 2**20:.0f} MiB/s avg)", flush=True)
