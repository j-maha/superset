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

import pytest
from pytest_mock import MockerFixture

from superset.commands.database.test_connection import TestConnectionDatabaseCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import OAuth2RedirectError, OAuth2RequiresSavedDBError


def test_command(mocker: MockerFixture) -> None:
    """
    Test the happy path of the command.
    """
    user = mocker.MagicMock()
    user.email = "alice@example.org"
    mocker.patch("superset.db_engine_specs.gsheets.g", user=user)
    mocker.patch("superset.db_engine_specs.gsheets.create_engine")

    database = mocker.MagicMock()
    database.db_engine_spec.__name__ = "GSheetsEngineSpec"
    with database.get_sqla_engine() as engine:
        engine.dialect.do_ping.return_value = True

    DatabaseDAO = mocker.patch("superset.commands.database.test_connection.DatabaseDAO")  # noqa: N806
    DatabaseDAO.build_db_for_connection_test.return_value = database

    properties = {
        "sqlalchemy_uri": "gsheets://",
        "engine": "gsheets",
        "driver": "gsheets",
        "catalog": {"test": "https://example.org/"},
    }
    command = TestConnectionDatabaseCommand(properties)
    command.run()


def test_command_with_oauth2(mocker: MockerFixture) -> None:
    """
    Test the command when OAuth2 is needed.
    """
    user = mocker.MagicMock()
    user.email = "alice@example.org"
    mocker.patch("superset.db_engine_specs.gsheets.g", user=user)
    mocker.patch("superset.db_engine_specs.gsheets.create_engine")

    database = mocker.MagicMock()
    database.is_oauth2_enabled.return_value = True
    database.db_engine_spec.needs_oauth2.return_value = True
    database.start_oauth2_dance.side_effect = OAuth2RedirectError(
        "url",
        "tab_id",
        "redirect_uri",
    )
    database.db_engine_spec.__name__ = "GSheetsEngineSpec"
    with database.get_sqla_engine() as engine:
        engine.dialect.do_ping.side_effect = Exception("OAuth2 needed")

    DatabaseDAO = mocker.patch("superset.commands.database.test_connection.DatabaseDAO")  # noqa: N806
    DatabaseDAO.build_db_for_connection_test.return_value = database

    properties = {
        "sqlalchemy_uri": "gsheets://",
        "engine": "gsheets",
        "driver": "gsheets",
        "catalog": {"test": "https://example.org/"},
    }
    command = TestConnectionDatabaseCommand(properties)
    with pytest.raises(OAuth2RedirectError) as excinfo:
        command.run()
    assert excinfo.value.error == SupersetError(
        message="You don't have permission to access the data.",
        error_type=SupersetErrorType.OAUTH2_REDIRECT,
        level=ErrorLevel.WARNING,
        extra={"url": "url", "tab_id": "tab_id", "redirect_uri": "redirect_uri"},
    )


def test_existing_database_oauth_uses_persisted_model(
    mocker: MockerFixture,
) -> None:
    """OAuth for an edit test must use the persisted database model."""
    user = mocker.MagicMock()
    user.email = "alice@example.org"
    mocker.patch("superset.db_engine_specs.gsheets.g", user=user)
    mocker.patch("superset.db_engine_specs.gsheets.create_engine")

    persisted_database = mocker.MagicMock()
    persisted_database.id = 42
    persisted_database.safe_sqlalchemy_uri.return_value = "gsheets://"
    persisted_database.start_oauth2_dance.side_effect = OAuth2RedirectError(
        "url",
        "tab_id",
        "redirect_uri",
    )

    transient_database = mocker.MagicMock()
    transient_database.id = None
    transient_database.is_oauth2_enabled.return_value = True
    transient_database.db_engine_spec.needs_oauth2.return_value = True
    transient_database.db_engine_spec.__name__ = "GSheetsEngineSpec"
    transient_database.get_sqla_engine.return_value.__enter__.side_effect = Exception(
        "OAuth2 needed"
    )

    database_dao = mocker.patch(
        "superset.commands.database.test_connection.DatabaseDAO"
    )
    database_dao.get_database_by_name.return_value = persisted_database
    database_dao.build_db_for_connection_test.return_value = transient_database

    properties = {
        "database_name": "existing",
        "sqlalchemy_uri": "gsheets://",
        "engine": "gsheets",
        "driver": "gsheets",
        "catalog": {"test": "https://example.org/"},
    }

    with pytest.raises(OAuth2RedirectError):
        TestConnectionDatabaseCommand(properties).run()

    persisted_database.start_oauth2_dance.assert_called_once_with()
    transient_database.start_oauth2_dance.assert_not_called()
    assert transient_database.id is None


def test_command_with_oauth2_unsaved_database(mocker: MockerFixture) -> None:
    """Test strict and create-flow handling of unsaved OAuth2 connections."""
    user = mocker.MagicMock()
    user.email = "alice@example.org"
    mocker.patch("superset.db_engine_specs.gsheets.g", user=user)
    mocker.patch("superset.db_engine_specs.gsheets.create_engine")

    database = mocker.MagicMock()
    database.id = None
    database.is_oauth2_enabled.return_value = False
    database.db_engine_spec.needs_oauth2.return_value = True
    database.db_engine_spec.__name__ = "GSheetsEngineSpec"
    database.get_sqla_engine.return_value.__enter__.side_effect = Exception(
        "OAuth2 needed"
    )

    DatabaseDAO = mocker.patch("superset.commands.database.test_connection.DatabaseDAO")  # noqa: N806
    DatabaseDAO.build_db_for_connection_test.return_value = database

    properties = {
        "sqlalchemy_uri": "gsheets://",
        "engine": "gsheets",
        "driver": "gsheets",
        "catalog": {"test": "https://example.org/"},
    }
    command = TestConnectionDatabaseCommand(properties)
    with pytest.raises(OAuth2RequiresSavedDBError) as excinfo:
        command.run()
    assert excinfo.value.error.error_type == (
        SupersetErrorType.OAUTH2_REQUIRES_SAVED_DATABASE
    )

    TestConnectionDatabaseCommand(
        properties,
        allow_unsaved_oauth2=True,
    ).run()
