"""Text embeddings for the knowledge base.

Default backend is TF-IDF (scikit-learn): fully offline, no model download, no Hugging Face.
The optional ``sentence_transformer`` backend needs ``pip install -r requirements-ml.txt`` and
Hugging Face access; it is imported lazily so the app runs without it.
"""

from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.config import EMBEDDING_MODEL

_model = None


def sentence_transformers_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def get_embedding_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "sentence-transformers is not installed. Use EMBEDDING_BACKEND=tfidf (default) or "
                "pip install -r requirements-ml.txt"
            ) from error
        _model = SentenceTransformer(EMBEDDING_MODEL)
    return _model


def generate_sentence_embeddings(texts, batch_size):
    return get_embedding_model().encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )


def generate_tfidf_embeddings(texts):
    vectorizer = create_vectorizer()
    return vectorizer, vectorizer.fit_transform(texts)


def create_vectorizer():
    """
    Create TF-IDF vectorizer for local semantic-like text retrieval.
    This avoids Hugging Face, Gemini, and external model downloads.
    """
    return TfidfVectorizer(
        stop_words="english",
        max_features=5000,
        ngram_range=(1, 2)
    )


def generate_embeddings(texts):
    """
    Kept for compatibility with build_kb.py.
    Returns TF-IDF matrix and vectorizer.
    """
    vectorizer = create_vectorizer()
    matrix = vectorizer.fit_transform(texts)

    return vectorizer, matrix
