"""
eBay Browse API client for competitor price monitoring.

Uses application-level OAuth (client_credentials grant — no user token needed).
Reads app_id + cert_id from the same Secrets Manager key used by the Trading API.

Rate limit: 5000 calls/day budget → ~100/min sustained.
"""

import os
import json
import base64
import time
import threading

import boto3
import requests


# Browse API search endpoint
BROWSE_SEARCH_URL = 'https://api.ebay.com/buy/browse/v1/item_summary/search'

# OAuth token endpoint
TOKEN_URL = 'https://api.ebay.com/identity/v1/oauth2/token'

# Browse API scope (read-only public data)
BROWSE_SCOPE = 'https://api.ebay.com/oauth/api_scope'

# TCG category on eBay
TCG_CATEGORY_ID = '183050'

# Game name mapping for aspect_filter
GAME_NAMES = {
    'OP': 'One Piece Card Game',
    'PKMN': 'Pokemon Card Game',
    'EB': 'One Piece Card Game',  # EB sets are One Piece extra boosters
}


class RateLimiter:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.calls = []

    def wait(self):
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.pop(0)
                if len(self.calls) < self.max_calls:
                    self.calls.append(time.time())
                    return
                sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)


class BrowseClient:
    """eBay Browse API client with application-level OAuth."""

    def __init__(self, app_id=None, cert_id=None, our_seller_id=None):
        if app_id and cert_id:
            self.app_id = app_id
            self.cert_id = cert_id
        else:
            creds = self._load_credentials()
            self.app_id = creds['app_id']
            self.cert_id = creds['cert_id']

        self.our_seller_id = our_seller_id or os.environ.get('EBAY_OUR_SELLER_ID', '')
        self._token = None
        self._token_expiry = 0
        self._rate_limiter = RateLimiter(max_calls=100, period=60)
        self._session = requests.Session()

    @staticmethod
    def _load_credentials():
        """Load app_id + cert_id from Secrets Manager."""
        secret_name = os.environ.get('EBAY_SECRET_NAME', 'ebay-trading-api-credentials')
        client = boto3.client('secretsmanager')
        response = client.get_secret_value(SecretId=secret_name)
        return json.loads(response['SecretString'])

    def _get_app_token(self):
        """
        Get application OAuth token via client_credentials grant.
        Cached with 5-min-early refresh (same pattern as ebay_auth.py:60-91).
        """
        if self._token and time.time() < self._token_expiry:
            return self._token

        auth_string = f"{self.app_id}:{self.cert_id}"
        auth_header = base64.b64encode(auth_string.encode()).decode()

        response = requests.post(
            TOKEN_URL,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {auth_header}',
            },
            data={
                'grant_type': 'client_credentials',
                'scope': BROWSE_SCOPE,
            },
            timeout=15,
        )

        if response.status_code != 200:
            raise Exception(
                f"App token request failed ({response.status_code}): {response.text}"
            )

        token_data = response.json()
        self._token = token_data['access_token']
        expires_in = token_data.get('expires_in', 7200)
        self._token_expiry = time.time() + expires_in - 300
        return self._token

    def search_competitors(self, game, set_code, card_number, language, our_price):
        """
        Search eBay Browse API for competitor listings of a specific card.

        Primary: aspect_filter (Game + Card Number + Language) + keyword (set code).
        Fallback: keyword-only search if aspect filter returns 0 results.

        Returns list of dicts: [{price, seller, title, item_id, url}]
        """
        token = self._get_app_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_GB',
            'X-EBAY-C-ENDUSERCTX': 'contextualLocation=country=GB,zip=CB11AA',
        }

        game_name = GAME_NAMES.get(game, game)
        min_ratio = float(os.environ.get('MIN_PRICE_RATIO', '0.30'))
        min_price = f"{our_price * min_ratio:.2f}"
        max_price = f"{our_price * 1.5:.2f}"

        # Build filter string
        # Price floor excludes bulk lots / wrong variations / proxies
        filter_parts = [
            f'price:[{min_price}..{max_price}]',
            'priceCurrency:GBP',
            'conditionIds:{4000}',
            'deliveryCountry:GB',
            'buyingOptions:{FIXED_PRICE}',
        ]
        if self.our_seller_id:
            filter_parts.append(f'excludeSellers:{{{self.our_seller_id}}}')
        filter_str = ','.join(filter_parts)

        # Primary search: "{set_code}-{card_number} {language}" keyword
        # eBay titles use "OP01-001" format, so hyphenated keyword is most precise
        primary_keyword = f"{set_code}-{card_number} {language}"
        aspect_filter = (
            f'categoryId:{TCG_CATEGORY_ID},'
            f'Card Game:{{{game_name}}}'
        )

        params = {
            'q': primary_keyword,
            'category_ids': TCG_CATEGORY_ID,
            'aspect_filter': aspect_filter,
            'filter': filter_str,
            'sort': 'price',
            'limit': '50',
        }

        self._rate_limiter.wait()
        results = self._do_search(headers, params)

        # Fallback: drop aspect_filter, broaden keyword
        if not results:
            fallback_keyword = f"{set_code}-{card_number}"
            fallback_params = {
                'q': fallback_keyword,
                'category_ids': TCG_CATEGORY_ID,
                'filter': filter_str,
                'sort': 'price',
                'limit': '50',
            }
            self._rate_limiter.wait()
            results = self._do_search(headers, fallback_params)

        return results

    def _do_search(self, headers, params):
        """Execute a Browse API search and parse the response."""
        try:
            response = self._session.get(
                BROWSE_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=15,
            )
        except Exception as e:
            print(f"  Browse API request failed: {e}")
            return []

        if response.status_code != 200:
            print(f"  Browse API HTTP {response.status_code}: {response.text[:200]}")
            return []

        data = response.json()
        return self._parse_search_response(data)

    @staticmethod
    def _parse_search_response(data):
        """Extract competitor listings from Browse API ItemSummary response."""
        items = data.get('itemSummaries', [])
        results = []

        for item in items:
            price_info = item.get('price', {})
            price_value = price_info.get('value')
            if price_value is None:
                continue

            try:
                price = float(price_value)
            except (ValueError, TypeError):
                continue

            seller_info = item.get('seller', {})
            results.append({
                'price': price,
                'seller': seller_info.get('username', ''),
                'title': item.get('title', ''),
                'item_id': item.get('itemId', ''),
                'url': item.get('itemWebUrl', ''),
            })

        return results
