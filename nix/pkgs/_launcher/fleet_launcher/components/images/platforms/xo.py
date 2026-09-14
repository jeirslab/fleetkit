"""Xen Orchestra registration: raw disk → VDI → staging VM → template.

Two API surfaces, on purpose (same split as fleetkit's xoa-cli):
  * REST  (/rest/v0, token cookie) — reads and the VDI upload;
  * JSON-RPC over the websocket (/api/) — every mutation (vm.create,
    vm.attachDisk, vm.set, vm.convertToTemplate), which is what upstream
    xo-cli drives and is known-good; the REST PATCH surface is not.

Credentials: XO_URL + XO_TOKEN (or XOA_URL / XOA_TOKEN) from the
environment, never a file. XO_INSECURE=1 skips TLS verification for a
self-signed XO.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

from ..nix import DeployerError

GIB = 1 << 30
BASE_TEMPLATE = "Other install media"


def credentials() -> tuple[str, str, bool]:
    url = os.environ.get("XO_URL") or os.environ.get("XOA_URL")
    token = os.environ.get("XO_TOKEN") or os.environ.get("XOA_TOKEN")
    if not url or not token:
        raise DeployerError("set XO_URL and XO_TOKEN (e.g. `infisical run -- deployer …`)")
    insecure = os.environ.get("XO_INSECURE", os.environ.get("XOA_INSECURE", "")) in ("1", "true", "yes")
    return url.rstrip("/"), token, insecure


def _http_base(url: str) -> str:
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


def _ws_url(url: str) -> str:
    base = _http_base(url)
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/"
    return "ws://" + base[len("http://"):] + "/api/"


class XoRest:
    def __init__(self, url: str, token: str, insecure: bool):
        self.base = _http_base(url)
        self.token = token
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str, fields: list[str] | None = None) -> object:
        query = "?" + urllib.parse.urlencode({"fields": ",".join(fields)}) if fields else ""
        req = urllib.request.Request(f"{self.base}/rest/v0/{path}{query}",
                                     headers={"Cookie": f"authenticationToken={self.token}"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            raise DeployerError(f"XO GET {path} → HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DeployerError(f"XO GET {path} failed: {exc}") from exc

    def uuid_by_label(self, collection: str, label: str) -> str:
        objs = self.get(collection, fields=["name_label", "uuid"])
        matches = [o["uuid"] for o in objs if isinstance(o, dict) and o.get("name_label") == label]
        if len(matches) != 1:
            raise DeployerError(f"XO {collection}: {len(matches)} objects named {label!r}, need exactly 1")
        return matches[0]

    def import_raw_vdi(self, sr_uuid: str, name_label: str, path: str, timeout: int = 3600) -> str:
        """POST the raw image as a VDI; return its uuid.

        Raw, not VHD: XO sizes the VDI by Content-Length, which for a sparse
        VHD is the file size, not the virtual size — the VDI comes out
        truncated and EDK2 refuses the 'invalid' GPT. Raw makes both equal.
        """
        query = urllib.parse.urlencode({"name_label": name_label, "raw": "true"})
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            req = urllib.request.Request(
                f"{self.base}/rest/v0/srs/{sr_uuid}/vdis?{query}", data=fh, method="POST",
                headers={"Cookie": f"authenticationToken={self.token}",
                         "Content-Type": "application/octet-stream", "Content-Length": str(size)})
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", "replace").strip()
            except urllib.error.HTTPError as exc:
                raise DeployerError(f"XO VDI import → HTTP {exc.code}: {exc.read()[:300]!r}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise DeployerError(f"XO VDI import failed: {exc}") from exc
        return parse_vdi_href(body)


def parse_vdi_href(body: str) -> str:
    """'/rest/v0/vdis/<uuid>' (possibly JSON-quoted) → '<uuid>'."""
    vdi = body.strip().strip('"').rstrip("/").rsplit("/", 1)[-1]
    if not vdi:
        raise DeployerError(f"XO VDI import: unparseable response {body!r}")
    return vdi


class XoRpc:
    """Minimal JSON-RPC 2.0 over XO's websocket (session.signInWithToken, then calls)."""

    def __init__(self, url: str, token: str, insecure: bool, timeout: int = 300):
        import websocket  # deferred: only mutations need it

        sslopt = {"cert_reqs": ssl.CERT_NONE} if insecure else None
        try:
            self.ws = websocket.create_connection(_ws_url(url), sslopt=sslopt, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — transport errors of many types
            raise DeployerError(f"XO websocket connect failed: {exc}") from exc
        self._id = 0
        self.call("session.signInWithToken", {"token": token})

    def call(self, method: str, params: dict | None = None) -> object:
        self._id += 1
        rid = self._id
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue  # notification / unrelated frame
            if "error" in msg:
                err = msg["error"]
                raise DeployerError(f"XO {method}: {err.get('message', err)} (code {err.get('code')})")
            return msg.get("result")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def register(*, image: str, name: str, sr: str, network: str | None, firmware: str, dry_run: bool) -> None:
    """raw .img → VDI → staging VM (from 'Other install media') → template."""
    with open(image, "rb") as fh:
        magic = fh.read(8)
    if magic[:4] == b"QFI\xfb" or magic == b"conectix":
        raise DeployerError("expected a RAW disk (build target xen-orchestra); convert with qemu-img convert -O raw")
    size_gib = os.path.getsize(image) / GIB
    if dry_run:
        print(f"[dry-run] would upload {image} ({size_gib:.2f} GiB) to SR {sr!r} as {name}-disk, "
              f"create a {firmware} VM from {BASE_TEMPLATE!r}"
              f"{' on network ' + repr(network) if network else ''}, and convert it to template {name!r}")
        return

    url, token, insecure = credentials()
    rest = XoRest(url, token, insecure)

    templates = rest.get("vm-templates", fields=["name_label", "uuid"])
    if any(isinstance(t, dict) and t.get("name_label") == name for t in templates):
        raise DeployerError(f"template {name!r} already exists — templates are versioned, pick a new name")

    sr_uuid = rest.uuid_by_label("srs", sr)
    base = rest.uuid_by_label("vm-templates", BASE_TEMPLATE)
    net = rest.uuid_by_label("networks", network) if network else None
    pools = rest.get("pools", fields=["uuid"])
    if not pools:
        raise DeployerError("XO reports no pools")
    pool = pools[0]["uuid"]

    print(f"uploading {image} ({size_gib:.2f} GiB raw) to {sr} …")
    vdi = rest.import_raw_vdi(sr_uuid, f"{name}-disk", image)
    print(f"VDI {vdi}")

    rpc = XoRpc(url, token, insecure)
    try:
        vm = rpc.call("vm.create", {
            "name_label": f"{name}-staging",
            "template": f"{pool}-{base}",
            "bootAfterCreate": False,
            "existingDisks": {},
            "VIFs": [{"network": net}] if net else [],
        })
        rpc.call("vm.attachDisk", {"vm": vm, "vdi": vdi, "bootable": True, "position": "0", "mode": "RW"})
        rpc.call("vm.setBootOrder", {"vm": vm, "order": "c"})
        rpc.call("vm.set", {"id": vm, "name_label": name, "hvmBootFirmware": firmware})
        rpc.call("vm.convertToTemplate", {"id": vm})
    finally:
        rpc.close()
    print(f"registered: XO template {name!r} ({firmware}) — reference it as data.xenorchestra_template")
