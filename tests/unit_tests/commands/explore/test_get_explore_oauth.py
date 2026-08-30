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

from unittest.mock import MagicMock, PropertyMock

import pytest
from flask import current_app
from pytest_mock import MockerFixture

from superset.commands.explore.get import GetExploreCommand
from superset.commands.explore.parameters import CommandParameters
from superset.exceptions import OAuth2RedirectError


def test_get_explore_preserves_oauth_redirect_when_loading_dataset_metadata(
    app_context: None,
    mocker: MockerFixture,
) -> None:
    """Explore must return the authorization URL instead of empty dataset metadata."""
    oauth_error = OAuth2RedirectError(
        "https://accounts.example.test/authorize",
        "test-tab-id",
        "http://localhost/api/v1/database/oauth2/",
    )
    datasource = MagicMock(name="datasource")
    datasource.name = "sample_data"
    type(datasource).data = PropertyMock(side_effect=oauth_error)

    mocker.patch(
        "superset.commands.explore.get.get_form_data",
        return_value=({"datasource": "3__table", "viz_type": "table"}, None),
    )
    mocker.patch(
        "superset.commands.explore.get.get_datasource_info",
        return_value=(3, "table"),
    )
    mocker.patch(
        "superset.commands.explore.get.DatasourceDAO.get_datasource",
        return_value=datasource,
    )
    mocker.patch("superset.commands.explore.get.security_manager.raise_for_access")

    with current_app.test_request_context("/api/v1/explore/"):
        with pytest.raises(OAuth2RedirectError) as excinfo:
            GetExploreCommand(
                CommandParameters(
                    permalink_key=None,
                    form_data_key=None,
                    datasource_id=3,
                    datasource_type="table",
                    slice_id=None,
                )
            ).run()

    assert excinfo.value is oauth_error
