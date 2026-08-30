/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { t } from '@apache-superset/core/translation';

import type { ErrorMessageComponentProps } from './types';
import { ErrorAlert } from './ErrorAlert';

interface OAuth2ScopeMismatchExtra {
  policy?: string;
  required_scopes?: string[];
  granted_scopes?: string[];
  missing_scopes?: string[];
  unexpected_scopes?: string[];
}

const formatScopes = (scopes?: string[]) =>
  scopes?.length ? scopes.join(', ') : t('None');

export function OAuth2ScopeMismatchMessage({
  error,
  closable,
}: ErrorMessageComponentProps<OAuth2ScopeMismatchExtra>) {
  const extra = error.extra ?? {};
  const details = [
    `${t('Matching policy')}: ${extra.policy ?? t('unknown')}`,
    `${t('Required scopes')}: ${formatScopes(extra.required_scopes)}`,
    `${t('Granted scopes')}: ${formatScopes(extra.granted_scopes)}`,
    `${t('Missing scopes')}: ${formatScopes(extra.missing_scopes)}`,
    `${t('Unexpected scopes')}: ${formatScopes(extra.unexpected_scopes)}`,
  ].join('\n');

  return (
    <ErrorAlert
      errorType={t('OAuth2 scope mismatch')}
      message={t(
        'OAuth2 authorization completed, but the granted scopes do not satisfy the configured policy.',
      )}
      description={t(
        'Please update the OAuth2 configuration or authorize again.',
      )}
      descriptionDetails={details}
      type="error"
      closable={closable}
    />
  );
}
