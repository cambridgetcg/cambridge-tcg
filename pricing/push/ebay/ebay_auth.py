"""
eBay OAuth 2.0 token management.

Retrieves credentials from AWS Secrets Manager, refreshes the User Access Token
using the long-lived refresh token (~1.9 year validity).

Secrets Manager key: ebay-trading-api-credentials
Expected secret JSON:
{
    "app_id": "...",
    "cert_id": "...",
    "dev_id": "...",
    "refresh_token": "...",
    "environment": "PRODUCTION"  // or "SANDBOX"
}
"""

import os
import json
import base64
import time
import boto3
import requests

# Token endpoints
ENDPOINTS = {
    'PRODUCTION': 'https://api.ebay.com/identity/v1/oauth2/token',
    'SANDBOX': 'https://api.sandbox.ebay.com/identity/v1/oauth2/token',
}

# OAuth scope for Trading API access
OAUTH_SCOPE = 'https://api.ebay.com/oauth/api_scope/sell.inventory'

# Module-level cache for access token (with TTL)
_cached_token = None
_token_expiry = 0


def get_credentials(secret_name=None):
    """Retrieve eBay API credentials from Secrets Manager."""
    secret_name = secret_name or os.environ.get(
        'EBAY_SECRET_NAME', 'ebay-trading-api-credentials'
    )

    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response['SecretString'])


def refresh_access_token(credentials):
    """
    Exchange refresh token for a new User Access Token.

    The refresh token has ~1.9 year validity. The access token
    returned is valid for 2 hours.

    Returns:
        str: Fresh access token
    """
    global _cached_token, _token_expiry

    environment = credentials.get('environment', 'PRODUCTION')
    token_url = ENDPOINTS[environment]

    # Basic auth: base64(app_id:cert_id)
    auth_string = f"{credentials['app_id']}:{credentials['cert_id']}"
    auth_header = base64.b64encode(auth_string.encode()).decode()

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': f'Basic {auth_header}',
    }

    data = {
        'grant_type': 'refresh_token',
        'refresh_token': credentials['refresh_token'],
        'scope': OAUTH_SCOPE,
    }

    response = requests.post(token_url, headers=headers, data=data)

    if response.status_code != 200:
        raise Exception(
            f"Token refresh failed ({response.status_code}): {response.text}"
        )

    token_data = response.json()
    _cached_token = token_data['access_token']
    expires_in = token_data.get('expires_in', 7200)
    _token_expiry = time.time() + expires_in - 300  # refresh 5min early
    return _cached_token


def get_access_token(credentials=None):
    """
    Get a valid access token, using cache if available.

    Supports two credential types:
    - auth_token: Direct-use token (Auth'n'Auth or pre-exchanged OAuth)
    - refresh_token: OAuth refresh flow (existing logic, cached with TTL)

    Tokens are cached with TTL. Refreshes 5 minutes before expiry
    to avoid using stale tokens on warm Lambda containers.
    """
    global _cached_token, _token_expiry

    if _cached_token and time.time() < _token_expiry:
        return _cached_token

    if credentials is None:
        credentials = get_credentials()

    # Direct-use token (Auth'n'Auth / pre-exchanged OAuth)
    if 'auth_token' in credentials:
        return credentials['auth_token']

    return refresh_access_token(credentials)
