"""Shopify Admin API client for stock and listing sync.

Wraps GraphQL queries for variant/inventory reads and mutations for
inventory level updates and product metadata edits.

Rate limiting: Shopify GraphQL uses cost-based throttling (~1000 points/sec).
This client tracks the throttle status returned in each response and backs off
when remaining points are low.

Token management: Access tokens are obtained via client credentials grant
(24h expiry) and cached locally. See auth.py for details.

Environment Variables:
    SHOPIFY_STORE: e.g. "yourstore.myshopify.com"
    SHOPIFY_CLIENT_ID: App client ID
    SHOPIFY_CLIENT_SECRET: App client secret
    SHOPIFY_API_VERSION: e.g. "2024-01"
"""

import logging
import os
import time

import requests

from stock.sync.shopify.auth import get_access_token

logger = logging.getLogger(__name__)

# GraphQL queries

_VARIANTS_QUERY = """\
{
  productVariants(first: 250, after: CURSOR_PLACEHOLDER) {
    edges {
      node {
        id
        sku
        price
        inventoryPolicy
        inventoryItem {
          id
          inventoryLevels(first: 1) {
            edges {
              node {
                id
                location {
                  id
                }
                quantities(names: ["available"]) {
                  name
                  quantity
                }
              }
            }
          }
        }
        product {
          id
          title
          descriptionHtml
          tags
          options {
            id
            name
            values
          }
          metafields(first: 10) {
            edges {
              node {
                namespace
                key
                value
              }
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

_SET_QUANTITIES_MUTATION = """\
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup {
      reason
    }
    userErrors {
      field
      message
    }
  }
}
"""

_PRODUCT_UPDATE_MUTATION = """\
mutation productUpdate($input: ProductInput!) {
  productUpdate(input: $input) {
    product {
      id
      title
    }
    userErrors {
      field
      message
    }
  }
}
"""

_VARIANTS_BULK_UPDATE_MUTATION = """\
mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants {
      id
      inventoryPolicy
    }
    userErrors {
      field
      message
    }
  }
}
"""

_PRODUCT_OPTIONS_CREATE_MUTATION = """\
mutation productOptionsCreate($productId: ID!, $options: [OptionCreateInput!]!, $variantStrategy: ProductOptionCreateVariantStrategy) {
  productOptionsCreate(productId: $productId, options: $options, variantStrategy: $variantStrategy) {
    product {
      id
      options {
        id
        name
        values
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

_VARIANTS_BULK_CREATE_MUTATION = """\
mutation productVariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
    productVariants {
      id
      sku
      price
      inventoryPolicy
    }
    userErrors {
      field
      message
    }
  }
}
"""

_METAFIELDS_SET_MUTATION = """\
mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields {
      id
      namespace
      key
    }
    userErrors {
      field
      message
    }
  }
}
"""

_ORDERS_QUERY = """\
{
  orders(first: LIMIT_PLACEHOLDER, query: "created_at:>CREATED_AT_PLACEHOLDER", sortKey: CREATED_AT) {
    edges {
      node {
        id
        name
        createdAt
        lineItems(first: 50) {
          edges {
            node {
              sku
              quantity
              originalUnitPriceSet {
                shopMoney {
                  amount
                }
              }
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

_WEBHOOK_SUBSCRIPTION_CREATE_MUTATION = """\
mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription {
      id
      topic
      endpoint {
        ... on WebhookHttpEndpoint {
          callbackUrl
        }
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

_WEBHOOK_SUBSCRIPTIONS_QUERY = """\
{
  webhookSubscriptions(first: 50) {
    edges {
      node {
        id
        topic
        endpoint {
          ... on WebhookHttpEndpoint {
            callbackUrl
          }
        }
      }
    }
  }
}
"""

_WEBHOOK_SUBSCRIPTION_DELETE_MUTATION = """\
mutation webhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors {
      field
      message
    }
  }
}
"""

_LOCATIONS_QUERY = """\
{
  locations(first: 10) {
    edges {
      node {
        id
        name
        isActive
      }
    }
  }
}
"""


class ShopifyClient:
    """Shopify Admin API client (GraphQL)."""

    def __init__(self, store=None, token=None, api_version=None):
        self.store = store or os.environ['SHOPIFY_STORE']
        self.api_version = api_version or os.environ.get('SHOPIFY_API_VERSION', '2024-01')
        self.graphql_url = f"https://{self.store}/admin/api/{self.api_version}/graphql.json"
        self._static_token = token  # If provided, skip auth.py
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
        })

    def _get_token(self):
        """Get a valid access token (auto-refreshes if expired)."""
        if self._static_token:
            return self._static_token
        return get_access_token(store=self.store)

    def _graphql(self, query, variables=None):
        """Execute a GraphQL query/mutation. Returns parsed JSON data.

        Handles Shopify cost-based throttling by backing off when
        remaining points fall below 200.
        """
        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        headers = {'X-Shopify-Access-Token': self._get_token()}
        resp = self.session.post(self.graphql_url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"Shopify GraphQL HTTP {resp.status_code}: {resp.text[:500]}")

        body = resp.json()

        # Check for top-level errors
        if body.get('errors'):
            msgs = [e.get('message', str(e)) for e in body['errors']]
            raise Exception(f"Shopify GraphQL errors: {'; '.join(msgs)}")

        # Throttle awareness
        extensions = body.get('extensions', {})
        cost = extensions.get('cost', {})
        throttle = cost.get('throttleStatus', {})
        remaining = throttle.get('currentlyAvailable', 1000)
        if remaining < 200:
            restore_rate = throttle.get('restoreRate', 50)
            sleep_time = (200 - remaining) / max(restore_rate, 1)
            logger.debug(f"Throttle: {remaining} points remaining, sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

        return body.get('data')

    def get_locations(self):
        """List active fulfillment locations.

        Returns list of {'id': 'gid://...', 'name': str, 'is_active': bool}.
        """
        data = self._graphql(_LOCATIONS_QUERY)
        locations = []
        for edge in data['locations']['edges']:
            node = edge['node']
            locations.append({
                'id': node['id'],
                'name': node['name'],
                'is_active': node['isActive'],
            })
        return locations

    def list_webhooks(self):
        """List all webhook subscriptions.

        Returns list of {'id': 'gid://...', 'topic': str, 'callback_url': str}.
        """
        data = self._graphql(_WEBHOOK_SUBSCRIPTIONS_QUERY)
        webhooks = []
        for edge in data['webhookSubscriptions']['edges']:
            node = edge['node']
            endpoint = node.get('endpoint', {})
            webhooks.append({
                'id': node['id'],
                'topic': node['topic'],
                'callback_url': endpoint.get('callbackUrl', ''),
            })
        return webhooks

    def register_webhook(self, topic, callback_url):
        """Register a webhook subscription.

        Args:
            topic: Shopify webhook topic enum (e.g. 'ORDERS_CREATE', 'ORDERS_CANCELLED')
            callback_url: HTTPS URL to receive webhook POSTs

        Returns:
            {'success': bool, 'webhook_id': str|None, 'errors': list}
        """
        data = self._graphql(
            _WEBHOOK_SUBSCRIPTION_CREATE_MUTATION,
            {
                'topic': topic,
                'webhookSubscription': {
                    'callbackUrl': callback_url,
                    'format': 'JSON',
                },
            },
        )
        result = data['webhookSubscriptionCreate']
        user_errors = result.get('userErrors', [])

        if user_errors:
            error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
            return {'success': False, 'webhook_id': None, 'errors': error_msgs}

        subscription = result.get('webhookSubscription')
        webhook_id = subscription['id'] if subscription else None
        return {'success': True, 'webhook_id': webhook_id, 'errors': []}

    def delete_webhook(self, webhook_id):
        """Delete a webhook subscription by GID.

        Returns {'success': bool, 'errors': list}.
        """
        data = self._graphql(
            _WEBHOOK_SUBSCRIPTION_DELETE_MUTATION,
            {'id': webhook_id},
        )
        result = data['webhookSubscriptionDelete']
        user_errors = result.get('userErrors', [])

        if user_errors:
            error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
            return {'success': False, 'errors': error_msgs}
        return {'success': True, 'errors': []}

    def ensure_webhooks(self, callback_url, topics=None):
        """Ensure webhook subscriptions exist for given topics.

        Idempotent: skips topics already registered to the same callback URL.
        Replaces webhooks pointing to a different URL for the same topic.

        Args:
            callback_url: HTTPS URL for webhook delivery
            topics: List of topic enums (default: ORDERS_CREATE + ORDERS_CANCELLED)

        Returns:
            {'created': list, 'existing': list, 'replaced': list, 'errors': list}
        """
        if topics is None:
            topics = ['ORDERS_CREATE', 'ORDERS_CANCELLED']

        existing = self.list_webhooks()
        by_topic = {}
        for wh in existing:
            by_topic.setdefault(wh['topic'], []).append(wh)

        created = []
        already_exists = []
        replaced = []
        errors = []

        for topic in topics:
            current = by_topic.get(topic, [])

            # Check if already registered to the correct URL
            matching = [w for w in current if w['callback_url'] == callback_url]
            if matching:
                already_exists.append(topic)
                continue

            # Remove stale registrations for this topic pointing elsewhere
            for stale in current:
                logger.info(f"Replacing webhook {stale['id']} for {topic} "
                            f"(was: {stale['callback_url']})")
                self.delete_webhook(stale['id'])
                replaced.append({'topic': topic, 'old_url': stale['callback_url']})

            # Register new
            result = self.register_webhook(topic, callback_url)
            if result['success']:
                created.append(topic)
                logger.info(f"Registered webhook for {topic} → {callback_url}")
            else:
                errors.extend(result['errors'])
                logger.error(f"Failed to register webhook for {topic}: {result['errors']}")

        return {
            'created': created,
            'existing': already_exists,
            'replaced': replaced,
            'errors': errors,
        }

    def get_orders(self, created_at_min, limit=50):
        """Fetch recent orders via GraphQL.

        Args:
            created_at_min: ISO 8601 datetime string (e.g. '2026-02-10T00:00:00Z')
            limit: Max orders to fetch (default 50)

        Returns:
            List of {order_id, name, created_at, line_items: [{sku, quantity, price_gbp}]}
        """
        query = _ORDERS_QUERY.replace('LIMIT_PLACEHOLDER', str(limit))
        query = query.replace('CREATED_AT_PLACEHOLDER', created_at_min)

        data = self._graphql(query)
        orders = []

        for edge in data['orders']['edges']:
            node = edge['node']
            line_items = []
            for li_edge in node['lineItems']['edges']:
                li = li_edge['node']
                sku = (li.get('sku') or '').strip()
                qty = li.get('quantity', 0)
                price_gbp = None
                price_set = li.get('originalUnitPriceSet')
                if price_set:
                    shop_money = price_set.get('shopMoney', {})
                    try:
                        price_gbp = float(shop_money.get('amount', 0))
                    except (ValueError, TypeError):
                        pass
                if sku and qty > 0:
                    line_items.append({
                        'sku': sku,
                        'quantity': qty,
                        'price_gbp': price_gbp,
                    })

            if line_items:
                orders.append({
                    'order_id': node['id'],
                    'name': node.get('name', ''),
                    'created_at': node.get('createdAt', ''),
                    'line_items': line_items,
                })

        return orders

    def get_all_variants(self):
        """Fetch all product variants with inventory and product info.

        Paginates automatically through all variants (250 per page).

        Returns list of dicts:
        {
            'variant_id': 'gid://shopify/ProductVariant/...',
            'sku': str,
            'price': float,
            'inventory_item_id': 'gid://shopify/InventoryItem/...',
            'location_id': 'gid://shopify/Location/...',
            'inventory_level_id': 'gid://shopify/InventoryLevel/...',
            'available': int,
            'product_id': 'gid://shopify/Product/...',
            'title': str,
            'description_html': str,
            'tags': list[str],
        }
        """
        variants = []
        cursor = None

        while True:
            # Build query with cursor
            if cursor:
                query = _VARIANTS_QUERY.replace(
                    'CURSOR_PLACEHOLDER', f'"{cursor}"')
            else:
                query = _VARIANTS_QUERY.replace(
                    ', after: CURSOR_PLACEHOLDER', '')

            data = self._graphql(query)
            pv_data = data['productVariants']

            for edge in pv_data['edges']:
                node = edge['node']
                variant = self._parse_variant_node(node)
                if variant:
                    variants.append(variant)

            page_info = pv_data['pageInfo']
            if not page_info['hasNextPage']:
                break
            cursor = page_info['endCursor']
            logger.info(f"Fetched {len(variants)} variants so far...")

        logger.info(f"Total: {len(variants)} variants fetched")
        return variants

    def set_inventory_quantities(self, location_id, items):
        """Batch set absolute inventory quantities.

        Args:
            location_id: 'gid://shopify/Location/...'
            items: list of {'inventory_item_id': str, 'quantity': int}

        Processes in batches of 100 (Shopify limit for inventorySetQuantities).

        Returns list of {'batch': int, 'success': bool, 'errors': list}.
        """
        results = []

        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            batch_num = i // 100 + 1

            quantities = []
            for item in batch:
                quantities.append({
                    'inventoryItemId': item['inventory_item_id'],
                    'locationId': location_id,
                    'quantity': item['quantity'],
                })

            variables = {
                'input': {
                    'reason': 'correction',
                    'name': 'available',
                    'ignoreCompareQuantity': True,
                    'quantities': quantities,
                }
            }

            data = self._graphql(_SET_QUANTITIES_MUTATION, variables)
            mutation_data = data['inventorySetQuantities']
            user_errors = mutation_data.get('userErrors', [])

            if user_errors:
                error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
                logger.error(f"Batch {batch_num}: {'; '.join(error_msgs)}")
                results.append({
                    'batch': batch_num,
                    'success': False,
                    'errors': error_msgs,
                    'count': len(batch),
                })
            else:
                logger.info(f"Batch {batch_num}: {len(batch)} quantities set")
                results.append({
                    'batch': batch_num,
                    'success': True,
                    'errors': [],
                    'count': len(batch),
                })

        return results

    def update_product(self, product_id, title=None, description_html=None,
                       tags=None, metafields=None):
        """Update product metadata via GraphQL mutation.

        Only sends fields that are provided (non-None).

        Args:
            metafields: list of {'namespace': str, 'key': str, 'value': str, 'type': str}

        Returns {'success': bool, 'product_id': str, 'errors': list}.
        """
        product_input = {'id': product_id}
        if title is not None:
            product_input['title'] = title
        if description_html is not None:
            product_input['descriptionHtml'] = description_html
        if tags is not None:
            product_input['tags'] = tags
        if metafields is not None:
            product_input['metafields'] = metafields

        data = self._graphql(_PRODUCT_UPDATE_MUTATION, {'input': product_input})
        mutation_data = data['productUpdate']
        user_errors = mutation_data.get('userErrors', [])

        if user_errors:
            error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
            return {'success': False, 'product_id': product_id, 'errors': error_msgs}

        return {'success': True, 'product_id': product_id, 'errors': []}

    def set_metafields_batch(self, metafields):
        """Set metafields via metafieldsSet mutation.

        Args:
            metafields: list of dicts with keys:
                - ownerId: 'gid://shopify/Product/...'
                - namespace: str (e.g. 'shopify')
                - key: str (e.g. 'condition')
                - value: str (JSON-encoded, e.g. '["gid://shopify/Metaobject/..."]')
                - type: str (e.g. 'list.metaobject_reference')

        Batches in groups of 25 (Shopify limit for metafieldsSet).

        Returns list of {'batch': int, 'success': bool, 'errors': list, 'count': int}.
        """
        results = []

        for i in range(0, len(metafields), 25):
            batch = metafields[i:i + 25]
            batch_num = i // 25 + 1

            data = self._graphql(_METAFIELDS_SET_MUTATION, {'metafields': batch})
            mutation_data = data['metafieldsSet']
            user_errors = mutation_data.get('userErrors', [])

            if user_errors:
                error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
                logger.error(f"metafieldsSet batch {batch_num}: {'; '.join(error_msgs)}")
                results.append({
                    'batch': batch_num,
                    'success': False,
                    'errors': error_msgs,
                    'count': len(batch),
                })
            else:
                set_count = len(mutation_data.get('metafields', []))
                logger.info(f"metafieldsSet batch {batch_num}: {set_count} metafields set")
                results.append({
                    'batch': batch_num,
                    'success': True,
                    'errors': [],
                    'count': set_count,
                })

        return results

    def set_inventory_policy(self, variants, policy='DENY'):
        """Bulk-update inventory policy on variants.

        Args:
            variants: list of {'variant_id': str, 'product_id': str}
            policy: 'DENY' (stop selling at 0) or 'CONTINUE'

        Groups by product_id (Shopify requires it) and batches updates.
        Returns {'updated': int, 'errors': int, 'error_details': list}.
        """
        # Group by product_id
        by_product = {}
        for v in variants:
            pid = v['product_id']
            by_product.setdefault(pid, []).append(v['variant_id'])

        updated = 0
        errors = 0
        error_details = []

        for product_id, variant_ids in by_product.items():
            variant_inputs = [{'id': vid, 'inventoryPolicy': policy} for vid in variant_ids]
            try:
                data = self._graphql(
                    _VARIANTS_BULK_UPDATE_MUTATION,
                    {'productId': product_id, 'variants': variant_inputs},
                )
                user_errors = data['productVariantsBulkUpdate'].get('userErrors', [])
                if user_errors:
                    errors += len(variant_ids)
                    error_details.extend(
                        f"{e['field']}: {e['message']}" for e in user_errors
                    )
                else:
                    updated += len(variant_ids)
            except Exception as e:
                errors += len(variant_ids)
                error_details.append(str(e))

        return {'updated': updated, 'errors': errors, 'error_details': error_details}

    def create_product_options(self, product_id, options, variant_strategy='LEAVE_AS_IS'):
        """Add options to an existing product.

        Args:
            product_id: 'gid://shopify/Product/...'
            options: [{'name': 'Availability', 'values': [{'name': 'In Stock'}, {'name': 'Pre-Order'}]}]
            variant_strategy: How to handle existing variants. LEAVE_AS_IS maps them
                to the first option value.

        Returns {'success': bool, 'product': dict|None, 'errors': list}.
        """
        data = self._graphql(
            _PRODUCT_OPTIONS_CREATE_MUTATION,
            {
                'productId': product_id,
                'options': options,
                'variantStrategy': variant_strategy,
            },
        )
        result = data['productOptionsCreate']
        user_errors = result.get('userErrors', [])
        if user_errors:
            error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
            return {'success': False, 'product': None, 'errors': error_msgs}
        return {'success': True, 'product': result.get('product'), 'errors': []}

    def create_variants_bulk(self, product_id, variants):
        """Bulk-create variants for a product.

        Args:
            product_id: 'gid://shopify/Product/...'
            variants: list of dicts with keys:
                - price: str (e.g. '9.80')
                - sku: str
                - inventoryPolicy: 'DENY' or 'CONTINUE'
                - optionValues: [{'optionName': str, 'name': str}]
                - metafields: [{'namespace': str, 'key': str, 'value': str, 'type': str}]

        Returns {'success': bool, 'variants': list|None, 'errors': list}.
        """
        data = self._graphql(
            _VARIANTS_BULK_CREATE_MUTATION,
            {'productId': product_id, 'variants': variants},
        )
        result = data['productVariantsBulkCreate']
        user_errors = result.get('userErrors', [])
        if user_errors:
            error_msgs = [f"{e['field']}: {e['message']}" for e in user_errors]
            return {'success': False, 'variants': None, 'errors': error_msgs}
        return {
            'success': True,
            'variants': result.get('productVariants', []),
            'errors': [],
        }

    def _parse_variant_node(self, node):
        """Parse a productVariant GraphQL node into a flat dict."""
        sku = (node.get('sku') or '').strip()

        # Extract inventory info
        inv_item = node.get('inventoryItem') or {}
        inv_item_id = inv_item.get('id', '')
        inv_levels = inv_item.get('inventoryLevels', {}).get('edges', [])

        location_id = ''
        inv_level_id = ''
        available = 0
        if inv_levels:
            level_node = inv_levels[0]['node']
            location_id = level_node.get('location', {}).get('id', '')
            inv_level_id = level_node.get('id', '')
            for q in level_node.get('quantities', []):
                if q['name'] == 'available':
                    available = q['quantity']

        # Extract product info
        product = node.get('product') or {}
        product_id = product.get('id', '')
        title = product.get('title', '')
        description_html = product.get('descriptionHtml', '')
        tags = product.get('tags', [])

        # Extract product options as [{'id': str, 'name': str, 'values': list}]
        options = product.get('options', [])

        # Extract metafields as {namespace.key: value}
        metafields = {}
        for mf_edge in product.get('metafields', {}).get('edges', []):
            mf = mf_edge['node']
            metafields[f"{mf['namespace']}.{mf['key']}"] = mf['value']

        price_str = node.get('price', '0')
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            price = 0.0

        return {
            'variant_id': node['id'],
            'sku': sku,
            'price': price,
            'inventory_policy': node.get('inventoryPolicy', 'DENY'),
            'inventory_item_id': inv_item_id,
            'location_id': location_id,
            'inventory_level_id': inv_level_id,
            'available': available,
            'product_id': product_id,
            'title': title,
            'description_html': description_html,
            'tags': tags,
            'options': options,
            'metafields': metafields,
        }
