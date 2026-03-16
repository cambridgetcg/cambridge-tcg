"""Shopify OAuth token management via client credentials grant.

Tokens expire every 24 hours. This module:
1. Checks local cache file (~/.shopify_token.json) for a valid token
2. If expired or missing, requests a new one via client_credentials grant
3. Caches the new token locally with expiry timestamp
4. Validates granted scopes against REQUIRED_SCOPES

Credentials are read from env vars:
    SHOPIFY_STORE: e.g. "6e824e-a9.myshopify.com"
    SHOPIFY_CLIENT_ID: App client ID
    SHOPIFY_CLIENT_SECRET: App client secret

Scope configuration:
    Scopes are set in Shopify Admin → Settings → Apps and sales channels →
    Develop apps → [App] → Configuration → Admin API access scopes.
    After changing scopes, delete .shopify_token.json to force a refresh.
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# All scopes needed for full storefront + pipeline management.
# Configure these on the app in Shopify Admin before requesting a token.
# Note: Shopify grants write_* without the read_* counterpart in the scope
# string, but write implies read access. We check write scopes only.
REQUIRED_SCOPES = {
    # Theme & storefront
    'write_themes',
    'write_content',
    'write_online_store_navigation',
    'write_files',
    'write_translations',
    # Products & inventory
    'write_products',
    'write_product_listings',
    'write_inventory',
    'read_locations',  # read-only — no write equivalent
    # Orders & customers
    'write_orders',
    'write_customers',
    'write_discounts',
    # Custom data
    'write_metaobject_definitions',
    'write_metaobjects',
}

# Local file cache — persists across CLI invocations
_DEFAULT_CACHE_PATH = os.path.join(os.path.dirname(__file__), '.shopify_token.json')

# In-memory cache — avoids file reads within same process
_cached_token = None
_token_expiry = 0


def get_access_token(store=None, client_id=None, client_secret=None,
                     cache_path=None):
    """Get a valid Shopify access token, refreshing if needed.

    Checks in-memory cache first, then local file cache, then requests
    a new token via client credentials grant.

    Returns:
        str: Valid access token (shpat_...)
    """
    global _cached_token, _token_expiry

    # 1. In-memory cache
    if _cached_token and time.time() < _token_expiry:
        return _cached_token

    cache_path = cache_path or _DEFAULT_CACHE_PATH

    # 2. File cache
    token_data = _read_cache(cache_path)
    if token_data:
        _cached_token = token_data['access_token']
        _token_expiry = token_data['expires_at']
        if time.time() < _token_expiry:
            logger.debug("Using cached Shopify token (file)")
            _check_scopes(token_data.get('scope', ''))
            return _cached_token

    # 3. Request new token
    store = store or os.environ['SHOPIFY_STORE']
    client_id = client_id or os.environ['SHOPIFY_CLIENT_ID']
    client_secret = client_secret or os.environ['SHOPIFY_CLIENT_SECRET']

    logger.info("Requesting new Shopify access token...")
    token_data = _request_token(store, client_id, client_secret)

    # Cache with 5-minute early expiry buffer
    expires_at = time.time() + token_data['expires_in'] - 300
    cache_entry = {
        'access_token': token_data['access_token'],
        'scope': token_data.get('scope', ''),
        'expires_at': expires_at,
    }

    _cached_token = token_data['access_token']
    _token_expiry = expires_at

    _write_cache(cache_path, cache_entry)
    logger.info(f"New token cached (expires in {token_data['expires_in'] // 3600}h)")

    _check_scopes(token_data.get('scope', ''))

    return _cached_token


def _check_scopes(granted_scope_str):
    """Warn about missing scopes. Non-fatal — logs warnings only."""
    granted = set(s.strip() for s in granted_scope_str.split(',') if s.strip())
    missing = REQUIRED_SCOPES - granted
    if missing:
        logger.warning(
            f"Token is missing {len(missing)} required scope(s): {', '.join(sorted(missing))}. "
            f"Update in Shopify Admin → Settings → Apps → Develop apps → API access scopes, "
            f"then delete .shopify_token.json to refresh."
        )
    else:
        logger.debug("All required scopes granted")


def _request_token(store, client_id, client_secret):
    """Request access token via client credentials grant."""
    url = f"https://{store}/admin/oauth/access_token"

    resp = requests.post(url, json={
        'client_id': client_id,
        'client_secret': client_secret,
        'grant_type': 'client_credentials',
    }, timeout=15)

    if resp.status_code != 200:
        raise Exception(
            f"Shopify token request failed ({resp.status_code}): {resp.text[:500]}"
        )

    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f"No access_token in response: {data}")

    return data


def _read_cache(path):
    """Read token cache file. Returns dict or None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Invalid token cache at {path}: {e}")
        return None


def _write_cache(path, data):
    """Write token cache file."""
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning(f"Failed to write token cache: {e}")
