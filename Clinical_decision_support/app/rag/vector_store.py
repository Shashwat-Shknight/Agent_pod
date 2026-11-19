"""ChromaDB vector store - only used by the optional sentence-transformer backend.

chromadb is imported lazily: the default TF-IDF backend never touches it, so the app runs on
machines where it is not installed (pip install -r requirements-ml.txt to enable it).
"""

from app.core.config import VECTOR_DB_PATH

COLLECTION_NAME = "medical_knowledge"
_client = None
_collection = None


def _get_client():
    global _client
    if _client is None:
        try:
            import chromadb
        except ImportError as error:
            raise RuntimeError(
                "chromadb is not installed. Use EMBEDDING_BACKEND=tfidf (default) or "
                "pip install -r requirements-ml.txt"
            ) from error
        _client = chromadb.PersistentClient(path=str(VECTOR_DB_PATH))
    return _client


def get_collection():
    global _collection
    if _collection is None:
        _collection = _get_client().get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    return _collection


def reset_collection():
    global _collection
    client = _get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001 - collection may not exist yet
        pass
    _collection = client.get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def store_chunks(chunks, embeddings, batch_size=250):
    collection = get_collection()
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        batch_embeddings = embeddings[start:start + batch_size]
        collection.upsert(
            # PDF chunks carry a "page"; Word chunks carry a "block" instead.
            ids=[f"{item['metadata']['source']}-"
                 f"{'p' + str(item['metadata']['page']) if 'page' in item['metadata'] else 'b' + str(item['metadata'].get('block', 0))}"
                 f"-c{start + offset}"
                 for offset, item in enumerate(batch)],
            documents=[item["text"] for item in batch],
            embeddings=batch_embeddings.tolist(),
            metadatas=[item["metadata"] for item in batch],
        )
