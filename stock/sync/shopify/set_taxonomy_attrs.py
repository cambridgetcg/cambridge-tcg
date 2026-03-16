"""Set category taxonomy attributes on all card products.

For each card product (SKU starts with OP- or PKMN-), sets Shopify taxonomy
metafields:

  - shopify.condition  → "Mint (M)"
  - shopify.theme      → "Anime"

These are list.metaobject_reference type metafields that require metaobject GIDs
as values. The script discovers metaobject types and GIDs at startup by querying
metafieldDefinitions → metaobjectDefinition → metaobjects.

Prerequisites:
  - App scopes: write_metaobjects, read_metaobject_definitions
  - Delete .shopify_token.json and re-auth if scopes were just added

Usage:
    python -m stock.sync.shopify.set_taxonomy_attrs [--dry-run] [--sku SKU]

Options:
    --dry-run   Preview changes without mutating
    --sku SKU   Only process the product with this SKU
"""

import argparse
import json
import logging
import sys

from stock.sync.shopify.client import ShopifyClient

logger = logging.getLogger(__name__)

# Which shopify.* keys we want to set, and the desired display name for each.
# Attributes whose category constraint doesn't match the product are skipped.
DESIRED_ATTRS = {
    'condition': 'Mint (M)',
    'theme': 'Anime',
}

# Product category for Collectible Trading Cards
PRODUCT_CATEGORY = 'ae-2-2-3'

_METAFIELD_DEFS_QUERY = """\
{
  metafieldDefinitions(first: 50, ownerType: PRODUCT, namespace: "shopify") {
    edges {
      node {
        id
        namespace
        key
        type { name }
        validations { name value }
      }
    }
  }
}
"""

_METAFIELD_DEF_CONSTRAINTS_QUERY = """\
query ($id: ID!) {
  metafieldDefinition(id: $id) {
    key
    constraints {
      key
      values(first: 100) {
        edges { node { ... on MetafieldDefinitionConstraintValue { value } } }
      }
    }
  }
}
"""

_METAOBJECT_DEF_QUERY = """\
query ($id: ID!) {
  metaobjectDefinition(id: $id) { type }
}
"""

_METAOBJECTS_QUERY = """\
query metaobjects($type: String!, $after: String) {
  metaobjects(type: $type, first: 50, after: $after) {
    edges {
      node {
        id
        displayName
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def _discover_taxonomy_values(client, keys, product_category):
    """Discover metaobject GID maps for the given shopify.* metafield keys.

    Steps:
      1. Query metafieldDefinitions (ownerType=PRODUCT, namespace=shopify)
      2. For each key, check category constraints — skip if product_category not allowed
      3. Look up the metaobjectDefinition to get the type string
      4. Query metaobjects of that type to build {displayName: gid} map

    Returns: {key: {displayName: gid, ...}, ...}
    """
    # Step 1: Get metafield definitions
    data = client._graphql(_METAFIELD_DEFS_QUERY)
    defs = {}
    for edge in data['metafieldDefinitions']['edges']:
        node = edge['node']
        key = node['key']
        if key in keys:
            validations = {v['name']: v['value'] for v in node.get('validations', [])}
            defs[key] = {
                'def_id': node['id'],
                'mo_def_id': validations.get('metaobject_definition_id'),
            }

    result = {}
    for key in keys:
        info = defs.get(key)
        if not info or not info['mo_def_id']:
            logger.warning(f'shopify.{key}: no metafield definition found (skipping)')
            continue

        # Step 2: Check category constraints
        constraint_data = client._graphql(
            _METAFIELD_DEF_CONSTRAINTS_QUERY, {'id': info['def_id']},
        )
        constraints = constraint_data['metafieldDefinition'].get('constraints')
        if constraints and constraints.get('key') == 'category':
            allowed = {e['node']['value'] for e in constraints['values']['edges']}
            if product_category not in allowed:
                logger.info(
                    f'shopify.{key}: category {product_category} not in allowed '
                    f'categories ({len(allowed)} total) — skipping'
                )
                continue

        # Step 3: Get the type string from the metaobject definition
        def_data = client._graphql(_METAOBJECT_DEF_QUERY, {'id': info['mo_def_id']})
        mo_type = def_data['metaobjectDefinition']['type']

        # Step 4: Query all metaobjects of this type
        mapping = {}
        cursor = None
        while True:
            variables = {'type': mo_type}
            if cursor:
                variables['after'] = cursor
            mo_data = client._graphql(_METAOBJECTS_QUERY, variables)['metaobjects']
            for edge in mo_data['edges']:
                node = edge['node']
                mapping[node['displayName']] = node['id']
            if not mo_data['pageInfo']['hasNextPage']:
                break
            cursor = mo_data['pageInfo']['endCursor']

        result[key] = mapping

    return result


def _is_card_sku(sku):
    """True if SKU belongs to a card product (not pre-order)."""
    return (sku.startswith('OP-') or sku.startswith('PKMN-')) and not sku.endswith('-PO')


def _group_by_product(variants):
    """Group variants by product_id. Returns {product_id: [variant, ...]}."""
    by_product = {}
    for v in variants:
        by_product.setdefault(v['product_id'], []).append(v)
    return by_product


def run(dry_run=False, sku_filter=None):
    client = ShopifyClient()

    # Phase 1: Discover metaobject GIDs dynamically (respecting category constraints)
    print(f'Discovering taxonomy metaobject GIDs for category {PRODUCT_CATEGORY}...')
    taxonomy_maps = _discover_taxonomy_values(
        client, list(DESIRED_ATTRS.keys()), PRODUCT_CATEGORY,
    )

    # Validate GIDs — skip attributes not available for this category
    gids = {}
    for key, desired_name in DESIRED_ATTRS.items():
        value_map = taxonomy_maps.get(key)
        if value_map is None:
            print(f'  shopify.{key}: SKIPPED (not valid for category {PRODUCT_CATEGORY})')
            continue

        print(f'  shopify.{key}: {len(value_map)} values — {list(value_map.keys())}')

        if desired_name not in value_map:
            print(f'\nERROR: "{desired_name}" not found in shopify.{key} metaobjects.')
            print(f'  Available: {list(value_map.keys())}')
            sys.exit(1)

        gids[key] = value_map[desired_name]
        print(f'    → "{desired_name}" = {gids[key]}')

    if not gids:
        print('\nNo valid taxonomy attributes for this category. Nothing to do.')
        return

    # Phase 2: Fetch all products
    print('\nFetching all variants...')
    all_variants = client.get_all_variants()
    print(f'  {len(all_variants)} variants fetched')

    by_product = _group_by_product(all_variants)

    # Phase 3: Compute desired metafields per product
    metafields_to_set = []
    skipped_non_card = 0
    products_to_update = []

    for product_id, variants in by_product.items():
        card_variants = [v for v in variants if _is_card_sku(v['sku'])]
        if not card_variants:
            skipped_non_card += 1
            continue

        source = card_variants[0]

        if sku_filter and not any(v['sku'] == sku_filter for v in card_variants):
            continue

        for key, gid in gids.items():
            metafields_to_set.append({
                'ownerId': product_id,
                'namespace': 'shopify',
                'key': key,
                'value': json.dumps([gid]),
                'type': 'list.metaobject_reference',
            })

        products_to_update.append({
            'product_id': product_id,
            'sku': source['sku'],
            'title': source['title'],
        })

    print(f'\nProducts to update: {len(products_to_update)}')
    print(f'Metafields to set:  {len(metafields_to_set)}')
    print(f'Skipped (non-card): {skipped_non_card}')

    if not metafields_to_set:
        print('Nothing to do.')
        return

    if dry_run:
        print('\n--- DRY RUN (no changes) ---')
        attrs = ', '.join(f'{k}="{v}"' for k, v in DESIRED_ATTRS.items())
        for p in products_to_update:
            print(f"  {p['sku']:30s}  {p['title'][:50]}")
        print(f'\nWould set {len(metafields_to_set)} metafields ({attrs})')
        print(f'across {len(products_to_update)} products.')
        return

    # Phase 4: Batch set metafields
    print(f'\nSetting {len(metafields_to_set)} metafields in batches of 25...')
    results = client.set_metafields_batch(metafields_to_set)

    total_set = 0
    total_errors = 0
    for r in results:
        if r['success']:
            total_set += r['count']
        else:
            total_errors += 1
            for err in r['errors']:
                print(f'  ERROR batch {r["batch"]}: {err}')

    print(f'\nDone: {total_set} metafields set, {total_errors} failed batches')


def main():
    parser = argparse.ArgumentParser(
        description='Set category taxonomy attributes on Shopify card products',
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without mutating')
    parser.add_argument('--sku', type=str, default=None,
                        help='Only process the product with this SKU')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s %(name)s: %(message)s',
    )

    try:
        run(dry_run=args.dry_run, sku_filter=args.sku)
    except KeyboardInterrupt:
        print('\nAborted.')
        sys.exit(1)
    except Exception as e:
        logger.error(f'Fatal: {e}', exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
