import re
import json

from requests import Session, HTTPError
from requests.cookies import cookiejar_from_dict
from urllib.parse import urljoin

from utils import extract_id
from block import Block, BLOCK_TYPES
from collection import Collection, CollectionView, CollectionRowBlock, COLLECTION_VIEW_TYPES
from settings import API_BASE_URL
from operations import operation_update_last_edited, build_operation
from store import RecordStore
from user import User
from space import Space


class NotionClient(object):
    """
    This is the entry point to using the API. Create an instance of this class, passing it the value of the
    "token_v2" cookie from a logged-in browser session on Notion.so. Most of the methods on here are primarily
    for internal use -- the main one you'll likely want to use is `get_block`.
    """

    def __init__(self, token_v2):
        self.session = Session()
        self.session.cookies = cookiejar_from_dict({"token_v2": token_v2})
        self.block_cache = {}
        self.user_cache = {}
        self.user_id = self.post("getUserAnalyticsSettings", {"platform": "web"}).json()["user_id"]
        self._store = RecordStore(self)

    def get_record_data(self, table, id, force_refresh=False):
        return self._store.get(table, id, force_refresh=force_refresh)

    def get_block(self, url_or_id, force_refresh=False):
        """
        Retrieve an instance of a subclass of Block that maps to the block/page identified by the URL or ID passed in.
        """
        block_id = extract_id(url_or_id)
        block = self.get_record_data("block", block_id, force_refresh=force_refresh)
        if not block:
            return None
        if block.get("parent_table") == "collection":
            block_class = CollectionRowBlock
        else:
            block_class = BLOCK_TYPES.get(block.get("type", ""), Block)
        return block_class(self, block_id)

    def get_collection(self, collection_id, force_refresh=False):
        """
        Retrieve an instance of Collection that maps to the collection identified by the ID passed in.
        """
        coll = self.get_record_data("collection", collection_id, force_refresh=force_refresh)
        return Collection(self, collection_id) if coll else None

    def get_user(self, user_id, force_refresh=False):
        """
        Retrieve an instance of User that maps to the notion_user identified by the ID passed in.
        """
        user = self.get_record_data("notion_user", user_id, force_refresh=force_refresh)
        return User(self, user_id) if user else None

    def get_space(self, space_id, force_refresh=False):
        """
        Retrieve an instance of Space that maps to the space identified by the ID passed in.
        """
        space = self.get_record_data("space", space_id, force_refresh=force_refresh)
        return Space(self, space_id) if space else None

    def get_collection_view(self, url_or_id, collection=None, force_refresh=False):
        """
        Retrieve an instance of a subclass of CollectionView that maps to the appropriate type.
        The `url_or_id` argument can either be the URL for a database page, or the ID of a collection_view (in which case
        you must also pass the collection)
        """
        # if it's a URL for a database page, try extracting the collection and view IDs
        if url_or_id.startswith("http"):
            match = re.search("([a-f0-9]{32})\?v=([a-f0-9]{32})", url_or_id)
            if not match:
                raise Exception("Invalid collection view URL")
            block_id, view_id = match.groups()
            collection = self.get_block(block_id, force_refresh=force_refresh).collection
        else:
            view_id = url_or_id
            assert collection is not None, "If 'url_or_id' is an ID (not a URL), you must also pass the 'collection'"

        view = self.get_record_data("collection_view", view_id, force_refresh=force_refresh)

        return COLLECTION_VIEW_TYPES.get(view.get("type", ""), CollectionView)(self, view_id, collection=collection) if view else None

    def refresh_records(self, **kwargs):
        """
        The keyword arguments map table names into lists of (or singular) record IDs to load for that table.
        Use True to refresh all known records for that table.
        """
        self._store.call_get_record_values(**kwargs)

    def post(self, endpoint, data):
        """
        All API requests on Notion.so are done as POSTs (except the websocket communications).
        """
        url = urljoin(API_BASE_URL, endpoint)
        response = self.session.post(url, json=data)
        if response.status_code == 400:
            print("Attempted to POST to {}, with data: {}".format(endpoint, json.dumps(data, indent=2)))
            raise HTTPError(response.json().get("message", "There was an error (400) submitting the request."))
        response.raise_for_status()
        return response

    def submit_transaction(self, operations, update_last_edited=True):

        if isinstance(operations, dict):
            operations = [operations]

        if update_last_edited:
            updated_blocks = set([op["id"] for op in operations if op["table"] == "block"])
            operations += [operation_update_last_edited(self.user_id, block_id) for block_id in updated_blocks]

        # if we're in a transaction, just add these operations to the list; otherwise, execute them right away
        if self.in_transaction():
            self._transaction_operations += operations
        else:
            data = {
                "operations": operations
            }
            return self.post("submitTransaction", data).json()

    def query_collection(self, *args, **kwargs):
        return self._store.call_query_collection(*args, **kwargs)

    def as_atomic_transaction(self):
        """
        Returns a context manager that buffers up all calls to `submit_transaction` and sends them as one big transaction
        when the context manager exits.
        """
        return Transaction(client=self)

    def in_transaction(self):
        """
        Returns True if we're currently in a transaction, otherwise False.
        """
        return hasattr(self, "_transaction_operations")

    def search_pages_with_parent(self, parent_id, search=""):

        data = {"query": search, "parentId": parent_id, "limit": 10000}

        response = self.post("searchPagesWithParent", data).json()

        self._store.store_recordmap(response["recordMap"])

        return [self.get_block(page_id) for page_id in response["results"]]


class Transaction(object):

    is_dummy_nested_transaction = False

    def __init__(self, client):
        self.client = client

    def __enter__(self):

        if hasattr(self.client, "_transaction_operations"):
            # client is already in a transaction, so we'll just make this one a nullop and let the outer one handle it
            self.is_dummy_nested_transaction = True
            return

        self.client._transaction_operations = []
        self.client._pages_to_refresh = []
        self.client._blocks_to_refresh = []

    def __exit__(self, exc_type, exc_value, traceback):

        if self.is_dummy_nested_transaction:
            return

        operations = self.client._transaction_operations
        del self.client._transaction_operations

        # only actually submit the transaction if there was no exception
        if not exc_type:
            self.client.submit_transaction(operations)

        self.client._store.handle_post_transaction_refreshing()

