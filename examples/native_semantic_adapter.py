"""Optional out-of-process DataHub/dbt adapter with operator-defined scopes.

Run from examples with the project's Python runtime. The mounted operator
profile selects endpoints, tokens and artifact paths; request bodies select only
an approved scope. Keep upstream tokens read-only and mount artifacts read-only.
"""

from __future__ import annotations

import hmac
import os
import stat
from datetime import datetime
from typing import ClassVar

from reference_adapter import BoundedHTTPServer, Handler

from knowledge_layer.adapters.catalog import MAX_BYTES as CATALOG_MAX_BYTES
from knowledge_layer.adapters.catalog import inventory_records as catalog_inventory
from knowledge_layer.adapters.catalog import scope_digest
from knowledge_layer.adapters.catalog_mapping import binding_digest, map_export
from knowledge_layer.adapters.datahub import discover_glossary
from knowledge_layer.adapters.datahub import inventory_records as datahub_inventory
from knowledge_layer.adapters.dbt import MAX_BYTES, MAX_INVENTORY_BYTES, import_manifest
from knowledge_layer.adapters.dbt import inventory_records as dbt_inventory
from knowledge_layer.adapters.paging import NativeInventoryPager
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import decode_response, validate_profile
from knowledge_layer.connectors.external_inventory import (
    MAX_REQUEST_BYTES,
    validate_inventory_request,
)
from knowledge_layer.inventory import inventory_digest
from knowledge_layer.inventory_refresh import inventory_profile_digest
from knowledge_layer.operator_secrets import read_owner_only_file


class NativeHandler(Handler):
    pager: ClassVar[NativeInventoryPager] = NativeInventoryPager()

    @staticmethod
    def _profile():
        raw = read_owner_only_file(os.environ["NATIVE_ADAPTER_PROFILE_FILE"], max_bytes=64 * 1024)
        profile = decode_response(raw, max_bytes=64 * 1024)
        if not isinstance(profile, dict) or not 1 <= len(profile) <= 100:
            raise ValueError
        return profile

    @staticmethod
    def _artifact(path, maximum):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if (len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError
            return raw

    @staticmethod
    def _inventory(scope, request):
        if scope.get("format") == "catalog_export":
            expected = scope["expected_scope"]
            mapping = scope.get("mapping")
            platform = scope["platform_profile"]
            validate_profile(platform, scope=request["scope"])
            if (expected["provider"] != request["provider"] or expected["scope_id"] != request["scope"]
                    or platform.get("protocol_version") != "1.3"
                    or platform.get("provider") != request["provider"]
                    or platform.get("inventory_scope_bindings", {}).get(request["scope"]) != binding_digest(expected, mapping)
                    or inventory_profile_digest(platform) != request["profile_digest"]):
                raise ConnectorError("catalog_scope_binding_changed", "The operator catalog scope binding is unavailable")
            raw = NativeHandler._artifact(scope["artifact"], CATALOG_MAX_BYTES)
            if mapping is not None:
                contract = scope["export_contract"]
                if scope_digest(contract["scope"]) != scope_digest(expected):
                    raise ConnectorError("catalog_scope_binding_changed", "The operator catalog scope binding is unavailable")
                raw = map_export(raw, contract=contract, mapping=mapping)
            return catalog_inventory(raw, expected_scope=expected)
        if scope["provider"] == "dbt":
            raw = NativeHandler._artifact(scope["manifest"], MAX_INVENTORY_BYTES)
            return dbt_inventory(raw, namespace=scope["namespace"], unique_ids=scope["unique_ids"])
        if scope["provider"] == "datahub":
            return datahub_inventory(endpoint=scope["endpoint"], bearer_token=scope["read_token"],
                namespace=scope["namespace"], urns=scope["urns"],
                deadline=datetime.fromisoformat(request["scan_expires_at"]))
        raise ValueError

    def do_POST(self):
        token = os.environ.get("ADAPTER_BEARER_TOKEN", "")
        if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path != "/discover":
            self._reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (not 1 <= length <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise ValueError
            body = decode_response(self.rfile.read(length), max_bytes=MAX_REQUEST_BYTES)
            if (not isinstance(body, dict) or body.get("protocol_version") not in {"1.2", "1.3"}
                    or not isinstance(body.get("scope"), str)):
                raise ValueError
            profile = self._profile()
            scope = profile.get(body["scope"])
            if not isinstance(scope, dict) or body.get("provider") != scope.get("provider"):
                self._reply(403, {"error": "scope_denied"})
                return
            if body["protocol_version"] == "1.3":
                request = validate_inventory_request(body)
                result = self.pager.page(request, operator_digest=inventory_digest(profile),
                                         load_records=lambda: self._inventory(scope, request))
                self._reply(200, result)
                return
            maximum = body.get("max_assets")
            if type(maximum) is not int or not 1 <= maximum <= 100:
                raise ValueError
            if scope["provider"] == "dbt":
                raw = self._artifact(scope["manifest"], MAX_BYTES)
                result = import_manifest(raw, namespace=scope["namespace"], unique_ids=scope["unique_ids"])
            elif scope["provider"] == "datahub":
                result = discover_glossary(endpoint=scope["endpoint"], bearer_token=scope["read_token"],
                                           namespace=scope["namespace"], urns=scope["urns"])
            else:
                raise ValueError
            if len(result["assets"]) > maximum:
                self._reply(413, {"error": "scope_exceeds_scan_budget"})
                return
        except ConnectorError as exc:
            self._reply(422, {"error": exc.code})
            return
        except (ValueError, KeyError, TypeError, OSError):
            self._reply(422, {"error": "operator_profile_or_request_invalid"})
            return
        self._reply(200, result)


if __name__ == "__main__":
    # Fail before listening when the protected profile is unavailable. Re-read
    # on each request so removed scopes and rotated native tokens invalidate it.
    NativeHandler._profile()
    BoundedHTTPServer(("127.0.0.1", 8061), NativeHandler).serve_forever()
