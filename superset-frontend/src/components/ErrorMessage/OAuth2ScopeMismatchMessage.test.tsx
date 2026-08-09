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
import { fireEvent } from '@testing-library/react';
import { ErrorLevel, ErrorTypeEnum } from '@superset-ui/core';
import { render } from 'spec/helpers/testing-library';

import { OAuth2ScopeMismatchMessage } from './OAuth2ScopeMismatchMessage';

test('renders OAuth2 scope mismatch details', () => {
  const { getByRole, getByText } = render(
    <OAuth2ScopeMismatchMessage
      error={{
        error_type: ErrorTypeEnum.OAUTH2_SCOPE_MISMATCH,
        message: 'scope mismatch',
        level: 'error' as ErrorLevel,
        extra: {
          policy: 'subset',
          required_scopes: ['scope-a'],
          granted_scopes: ['scope-a', 'scope-extra'],
          missing_scopes: [],
          unexpected_scopes: ['scope-extra'],
        },
      }}
    />,
  );

  expect(getByText(/OAuth2 scope mismatch/i)).toBeInTheDocument();
  fireEvent.click(getByRole('button', { name: /See more/i }));
  expect(getByText(/Matching policy: subset/i)).toBeInTheDocument();
  expect(getByText(/scope-extra/i)).toBeInTheDocument();
});
