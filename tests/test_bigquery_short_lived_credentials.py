"""Explicit keyless profiles expire and never trigger ambient authentication."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from knowledge_layer.connectors.bigquery import _access_token
from knowledge_layer.connectors.cloud_metadata import MetadataBudget
from knowledge_layer.connectors.common import ConnectorError


def profile(seconds=120):
    return {"type": "short_lived_access_token", "access_token": "synthetic-token-value",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()}


def test_short_lived_explicit_profile_needs_no_oauth_exchange():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no authentication HTTP"))) as client:
        assert _access_token(profile(), MetadataBudget(), client) == "synthetic-token-value"


@pytest.mark.parametrize("seconds", [-60, 0, 30, 3605])
def test_short_lived_expired_and_excessive_lifetimes_refuse(seconds):
    with httpx.Client() as client, pytest.raises(ConnectorError):
        _access_token(profile(seconds), MetadataBudget(), client)


@pytest.mark.parametrize("change", [{"expires_at": "2026-10-05T12:00:00"}, {"access_token": "bad\nheader"},
                                    {"expires_at": None}, {"refresh_token": "unsupported"}])
def test_short_lived_malformed_profiles_refuse(change):
    value = profile()
    value.update(change)
    with httpx.Client() as client, pytest.raises(ConnectorError):
        _access_token(value, MetadataBudget(), client)
