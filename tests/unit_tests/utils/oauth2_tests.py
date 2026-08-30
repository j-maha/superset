# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# pylint: disable=invalid-name, disallowed-name

import base64
import hashlib
import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator, cast
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from freezegun import freeze_time
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from superset.db_engine_specs.base import BaseEngineSpec
from superset.exceptions import OAuth2ScopeMismatchError
from superset.superset_typing import OAuth2ClientConfig, OAuth2TokenResponse
from superset.utils.oauth2 import (
    _oauth2_scopes_match,
    get_oauth2_scope_mismatch,
    decode_oauth2_state,
    encode_oauth2_state,
    generate_code_challenge,
    generate_code_verifier,
    get_oauth2_access_token,
    get_oauth2_redirect_uri,
    refresh_oauth2_token,
)


@contextmanager
def local_oauth_provider(
    code_verifier: str,
) -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Run a local OAuth provider that validates authorization-code requests."""
    requests_received: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers["Content-Length"] or 0)
            body = self.rfile.read(content_length).decode()
            if self.headers.get_content_type() == "application/json":
                request_data = json.loads(body)
            else:
                request_data = {
                    key: values[0]
                    for key, values in parse_qs(body, keep_blank_values=True).items()
                }
            requests_received.append(request_data)

            if self.path != "/token":
                self.send_error(404)
                return

            if request_data.get("grant_type") == "authorization_code":
                if (
                    request_data.get("code") != "authorization-code"
                    or request_data.get("code_verifier") != code_verifier
                ):
                    self.send_error(400, "invalid authorization code request")
                    return
                response = {
                    "access_token": "local-access-token",
                    "expires_in": 3600,
                    "refresh_token": "local-refresh-token",
                    "scope": "scope-a",
                }
            elif request_data.get("grant_type") == "refresh_token":
                if request_data.get("refresh_token") != "local-refresh-token":
                    self.send_error(400, "invalid refresh token")
                    return
                response = {
                    "access_token": "refreshed-access-token",
                    "expires_in": 3600,
                    "refresh_token": "local-refresh-token",
                    "scope": "scope-a",
                }
            else:
                self.send_error(400, "unsupported grant type")
                return

            response_body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/token", requests_received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class LocalOAuth2ProviderEngineSpec(BaseEngineSpec):
    """Engine spec used to exercise the real OAuth HTTP and persistence flow."""

    engine = "local_oauth2_provider"


DUMMY_OAUTH2_CONFIG = cast(OAuth2ClientConfig, {})


@pytest.mark.parametrize(
    ("requested", "granted", "matches"),
    [
        (None, None, True),
        ("", None, True),
        (None, "", True),
        ("scope-a scope-b", "scope-b   scope-a", True),
        ("scope-a scope-a", "scope-a", True),
        ("scope-a", "scope-a scope-extra", False),
        (
            "https://www.googleapis.com/auth/bigquery.readonly",
            "https://www.googleapis.com/auth/bigquery",
            False,
        ),
        ("Scope-A", "scope-a", False),
        (None, "scope-a", False),
    ],
)
def test_oauth2_scopes_match_exactly(
    requested: str | None, granted: str | None, matches: bool
) -> None:
    assert _oauth2_scopes_match(requested, granted) is matches


@pytest.mark.parametrize(
    ("policy", "granted", "matches"),
    [
        ("ignore", "scope-a scope-extra", True),
        ("subset", "scope-a scope-extra", True),
        ("subset", "scope-extra", False),
        ("exact", "scope-a", True),
        ("exact", "scope-a scope-extra", False),
    ],
)
def test_oauth2_scope_matching_policy(
    policy: str, granted: str, matches: bool
) -> None:
    config = cast(
        OAuth2ClientConfig,
        {
            "scope": "scope-a",
            "scope_matching_policy": policy,
        },
    )
    mismatch = get_oauth2_scope_mismatch(config, granted)
    assert (mismatch is None) is matches

    if not matches:
        assert mismatch == {
            "policy": policy,
            "required_scopes": ["scope-a"],
            "granted_scopes": granted.split(),
            "missing_scopes": [] if "scope-a" in granted.split() else ["scope-a"],
            "unexpected_scopes": (
                ["scope-extra"] if "scope-extra" in granted.split() else []
            ),
        }


class LocalOAuth2EngineSpec(BaseEngineSpec):
    @classmethod
    def get_oauth2_fresh_token(
        cls,
        config: OAuth2ClientConfig,
        refresh_token: str,
    ) -> OAuth2TokenResponse:
        assert config == DUMMY_OAUTH2_CONFIG
        assert refresh_token == "old-refresh-token"  # noqa: S105
        return {
            "access_token": "new-access-token",
            "expires_in": 3600,
            "refresh_token": "new-refresh-token",
        }


def test_get_oauth2_access_token_base_no_token(mocker: MockerFixture) -> None:
    """
    Test `get_oauth2_access_token` when there's no token.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    db.session.query().filter_by().one_or_none.return_value = None

    assert get_oauth2_access_token({}, 1, 1, db_engine_spec) is None


def test_get_oauth2_access_token_base_token_valid(mocker: MockerFixture) -> None:
    """
    Test `get_oauth2_access_token` when the token is valid.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.access_token = "access-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 2)
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-01"):
        assert get_oauth2_access_token({}, 1, 1, db_engine_spec) == "access-token"


def test_get_oauth2_access_token_base_refresh(mocker: MockerFixture) -> None:
    """
    Test `get_oauth2_access_token` when the token needs to be refreshed.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "access_token": "new-token",
        "expires_in": 3600,
    }
    token = mocker.MagicMock()
    token.access_token = "access-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 1)
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-02"):
        assert get_oauth2_access_token({}, 1, 1, db_engine_spec) == "new-token"

    # check that token was updated
    assert token.access_token == "new-token"  # noqa: S105
    assert token.access_token_expiration == datetime(2024, 1, 2, 1)
    db.session.add.assert_called_with(token)


@pytest.mark.parametrize(
    "access_token_expiration",
    [datetime(2024, 1, 1), None],
    ids=["expired", "legacy-null-expiration"],
)
def test_get_oauth2_access_token_persists_refresh(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    access_token_expiration: datetime | None,
) -> None:
    """Persist refreshed access and refresh tokens through the real ORM session."""
    from flask_appbuilder.security.sqla.models import User

    from superset.models.core import Database, DatabaseUserOAuth2Tokens

    Database.metadata.create_all(session.get_bind())  # pylint: disable=no-member

    user = User(
        first_name="Refresh",
        last_name="User",
        email="oauth-refresh@example.org",
        username="oauth-refresh",
    )
    database = Database(
        database_name="oauth_refresh_db",
        sqlalchemy_uri="sqlite://",
    )
    session.add_all([user, database])
    session.flush()
    session.add(
        DatabaseUserOAuth2Tokens(
            user_id=user.id,
            database_id=database.id,
            access_token="expired-access-token",  # noqa: S106
            access_token_expiration=access_token_expiration,
            refresh_token="old-refresh-token",  # noqa: S106
        )
    )
    session.flush()

    monkeypatch.setattr(
        "superset.utils.oauth2.DistributedLock",
        lambda **_: nullcontext(),
    )

    with freeze_time("2024-01-02"):
        result = get_oauth2_access_token(
            DUMMY_OAUTH2_CONFIG,
            database.id,
            user.id,
            LocalOAuth2EngineSpec,
        )

    session.flush()
    session.expire_all()
    token = (
        session.query(DatabaseUserOAuth2Tokens)
        .filter_by(user_id=user.id, database_id=database.id)
        .one()
    )
    assert result == "new-access-token"  # noqa: S105
    assert token.access_token == "new-access-token"  # noqa: S105
    assert token.access_token_expiration == datetime(2024, 1, 2, 1)
    assert token.refresh_token == "new-refresh-token"  # noqa: S105


def test_oauth2_callback_exchange_persists_and_refreshes_with_local_provider(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise callback exchange, PKCE cleanup, persistence, and refresh locally."""
    from flask_appbuilder.security.sqla.models import User

    from superset.commands.database.oauth2 import OAuth2StoreTokenCommand
    from superset.daos.key_value import KeyValueDAO
    from superset.key_value.models import KeyValueEntry
    from superset.key_value.types import JsonKeyValueCodec, KeyValueResource
    from superset.models.core import Database, DatabaseUserOAuth2Tokens

    Database.metadata.create_all(session.get_bind())  # pylint: disable=no-member
    KeyValueEntry.metadata.create_all(session.get_bind())  # pylint: disable=no-member

    user = User(
        first_name="Local",
        last_name="OAuth",
        email="local-oauth@example.org",
        username="local-oauth",
    )
    database = Database(
        database_name="local_oauth_db",
        sqlalchemy_uri="sqlite://",
        encrypted_extra=json.dumps(
            {
                "oauth2_client_info": {
                    "id": "client-id",
                    "secret": "client-secret",
                    "scope": "scope-a",
                    "scope_matching_policy": "exact",
                    "authorization_request_uri": "http://unused/authorize",
                    "token_request_uri": "http://unused/token",
                    "request_content_type": "data",
                    "redirect_uri": "http://superset.test/oauth2/callback",
                }
            }
        ),
    )
    session.add_all([user, database])
    session.flush()

    code_verifier = generate_code_verifier()
    tab_id = uuid4()
    state = {
        "database_id": database.id,
        "user_id": user.id,
        "default_redirect_uri": "http://superset.test/oauth2/callback",
        "tab_id": str(tab_id),
    }

    with local_oauth_provider(code_verifier) as (token_uri, requests_received):
        database.encrypted_extra = json.dumps(
            {
                "oauth2_client_info": {
                    "id": "client-id",
                    "secret": "client-secret",
                    "scope": "scope-a",
                    "scope_matching_policy": "exact",
                    "authorization_request_uri": "http://unused/authorize",
                    "token_request_uri": token_uri,
                    "request_content_type": "data",
                    "redirect_uri": "http://superset.test/oauth2/callback",
                }
            }
        )
        session.flush()
        config = database.get_oauth2_config()
        assert config is not None

        authorization_uri = LocalOAuth2ProviderEngineSpec.get_oauth2_authorization_uri(
            config,
            state,
            code_verifier=code_verifier,
        )
        authorization_query = parse_qs(urlparse(authorization_uri).query)
        assert authorization_query["code_challenge_method"] == ["S256"]
        assert authorization_query["code_challenge"] == [
            generate_code_challenge(code_verifier)
        ]

        KeyValueDAO.create_entry(
            resource=KeyValueResource.PKCE_CODE_VERIFIER,
            key=tab_id,
            value={"code_verifier": code_verifier},
            codec=JsonKeyValueCodec(),
        )
        session.flush()
        monkeypatch.setattr(
            "superset.utils.oauth2.DistributedLock",
            lambda **_: nullcontext(),
        )

        result = OAuth2StoreTokenCommand(
            {
                "code": "authorization-code",
                "state": encode_oauth2_state(state),
            }
        ).run()

        assert result.access_token == "local-access-token"  # noqa: S105
        assert result.refresh_token == "local-refresh-token"  # noqa: S105
        assert result.scope == "scope-a"
        assert (
            KeyValueDAO.get_value(
                KeyValueResource.PKCE_CODE_VERIFIER,
                tab_id,
                JsonKeyValueCodec(),
            )
            is None
        )
        assert requests_received == [
            {
                "code": "authorization-code",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "redirect_uri": "http://superset.test/oauth2/callback",
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            }
        ]

        stored_token = (
            session.query(DatabaseUserOAuth2Tokens)
            .filter_by(user_id=user.id, database_id=database.id)
            .one()
        )
        stored_token.access_token_expiration = datetime(2020, 1, 1)
        session.flush()

        assert (
            get_oauth2_access_token(config, database.id, user.id, LocalOAuth2ProviderEngineSpec)
            == "refreshed-access-token"
        )
        assert requests_received[-1] == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "local-refresh-token",
            "grant_type": "refresh_token",
        }


def test_get_oauth2_access_token_base_no_refresh(mocker: MockerFixture) -> None:
    """
    Test `get_oauth2_access_token` when token is expired and there's no refresh.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.access_token = "access-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 1)
    token.refresh_token = None
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-02"):
        assert get_oauth2_access_token({}, 1, 1, db_engine_spec) is None

    # check that token was deleted
    db.session.delete.assert_called_with(token)


def test_get_oauth2_access_token_preserves_token_with_stale_scope(
    mocker: MockerFixture,
) -> None:
    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.scope = "openid https://www.googleapis.com/auth/bigquery"
    db.session.query().filter_by().one_or_none.return_value = token

    with pytest.raises(OAuth2ScopeMismatchError) as exc_info:
        get_oauth2_access_token(
            {
                "scope": "openid https://www.googleapis.com/auth/bigquery.readonly",
                "scope_matching_policy": "exact",
            },
            1,
            1,
            db_engine_spec,
        )

    assert "scope matching policy" in str(exc_info.value)
    db.session.delete.assert_called_once_with(token)
    db.session.flush.assert_called_once_with()


def test_refresh_oauth2_token_deletes_token_on_scope_mismatch(
    mocker: MockerFixture,
) -> None:
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
        "scope": "scope-b",
    }
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "refresh-token"  # noqa: S105
    token.scope = "scope-a"
    db.session.query().filter_by().one_or_none.return_value = token

    config = cast(
        OAuth2ClientConfig,
        {"scope": "scope-a", "scope_matching_policy": "exact"},
    )
    with pytest.raises(OAuth2ScopeMismatchError):
        refresh_oauth2_token(config, 1, 1, db_engine_spec)

    db.session.delete.assert_called_once_with(token)
    db.session.flush.assert_called_once_with()
    db.session.add.assert_not_called()


def test_refresh_oauth2_token_deletes_token_on_oauth2_exception(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token deletes the token on OAuth2-specific exception.

    When the token refresh fails with an OAuth2-specific exception (e.g., token
    was revoked), the invalid token should be deleted and the exception re-raised.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")

    class OAuth2ExceptionError(Exception):
        pass

    db_engine_spec = mocker.MagicMock()
    db_engine_spec.oauth2_exception = OAuth2ExceptionError
    db_engine_spec.get_oauth2_fresh_token.side_effect = OAuth2ExceptionError(
        "Token revoked"
    )
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with pytest.raises(OAuth2ExceptionError):
        refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    db.session.delete.assert_called_with(token)
    db.session.flush.assert_called_once()


def test_refresh_oauth2_token_keeps_token_on_other_exception(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token keeps the token on non-OAuth2 exceptions.

    When the token refresh fails with a transient error (e.g., network issue),
    the token should be kept (refresh token may still be valid) and the
    exception re-raised.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")

    class OAuth2ExceptionError(Exception):
        pass

    db_engine_spec = mocker.MagicMock()
    db_engine_spec.oauth2_exception = OAuth2ExceptionError
    db_engine_spec.get_oauth2_fresh_token.side_effect = Exception("Network error")
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with pytest.raises(Exception, match="Network error"):
        refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    db.session.delete.assert_not_called()


def test_refresh_oauth2_token_no_access_token_in_response(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token returns None when no access_token in response.

    This can happen when the refresh token was revoked.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "error": "invalid_grant",
    }
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    result = refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert result is None


def test_refresh_oauth2_token_updates_refresh_token(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token updates the refresh token when a new one is returned.

    Some OAuth2 providers issue single-use refresh tokens, where each token refresh
    response includes a new refresh token that replaces the previous one.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
        "refresh_token": "new-refresh-token",
    }
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "old-refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-01"):
        refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert token.access_token == "new-access-token"  # noqa: S105
    assert token.access_token_expiration == datetime(2024, 1, 1, 1)
    assert token.refresh_token == "new-refresh-token"  # noqa: S105
    db.session.add.assert_called_with(token)


def test_refresh_oauth2_token_keeps_refresh_token(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token keeps the existing refresh token when none returned.

    When the OAuth2 provider does not issue a new refresh token in the response,
    the original refresh token should be preserved.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
    }
    token = mocker.MagicMock()
    token.access_token = None
    token.refresh_token = "original-refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-01"):
        refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert token.access_token == "new-access-token"  # noqa: S105
    assert token.refresh_token == "original-refresh-token"  # noqa: S105
    db.session.add.assert_called_with(token)


def test_refresh_oauth2_token_refreshes_when_access_token_expired_under_lock(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token triggers a refresh when the access_token is expired.

    When the re-query under the lock returns a token whose access_token has expired
    but a refresh_token is available, the function should call the token endpoint
    and persist the new access_token.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db_engine_spec.get_oauth2_fresh_token.return_value = {
        "access_token": "new-access-token",
        "expires_in": 3600,
    }
    token = mocker.MagicMock()
    token.access_token = "expired-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 1)
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-02"):
        result = refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert result == "new-access-token"
    db_engine_spec.get_oauth2_fresh_token.assert_called_once_with(
        DUMMY_OAUTH2_CONFIG, "refresh-token"
    )
    db.session.add.assert_called_with(token)


def test_refresh_oauth2_token_returns_existing_token_when_still_valid_under_lock(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token returns the existing access_token if still valid.

    When concurrent requests are triggered and the first one refreshes the token and
    releases the lock before the second one gets to `refresh_oauth2_token`, the second
    request should pick up the already-refreshed access_token instead of refreshing
    it again.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.access_token = "fresh-access-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 2)
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-01"):
        result = refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert result == "fresh-access-token"
    db_engine_spec.get_oauth2_fresh_token.assert_not_called()
    db.session.delete.assert_not_called()


def test_refresh_oauth2_token_deletes_when_no_refresh_token_under_lock(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token deletes the row when there's no refresh_token.

    When the token has expired and the re-query under the lock shows no refresh_token
    is available, the row should be deleted and None returned so the caller can
    trigger the OAuth2 dance.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.access_token = "expired-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 1)
    token.refresh_token = None
    db.session.query().filter_by().one_or_none.return_value = token

    with freeze_time("2024-01-02"):
        result = refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert result is None
    db.session.delete.assert_called_with(token)
    db_engine_spec.get_oauth2_fresh_token.assert_not_called()


def test_refresh_oauth2_token_returns_none_when_row_deleted_under_lock(
    mocker: MockerFixture,
) -> None:
    """
    Test that refresh_oauth2_token returns None when the row is gone under the lock.

    When concurrent requests are triggered and the first one deletes the token row and
    releases the lock before the second one gets to `refresh_oauth2_token`, the token
    is queried again to avoid a stale reference.
    """
    db = mocker.patch("superset.utils.oauth2.db")
    mocker.patch("superset.utils.oauth2.DistributedLock")
    db_engine_spec = mocker.MagicMock()
    db.session.query().filter_by().one_or_none.return_value = None

    result = refresh_oauth2_token(DUMMY_OAUTH2_CONFIG, 1, 1, db_engine_spec)

    assert result is None
    db_engine_spec.get_oauth2_fresh_token.assert_not_called()


def test_generate_code_verifier_length() -> None:
    """
    Test that generate_code_verifier produces a string of valid length (RFC 7636).
    """
    code_verifier = generate_code_verifier()
    # RFC 7636 requires 43-128 characters
    assert 43 <= len(code_verifier) <= 128


def test_generate_code_verifier_uniqueness() -> None:
    """
    Test that generate_code_verifier produces unique values.
    """
    verifiers = {generate_code_verifier() for _ in range(100)}
    # All generated verifiers should be unique
    assert len(verifiers) == 100


def test_generate_code_verifier_valid_characters() -> None:
    """
    Test that generate_code_verifier only uses valid characters (RFC 7636).
    """
    code_verifier = generate_code_verifier()
    # RFC 7636 allows: [A-Z] / [a-z] / [0-9] / "-" / "." / "_" / "~"
    # URL-safe base64 uses: [A-Z] / [a-z] / [0-9] / "-" / "_"
    valid_chars = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert all(char in valid_chars for char in code_verifier)


def test_generate_code_challenge_s256() -> None:
    """
    Test that generate_code_challenge produces correct S256 challenge.
    """
    # Use a known code_verifier to verify the challenge computation
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    # Compute expected challenge manually
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    code_challenge = generate_code_challenge(code_verifier)
    assert code_challenge == expected_challenge


def test_generate_code_challenge_rfc_example() -> None:
    """
    Test PKCE code challenge against RFC 7636 Appendix B example.

    See: https://datatracker.ietf.org/doc/html/rfc7636#appendix-B
    """
    # RFC 7636 example code_verifier (Appendix B)
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    # RFC 7636 expected code_challenge for S256 method
    expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    code_challenge = generate_code_challenge(code_verifier)
    assert code_challenge == expected_challenge


def test_encode_decode_oauth2_state(
    mocker: MockerFixture,
) -> None:
    """
    Test that encode/decode cycle preserves state fields.
    """
    from superset.superset_typing import OAuth2State

    mocker.patch(
        "flask.current_app.config",
        {
            "SECRET_KEY": "test-secret-key",
            "DATABASE_OAUTH2_JWT_ALGORITHM": "HS256",
        },
    )

    state: OAuth2State = {
        "database_id": 1,
        "user_id": 2,
        "default_redirect_uri": "http://localhost:8088/api/v1/oauth2/",
        "tab_id": "test-tab-id",
    }

    with freeze_time("2024-01-01"):
        encoded = encode_oauth2_state(state)
        decoded = decode_oauth2_state(encoded)

    assert "code_verifier" not in decoded
    assert decoded["database_id"] == 1
    assert decoded["user_id"] == 2


def test_get_oauth2_access_token_lock_not_acquired_no_error_log(
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Test that when a distributed lock can't be acquired, no error is logged and
    the function returns None instead of raising.

    This scenario occurs when a dashboard with multiple charts from the same
    OAuth2-enabled DB has an expired token: simultaneous requests compete for
    the lock, and only the first one wins. The rest should silently return None.
    """
    import logging

    from superset.exceptions import AcquireDistributedLockFailedException

    mocker.patch("time.sleep")  # avoid backoff delays in tests

    db = mocker.patch("superset.utils.oauth2.db")
    db_engine_spec = mocker.MagicMock()
    token = mocker.MagicMock()
    token.access_token = "access-token"  # noqa: S105
    token.access_token_expiration = datetime(2024, 1, 1)
    token.refresh_token = "refresh-token"  # noqa: S105
    db.session.query().filter_by().one_or_none.return_value = token

    mocker.patch(
        "superset.utils.oauth2.refresh_oauth2_token",
        side_effect=AcquireDistributedLockFailedException("Lock not available"),
    )

    with freeze_time("2024-01-02"):
        with caplog.at_level(logging.DEBUG):
            result = get_oauth2_access_token({}, 1, 1, db_engine_spec)

    assert result is None
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_get_oauth2_redirect_uri_from_config(mocker: MockerFixture) -> None:
    """
    Test that get_oauth2_redirect_uri returns the configured value when set.
    """
    custom_uri = "https://proxy.example.com/oauth2/"
    mocker.patch(
        "flask.current_app.config",
        {"DATABASE_OAUTH2_REDIRECT_URI": custom_uri},
    )
    assert get_oauth2_redirect_uri() == custom_uri


def test_get_oauth2_redirect_uri_falls_back_to_url_for(mocker: MockerFixture) -> None:
    """
    Test that get_oauth2_redirect_uri falls back to url_for when config is not set.
    """
    fallback_uri = "http://localhost:8088/api/v1/database/oauth2/"
    mocker.patch("flask.current_app.config", {})
    mocker.patch(
        "superset.utils.oauth2.url_for",
        return_value=fallback_uri,
    )
    assert get_oauth2_redirect_uri() == fallback_uri


def test_get_oauth2_redirect_uri_raises_on_build_error(
    mocker: MockerFixture,
) -> None:
    """
    Test that get_oauth2_redirect_uri raises OAuth2Error when url_for raises
    BuildError (e.g. in headless/MCP contexts).
    """
    from werkzeug.routing import BuildError

    from superset.exceptions import OAuth2Error

    mocker.patch("flask.current_app.config", {})
    mocker.patch(
        "superset.utils.oauth2.url_for",
        side_effect=BuildError("DatabaseRestApi.oauth2", {}, ("GET",)),
    )
    with pytest.raises(OAuth2Error):
        get_oauth2_redirect_uri()


def test_get_oauth2_redirect_uri_raises_on_runtime_error(
    mocker: MockerFixture,
) -> None:
    """
    Test that get_oauth2_redirect_uri raises OAuth2Error when url_for raises
    RuntimeError (e.g. no request context and no SERVER_NAME).
    """
    from superset.exceptions import OAuth2Error

    mocker.patch("flask.current_app.config", {})
    mocker.patch(
        "superset.utils.oauth2.url_for",
        side_effect=RuntimeError("Unable to build URL outside of request context"),
    )
    with pytest.raises(OAuth2Error):
        get_oauth2_redirect_uri()
