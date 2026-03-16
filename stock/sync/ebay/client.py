"""eBay Trading API client for listing metadata sync.

Wraps GetMyeBaySelling (paginated listing fetch) and ReviseFixedPriceItem
(single-item metadata update). Reuses auth from pricing/push/ebay/ebay_auth.py.

Rate limiting: 5000 calls / 15 sec (conservative under eBay's 6000 limit).
"""

import sys
import os
import threading
import time
import xml.etree.ElementTree as ET

import requests

# Add pricing/push/ebay to path so we can import ebay_auth
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pricing', 'push', 'ebay'))
from ebay_auth import get_credentials, get_access_token


ENDPOINT = 'https://api.ebay.com/ws/api.dll'
NS = {'e': 'urn:ebay:apis:eBLBaseComponents'}


class RateLimiter:
    """Thread-safe token-bucket rate limiter. Sleep outside lock."""

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


class EbayClient:
    """eBay Trading API client for listing metadata operations."""

    def __init__(self, credentials=None, site_id='3'):
        self.credentials = credentials or get_credentials()
        self.site_id = site_id
        self.rate_limiter = RateLimiter(5000, 15)
        self.session = requests.Session()

    def _headers(self, call_name):
        return {
            'X-EBAY-API-SITEID': self.site_id,
            'X-EBAY-API-COMPATIBILITY-LEVEL': '1349',
            'X-EBAY-API-CALL-NAME': call_name,
            'X-EBAY-API-APP-NAME': self.credentials['app_id'],
            'X-EBAY-API-DEV-NAME': self.credentials['dev_id'],
            'X-EBAY-API-CERT-NAME': self.credentials['cert_id'],
            'Content-Type': 'text/xml',
        }

    def _auth_token(self):
        return get_access_token(self.credentials)

    def call(self, call_name, xml_body):
        """Make a Trading API call. Returns parsed XML Element."""
        self.rate_limiter.wait()
        headers = self._headers(call_name)
        response = self.session.post(ENDPOINT, headers=headers, data=xml_body, timeout=30)
        if response.status_code != 200:
            raise Exception(f"eBay API HTTP {response.status_code}: {response.text[:500]}")
        return ET.fromstring(response.text)

    def get_active_listings(self, page_size=200):
        """
        Fetch all active fixed-price listings via GetMyeBaySelling.

        Paginates automatically. Returns list of dicts:
        [{item_id, sku, title, description, item_specifics: {name: value}}]
        """
        listings = []
        page = 1

        while True:
            xml_body = self._build_get_my_ebay_selling_xml(page, page_size)
            root = self.call('GetMyeBaySelling', xml_body)

            ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
            if ack not in ('Success', 'Warning'):
                errors = []
                for err in root.findall('e:Errors', NS):
                    msg = err.findtext('e:LongMessage', namespaces=NS) or err.findtext('e:ShortMessage', namespaces=NS)
                    errors.append(msg)
                raise Exception(f"GetMyeBaySelling failed: {'; '.join(errors)}")

            active_list = root.find('e:ActiveList', NS)
            if active_list is None:
                break

            items = active_list.findall('e:ItemArray/e:Item', NS)
            if not items:
                break

            for item_elem in items:
                listing = self._parse_listing_element(item_elem)
                if listing:
                    listings.append(listing)

            # Check pagination
            pagination = active_list.find('e:PaginationResult', NS)
            if pagination is not None:
                total_pages = int(pagination.findtext('e:TotalNumberOfPages', default='1', namespaces=NS))
                total_entries = int(pagination.findtext('e:TotalNumberOfEntries', default='0', namespaces=NS))
                import logging
                logging.getLogger(__name__).info(
                    f"Pagination: page {page}/{total_pages}, "
                    f"{total_entries} total entries, {len(items)} items this page"
                )
                if page >= total_pages:
                    break
            else:
                import logging
                logging.getLogger(__name__).warning("No PaginationResult in response — stopping")
                break

            page += 1

        return listings

    def get_orders(self, create_time_from, create_time_to, order_status='Completed'):
        """Fetch orders via GetOrders Trading API.

        Args:
            create_time_from: ISO 8601 datetime string (e.g. '2026-02-10T00:00:00.000Z')
            create_time_to: ISO 8601 datetime string
            order_status: 'Completed', 'Active', or 'All' (default: Completed)

        Returns:
            List of {order_id, line_items: [{sku, quantity, price_gbp, item_id}]}
        """
        orders = []
        page = 1

        while True:
            xml_body = self._build_get_orders_xml(
                create_time_from, create_time_to, order_status, page)
            root = self.call('GetOrders', xml_body)

            ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
            if ack not in ('Success', 'Warning'):
                errors = []
                for err in root.findall('e:Errors', NS):
                    msg = (err.findtext('e:LongMessage', namespaces=NS)
                           or err.findtext('e:ShortMessage', namespaces=NS))
                    errors.append(msg)
                raise Exception(f"GetOrders failed: {'; '.join(errors)}")

            order_array = root.find('e:OrderArray', NS)
            if order_array is None:
                break

            for order_elem in order_array.findall('e:Order', NS):
                order = self._parse_order_element(order_elem)
                if order and order['line_items']:
                    orders.append(order)

            # Check pagination
            page_count = int(root.findtext(
                'e:PaginationResult/e:TotalNumberOfPages', default='1', namespaces=NS))
            if page >= page_count:
                break
            page += 1

        return orders

    def _build_get_orders_xml(self, create_time_from, create_time_to,
                               order_status, page, page_size=100):
        root = ET.Element('GetOrdersRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        ET.SubElement(root, 'CreateTimeFrom').text = create_time_from
        ET.SubElement(root, 'CreateTimeTo').text = create_time_to
        ET.SubElement(root, 'OrderStatus').text = order_status
        ET.SubElement(root, 'OrderRole').text = 'Seller'

        pagination = ET.SubElement(root, 'Pagination')
        ET.SubElement(pagination, 'EntriesPerPage').text = str(page_size)
        ET.SubElement(pagination, 'PageNumber').text = str(page)

        return ET.tostring(root, encoding='unicode', xml_declaration=True)

    def _parse_order_element(self, order_elem):
        """Parse an Order element from GetOrders into a dict."""
        order_id = order_elem.findtext('e:OrderID', default='', namespaces=NS)
        if not order_id:
            return None

        line_items = []
        txn_array = order_elem.find('e:TransactionArray', NS)
        if txn_array is not None:
            for txn in txn_array.findall('e:Transaction', NS):
                item_elem = txn.find('e:Item', NS)
                item_id = item_elem.findtext('e:ItemID', default='', namespaces=NS) if item_elem is not None else ''
                sku = item_elem.findtext('e:SKU', default='', namespaces=NS) if item_elem is not None else ''

                qty_text = txn.findtext('e:QuantityPurchased', default='0', namespaces=NS)
                quantity = int(qty_text)

                price_gbp = None
                price_elem = txn.find('e:TransactionPrice', NS)
                if price_elem is not None and price_elem.text:
                    try:
                        price_gbp = float(price_elem.text)
                    except ValueError:
                        pass

                if sku and quantity > 0:
                    line_items.append({
                        'sku': sku,
                        'quantity': quantity,
                        'price_gbp': price_gbp,
                        'item_id': item_id,
                    })

        return {
            'order_id': order_id,
            'line_items': line_items,
        }

    def get_item(self, item_id):
        """
        Fetch a single item's full details via GetItem.

        Returns dict with item_id, sku, title, description, item_specifics.
        """
        xml_body = self._build_get_item_xml(item_id)
        root = self.call('GetItem', xml_body)

        ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
        if ack not in ('Success', 'Warning'):
            errors = []
            for err in root.findall('e:Errors', NS):
                msg = err.findtext('e:LongMessage', namespaces=NS) or err.findtext('e:ShortMessage', namespaces=NS)
                errors.append(msg)
            raise Exception(f"GetItem failed for {item_id}: {'; '.join(errors)}")

        item_elem = root.find('e:Item', NS)
        if item_elem is None:
            return None
        return self._parse_item_detail(item_elem)

    def revise_item(self, item_id, title=None, description=None,
                    item_specifics=None, picture_urls=None, quantity=None):
        """
        Update listing metadata via ReviseFixedPriceItem.

        Only sends fields that are provided (non-None).
        quantity: For GTC listings, sets the desired available quantity
                  (eBay adds QuantitySold internally for total).
        picture_urls: full list of picture URLs (existing + new). eBay replaces all.
        Returns {ack, item_id, errors: [str]}.
        """
        xml_body = self._build_revise_xml(
            item_id, title, description, item_specifics, picture_urls, quantity)
        root = self.call('ReviseFixedPriceItem', xml_body)

        ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
        errors = []
        for err in root.findall('e:Errors', NS):
            severity = err.findtext('e:SeverityCode', namespaces=NS)
            msg = err.findtext('e:LongMessage', namespaces=NS) or err.findtext('e:ShortMessage', namespaces=NS)
            code = err.findtext('e:ErrorCode', namespaces=NS)
            errors.append(f"[{code}] {msg}" if code else msg)

        return {
            'ack': ack,
            'item_id': item_id,
            'errors': errors,
        }

    def revise_inventory_status(self, updates):
        """
        Batch-update quantity (and optionally price) via ReviseInventoryStatus.

        Args:
            updates: list of dicts with keys: item_id, quantity (total, not available).
                     Optionally: start_price.
        Returns:
            {ack, errors: [str], results: [{item_id, ack}]}

        Up to 4 items per call (eBay limit).
        """
        results = []
        # Process in batches of 4
        for i in range(0, len(updates), 4):
            batch = updates[i:i + 4]
            xml_body = self._build_revise_inventory_status_xml(batch)
            root = self.call('ReviseInventoryStatus', xml_body)

            ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
            errors = []
            for err in root.findall('e:Errors', NS):
                msg = err.findtext('e:LongMessage', namespaces=NS) or err.findtext('e:ShortMessage', namespaces=NS)
                code = err.findtext('e:ErrorCode', namespaces=NS)
                errors.append(f"[{code}] {msg}" if code else msg)

            for inv_status in root.findall('e:InventoryStatus', NS):
                item_id = inv_status.findtext('e:ItemID', namespaces=NS)
                qty = inv_status.findtext('e:Quantity', namespaces=NS)
                results.append({'item_id': item_id, 'quantity': qty})

            if ack not in ('Success', 'Warning') and errors:
                for e in errors:
                    print(f"  ERROR: {e}")

        return results

    def _build_revise_inventory_status_xml(self, batch):
        root = ET.Element('ReviseInventoryStatusRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        for update in batch:
            inv = ET.SubElement(root, 'InventoryStatus')
            ET.SubElement(inv, 'ItemID').text = str(update['item_id'])
            ET.SubElement(inv, 'Quantity').text = str(update['quantity'])
            if 'start_price' in update:
                ET.SubElement(inv, 'StartPrice').text = f"{update['start_price']:.2f}"

        return ET.tostring(root, encoding='unicode', xml_declaration=True)

    def upload_picture(self, image_url):
        """
        Upload an external image to eBay Picture Services via UploadSiteHostedPictures.

        Args:
            image_url: Public URL of the image to upload

        Returns:
            str: eBay-hosted URL for the uploaded image
        """
        xml_body = self._build_upload_picture_xml(image_url)
        root = self.call('UploadSiteHostedPictures', xml_body)

        ack = root.findtext('e:Ack', default='Failure', namespaces=NS)
        if ack not in ('Success', 'Warning'):
            errors = []
            for err in root.findall('e:Errors', NS):
                msg = err.findtext('e:LongMessage', namespaces=NS) or err.findtext('e:ShortMessage', namespaces=NS)
                errors.append(msg)
            raise Exception(f"UploadSiteHostedPictures failed: {'; '.join(errors)}")

        pic_set = root.find('e:SiteHostedPictureDetails', NS)
        if pic_set is not None:
            full_url = pic_set.findtext('e:FullURL', namespaces=NS)
            if full_url:
                return full_url

        raise Exception("UploadSiteHostedPictures: no FullURL in response")

    def _build_upload_picture_xml(self, image_url):
        root = ET.Element('UploadSiteHostedPicturesRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        ET.SubElement(root, 'ExternalPictureURL').text = image_url
        ET.SubElement(root, 'PictureName').text = 'condition-guide'

        return ET.tostring(root, encoding='unicode', xml_declaration=True)

    # ── XML builders ──────────────────────────────────────────────

    def _build_get_my_ebay_selling_xml(self, page, page_size):
        root = ET.Element('GetMyeBaySellingRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        active = ET.SubElement(root, 'ActiveList')
        sort = ET.SubElement(active, 'Sort')
        sort.text = 'TimeLeft'
        pagination = ET.SubElement(active, 'Pagination')
        ET.SubElement(pagination, 'EntriesPerPage').text = str(page_size)
        ET.SubElement(pagination, 'PageNumber').text = str(page)

        # Request detail level that includes item specifics
        detail = ET.SubElement(root, 'DetailLevel')
        detail.text = 'ReturnAll'

        # Output selector for fields we need
        for selector in ['ActiveList.ItemArray.Item.ItemID',
                         'ActiveList.ItemArray.Item.SKU',
                         'ActiveList.ItemArray.Item.Title',
                         'ActiveList.ItemArray.Item.Description',
                         'ActiveList.ItemArray.Item.ItemSpecifics',
                         'ActiveList.ItemArray.Item.Quantity',
                         'ActiveList.ItemArray.Item.SellingStatus.CurrentPrice',
                         'ActiveList.ItemArray.Item.SellingStatus.QuantitySold',
                         'ActiveList.PaginationResult']:
            el = ET.SubElement(root, 'OutputSelector')
            el.text = selector

        return ET.tostring(root, encoding='unicode', xml_declaration=True)

    def _build_get_item_xml(self, item_id):
        root = ET.Element('GetItemRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        ET.SubElement(root, 'ItemID').text = str(item_id)
        ET.SubElement(root, 'DetailLevel').text = 'ReturnAll'

        return ET.tostring(root, encoding='unicode', xml_declaration=True)

    def _build_revise_xml(self, item_id, title=None, description=None,
                          item_specifics=None, picture_urls=None, quantity=None):
        root = ET.Element('ReviseFixedPriceItemRequest')
        root.set('xmlns', 'urn:ebay:apis:eBLBaseComponents')

        creds = ET.SubElement(root, 'RequesterCredentials')
        token = ET.SubElement(creds, 'eBayAuthToken')
        token.text = self._auth_token()

        item_elem = ET.SubElement(root, 'Item')
        ET.SubElement(item_elem, 'ItemID').text = str(item_id)

        if quantity is not None:
            ET.SubElement(item_elem, 'Quantity').text = str(quantity)

        if title is not None:
            ET.SubElement(item_elem, 'Title').text = title

        # Description uses CDATA placeholder — replaced after serialization
        desc_placeholder = None
        if description is not None:
            desc_placeholder = f'__CDATA_DESC_{id(description)}__'
            ET.SubElement(item_elem, 'Description').text = desc_placeholder

        if item_specifics is not None:
            specifics_elem = ET.SubElement(item_elem, 'ItemSpecifics')
            for name, value in item_specifics.items():
                nv = ET.SubElement(specifics_elem, 'NameValueList')
                ET.SubElement(nv, 'Name').text = name
                ET.SubElement(nv, 'Value').text = str(value)

        if picture_urls is not None:
            pic_details = ET.SubElement(item_elem, 'PictureDetails')
            for url in picture_urls:
                ET.SubElement(pic_details, 'PictureURL').text = url

        xml_str = ET.tostring(root, encoding='unicode', xml_declaration=True)

        # Replace placeholder with CDATA-wrapped HTML
        if desc_placeholder and description is not None:
            xml_str = xml_str.replace(
                f'<Description>{desc_placeholder}</Description>',
                f'<Description><![CDATA[{description}]]></Description>',
            )

        return xml_str

    # ── Response parsers ──────────────────────────────────────────

    def _parse_listing_element(self, item_elem):
        """Parse an Item element from GetMyeBaySelling (summary level)."""
        item_id = item_elem.findtext('e:ItemID', namespaces=NS)
        if not item_id:
            return None

        sku = item_elem.findtext('e:SKU', default='', namespaces=NS)
        title = item_elem.findtext('e:Title', default='', namespaces=NS)

        # Description may not be in summary — will need GetItem for full details
        description = item_elem.findtext('e:Description', default='', namespaces=NS)

        item_specifics = self._parse_item_specifics(item_elem)

        # Quantity & price (for stock level sync)
        quantity = item_elem.findtext('e:Quantity', default='0', namespaces=NS)
        quantity_sold = 0
        current_price = None
        selling_status = item_elem.find('e:SellingStatus', NS)
        if selling_status is not None:
            quantity_sold = int(selling_status.findtext('e:QuantitySold', default='0', namespaces=NS))
            price_elem = selling_status.find('e:CurrentPrice', NS)
            if price_elem is not None and price_elem.text:
                current_price = float(price_elem.text)

        return {
            'item_id': item_id,
            'sku': sku,
            'title': title,
            'description': description,
            'item_specifics': item_specifics,
            'quantity': int(quantity),
            'quantity_sold': quantity_sold,
            'current_price': current_price,
        }

    def _parse_item_detail(self, item_elem):
        """Parse an Item element from GetItem (full detail level)."""
        item_id = item_elem.findtext('e:ItemID', namespaces=NS)
        sku = item_elem.findtext('e:SKU', default='', namespaces=NS)
        title = item_elem.findtext('e:Title', default='', namespaces=NS)
        description = item_elem.findtext('e:Description', default='', namespaces=NS)
        item_specifics = self._parse_item_specifics(item_elem)
        picture_urls = self._parse_picture_urls(item_elem)

        return {
            'item_id': item_id,
            'sku': sku,
            'title': title,
            'description': description,
            'item_specifics': item_specifics,
            'picture_urls': picture_urls,
        }

    def _parse_picture_urls(self, item_elem):
        """Extract PictureDetails/PictureURL list."""
        urls = []
        pic_details = item_elem.find('e:PictureDetails', NS)
        if pic_details is not None:
            for pic_url in pic_details.findall('e:PictureURL', NS):
                if pic_url.text:
                    urls.append(pic_url.text)
        return urls

    def _parse_item_specifics(self, item_elem):
        """Extract ItemSpecifics as {name: value} dict."""
        specifics = {}
        specifics_elem = item_elem.find('e:ItemSpecifics', NS)
        if specifics_elem is not None:
            for nv in specifics_elem.findall('e:NameValueList', NS):
                name = nv.findtext('e:Name', namespaces=NS)
                value = nv.findtext('e:Value', namespaces=NS)
                if name and value:
                    specifics[name] = value
        return specifics
