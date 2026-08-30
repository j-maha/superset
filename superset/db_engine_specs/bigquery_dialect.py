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

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.api_core import client_info
from google.cloud import bigquery
from google.oauth2.credentials import Credentials
from sqlalchemy.engine.url import URL
from sqlalchemy_bigquery import BigQueryDialect
from sqlalchemy_bigquery._helpers import SCOPES
from sqlalchemy_bigquery.parse_url import parse_url

from superset.db_engine_specs.exceptions import BigQueryOAuth2TokenRequiredError


class ExtendedQueryDialect(BigQueryDialect):
    """BigQuery dialect that can construct a client from a user token."""

    driver = "extended"
    arraysize: int
    credentials_path: str | None
    billing_project_id: str | None
    location: str | None
    credentials_base64: str | None
    project_id: str | None
    dataset_id: str | None
    list_tables_page_size: int

    def __init__(
        self,
        oauth_token_provider: Callable[[], str | None] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.oauth_token_provider = oauth_token_provider
        super().__init__(*args, **kwargs)

    def create_connect_args(self, url: URL) -> tuple[list[Any], dict[str, Any]]:
        """Build a BigQuery client without putting credentials in the URL."""
        if self.oauth_token_provider is None:
            return super().create_connect_args(url)

        # Impersonated engines should have a token, but the provider can still
        # return none after a token is revoked or unavailable in a worker.
        oauth_token = self.oauth_token_provider()
        if not oauth_token:
            raise BigQueryOAuth2TokenRequiredError(
                "An OAuth2 access token is required for impersonation"
            )

        (
            self.project_id,
            location,
            dataset_id,
            arraysize,
            credentials_path,
            credentials_base64,
            provided_job_config,
            list_tables_page_size,
            user_supplied_client,
        ) = parse_url(url)

        if user_supplied_client:
            raise ValueError("oauth_token cannot be combined with user_supplied_client")

        self.arraysize = arraysize or self.arraysize
        self.list_tables_page_size = list_tables_page_size or self.list_tables_page_size
        self.location = location or self.location
        self.credentials_path = credentials_path or self.credentials_path
        self.credentials_base64 = credentials_base64 or self.credentials_base64
        self.dataset_id = dataset_id
        self.billing_project_id = self.billing_project_id or self.project_id

        credentials = Credentials(token=oauth_token, scopes=SCOPES)
        client = bigquery.Client(
            client_info=client_info.ClientInfo(user_agent="superset"),
            project=self.billing_project_id,
            credentials=credentials,
            location=self.location,
            default_query_job_config=self.create_job_config(provided_job_config),
        )
        self.project_id = self.project_id or client.project
        self.billing_project_id = self.billing_project_id or client.project
        self.oauth_client = client
        return [], {"client": client}
