"""A minimal in-memory stand-in for the google.cloud.firestore client.

Only implements what agent.py actually uses: collection().document().get()/
update()/add(), collection().stream(), and collection().where(filter=...).
This lets the test suite exercise agent.py's real logic without any network
calls or GCP credentials.
"""


class FakeDocSnapshot:
    def __init__(self, doc_id, data, collection=None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self.reference = FakeDocRef(collection, doc_id) if collection is not None else None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, collection, doc_id):
        self._collection = collection
        self.id = doc_id

    def get(self):
        return FakeDocSnapshot(self.id, self._collection._docs.get(self.id), self._collection)

    def set(self, data):
        self._collection._docs[self.id] = dict(data)

    def update(self, data):
        existing = self._collection._docs.setdefault(self.id, {})
        existing.update(data)

    def delete(self):
        self._collection._docs.pop(self.id, None)


class FakeQuery:
    def __init__(self, collection, items):
        self._collection = collection
        self._items = items  # list of (doc_id, data)

    def where(self, filter=None):
        field, op, value = filter.field_path, filter.op_string, filter.value

        def matches(data):
            actual = data.get(field)
            if op == ">=":
                return actual is not None and actual >= value
            if op == "==":
                return actual == value
            raise NotImplementedError(f"Unsupported op {op!r} in fake Firestore")

        return FakeQuery(self._collection, [(i, d) for i, d in self._items if matches(d)])

    def stream(self):
        return [
            FakeDocSnapshot(doc_id, data, self._collection) for doc_id, data in self._items
        ]


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self._docs = {}
        self._auto_id_counter = 0

    def document(self, doc_id=None):
        if doc_id is None:
            self._auto_id_counter += 1
            doc_id = f"auto_{self._auto_id_counter}"
        return FakeDocRef(self, doc_id)

    def add(self, data):
        self._auto_id_counter += 1
        doc_id = f"auto_{self._auto_id_counter}"
        self._docs[doc_id] = dict(data)
        return (None, FakeDocRef(self, doc_id))

    def stream(self):
        return [FakeDocSnapshot(doc_id, data, self) for doc_id, data in self._docs.items()]

    def where(self, filter=None):
        return FakeQuery(self, list(self._docs.items())).where(filter=filter)


class FakeFirestoreClient:
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollection(name)
        return self._collections[name]

    def seed(self, collection_name, doc_id, data):
        """Test helper: directly insert a document."""
        self.collection(collection_name)._docs[doc_id] = dict(data)
