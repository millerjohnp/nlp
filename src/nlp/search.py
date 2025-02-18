import logging
import os
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Union

import numpy as np
from tqdm.auto import tqdm


if TYPE_CHECKING:
    from .arrow_dataset import Dataset  # noqa: F401


try:
    import elasticsearch as es
    from elasticsearch import Elasticsearch
    import elasticsearch.helpers

    _has_elasticsearch = True
except ImportError:
    _has_elasticsearch = False

try:
    import faiss

    _has_faiss = True
except ImportError:
    _has_faiss = False


logger = logging.getLogger(__name__)


class MissingIndex(Exception):
    pass


SearchResults = NamedTuple("SearchResults", [("scores", List[float]), ("indices", List[int])])
BatchedSearchResults = NamedTuple(
    "BatchedSearchResults", [("total_scores", List[List[float]]), ("total_indices", List[List[int]])]
)

NearestExamplesResults = NamedTuple("NearestExamplesResults", [("scores", List[float]), ("examples", List[dict])])
BatchedNearestExamplesResults = NamedTuple(
    "BatchedNearestExamplesResults", [("total_scores", List[List[float]]), ("total_examples", List[List[dict]])]
)


class BaseIndex:
    """Base class for indexing"""

    def search(self, query, k: int = 10) -> SearchResults:
        """
        To implement.
        This method has to return the scores and the indices of the retrieved examples given a certain query.
        """
        raise NotImplementedError

    def search_batch(self, queries, k: int = 10) -> BatchedSearchResults:
        """ Find the nearest examples indices to the query.

            Args:
                `queries` (`Union[List[str], np.ndarray]`): The queries as a list of strings if `column` is a text index or as a numpy array if `column` is a vector index.
                `k` (`int`): The number of examples to retrieve per query.

            Ouput:
                `total_scores` (`List[List[float]`): The retrieval scores of the retrieved examples per query.
                `total_indices` (`List[List[int]]`): The indices of the retrieved examples per query.
        """
        total_scores, total_indices = [], []
        for query in queries:
            scores, indices = self.search(query, k)
            total_scores.append(scores)
            total_indices.append(indices)
        return BatchedSearchResults(total_scores, total_indices)

    def save(self, file: str):
        """Serialize the index on disk"""
        raise NotImplementedError

    @classmethod
    def load(cls, file: str) -> "BaseIndex":
        """Deserialize the index from disk"""
        raise NotImplementedError


class ElasticSearchIndex(BaseIndex):
    """
    Sparse index using Elasticsearch. It is used to index text and run queries based on BM25 similarity.
    An Elasticsearch server needs to be accessible, and a python client is declared with
    ```
    es_client = Elasticsearch([{'host': 'localhost', 'port': '9200'}])
    ```
    for example.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        es_client: Optional["Elasticsearch"] = None,
        es_index_name: Optional[str] = None,
        es_index_config: Optional[dict] = None,
    ):
        assert (
            _has_elasticsearch
        ), "You must install ElasticSearch to use ElasticSearchIndex. To do so you can run `pip install elasticsearch==7.7.1 for example`"
        assert es_client is None or (
            host is None and port is None
        ), "Please specify either `es_client` or `(host, port)`, but not both."
        host = host or "localhost"
        port = port or 9200
        self.es_client = es_client if es_client is not None else Elasticsearch([{"host": host, "port": str(port)}])
        self.es_index_name = (
            es_index_name
            if es_index_name is not None
            else "huggingface_nlp_" + os.path.basename(tempfile.NamedTemporaryFile().name)
        )
        self.es_index_config = (
            es_index_config
            if es_index_config is not None
            else {
                "settings": {
                    "number_of_shards": 1,
                    "analysis": {"analyzer": {"stop_standard": {"type": "standard", " stopwords": "_english_"}}},
                },
                "mappings": {"properties": {"text": {"type": "text", "analyzer": "standard", "similarity": "BM25"}}},
            }
        )

    def add_documents(self, documents: Union[List[str], "Dataset"], column: Optional[str] = None):
        """
        Add documents to the index.
        If the documents are inside a certain column, you can specify it using the `column` argument.
        """
        # TODO: don't rebuild if it already exists
        index_name = self.es_index_name
        index_config = self.es_index_config
        self.es_client.indices.create(index=index_name, body=index_config)
        number_of_docs = len(documents)
        progress = tqdm(unit="docs", total=number_of_docs)
        successes = 0

        def passage_generator():
            if column is not None:
                for i, example in enumerate(documents):
                    yield {"text": example[column], "_id": i}
            else:
                for i, example in enumerate(documents):
                    yield {"text": example, "_id": i}

        # create the ES index
        for ok, action in es.helpers.streaming_bulk(
            client=self.es_client, index=index_name, actions=passage_generator(),
        ):
            progress.update(1)
            successes += ok
        if successes != len(documents):
            logging.warning(
                f"Some documents failed to be added to ElasticSearch. Failures: {len(documents)-successes}/{len(documents)}"
            )
        logger.info("Indexed %d documents" % (successes,))

    def search(self, query: str, k=10) -> SearchResults:
        """ Find the nearest examples indices to the query.

            Args:
                `query` (`str`): The query as a string.
                `k` (`int`): The number of examples to retrieve.

            Ouput:
                `scores` (`List[List[float]`): The retrieval scores of the retrieved examples.
                `indices` (`List[List[int]]`): The indices of the retrieved examples.
        """
        response = self.es_client.search(
            index=self.es_index_name,
            body={"query": {"multi_match": {"query": query, "fields": ["text"], "type": "cross_fields"}}, "size": k},
        )
        hits = response["hits"]["hits"]
        return SearchResults([hit["_score"] for hit in hits], [hit["_id"] for hit in hits])


@dataclass
class FaissGpuOptions:
    """
    Options to specify the GPU resources for Faiss.
    You can use them for multi-GPU settings for example.
    More info at https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU
    """

    resource_vec: Any
    device_vec: Any
    cloner_options: Any


class FaissIndex(BaseIndex):
    """
    Dense index using Faiss. It is used to index vectors.
    Faiss is a library for efficient similarity search and clustering of dense vectors.
    It contains algorithms that search in sets of vectors of any size, up to ones that possibly do not fit in RAM.
    You can find more information about Faiss here:
    - For index types and the string factory: https://github.com/facebookresearch/faiss/wiki/The-index-factory
    - For GPU settings: https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU
    """

    def __init__(
        self,
        device: Optional[int] = None,
        string_factory: Optional[str] = None,
        faiss_gpu_options: Optional[FaissGpuOptions] = None,
    ):
        """
        Create a Dense index using Faiss. You can specify `device` if you want to run it on GPU (`device` must be the GPU index).
        You can find more information about Faiss here:
        - For `string factory`: https://github.com/facebookresearch/faiss/wiki/The-index-factory
        - For `faiss_gpu_options`'s resource_vec, device_vec and cloner_options: https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU
        """
        assert not (
            device is not None and faiss_gpu_options is not None
        ), "Please specify either `device` or `faiss_gpu_options` but not both."
        self.device: int = device if device is not None else -1
        self.string_factory: Optional[str] = string_factory
        self.faiss_gpu_options: Optional[FaissGpuOptions] = faiss_gpu_options
        self.faiss_index = None
        assert (
            _has_faiss
        ), "You must install Faiss to use FaissIndex. To do so you can run `pip install faiss-cpu` or `pip install faiss-gpu`"

    def add_vectors(self, vectors: Union[np.array, "Dataset"], column: Optional[str] = None, batch_size=1000):
        """
        Add vectors to the index.
        If the arrays are inside a certain column, you can specify it using the `column` argument.
        """
        if self.faiss_index is None:
            size = len(vectors[0]) if column is None else len(vectors[0][column])
            if self.string_factory is not None:
                index = faiss.index_factory(size, self.string_factory)
            else:
                index = faiss.IndexFlatIP(size)
            if self.is_on_gpu():
                index = self._to_gpu(index)
            self.faiss_index = index
        for i in range(0, len(vectors), batch_size):
            vecs = vectors[i : i + batch_size] if column is None else vectors[i : i + batch_size][column]
            self.faiss_index.add(vecs)

    def search(self, query: np.array, k=10) -> SearchResults:
        """ Find the nearest examples indices to the query.

            Args:
                `query` (`np.array`): The query as a numpy array.
                `k` (`int`): The number of examples to retrieve.

            Ouput:
                `scores` (`List[List[float]`): The retrieval scores of the retrieved examples.
                `indices` (`List[List[int]]`): The indices of the retrieved examples.
        """
        assert len(query.shape) == 1 or (len(query.shape) == 2 and query.shape[0] == 1)
        queries = query.reshape(1, -1)
        if not queries.flags.c_contiguous:
            queries = np.asarray(queries, order="C")
        scores, indices = self.faiss_index.search(queries, k)
        return SearchResults(scores[0], indices[0].astype(int))

    def search_batch(self, queries: np.array, k=10) -> BatchedSearchResults:
        """ Find the nearest examples indices to the queries.

            Args:
                `queries` (`np.array`): The queries as a numpy array.
                `k` (`int`): The number of examples to retrieve.

            Ouput:
                `total_scores` (`List[List[float]`): The retrieval scores of the retrieved examples per query.
                `total_indices` (`List[List[int]]`): The indices of the retrieved examples per query.
        """
        assert len(queries.shape) == 2
        if not queries.flags.c_contiguous:
            queries = np.asarray(queries, order="C")
        scores, indices = self.faiss_index.search(queries, k)
        return BatchedSearchResults(scores, indices.astype(int))

    def _to_gpu(self, index):
        if self.device > -1:
            self.faiss_res = faiss.StandardGpuResources()
            return faiss.index_cpu_to_gpu(self.faiss_res, self.device, index)
        elif self.faiss_gpu_options is not None:
            return faiss.index_cpu_to_gpu_multiple(
                self.faiss_gpu_options.resource_vec,
                self.faiss_gpu_options.device_vec,
                index,
                self.faiss_gpu_options.cloner_options,
            )
        else:
            return index

    def is_on_gpu(self):
        return self.device > -1 or self.faiss_gpu_options is not None

    def save(self, file: str):
        """Serialize the FaissIndex on disk"""
        if self.is_on_gpu():
            index = faiss.index_gpu_to_cpu(self.faiss_index)
        else:
            index = self.faiss_index
        faiss.write_index(index, file)

    @classmethod
    def load(
        cls,
        file: str,
        device: Optional[int] = None,
        string_factory: Optional[str] = None,
        faiss_gpu_options: Optional[FaissGpuOptions] = None,
    ) -> "FaissIndex":
        """Deserialize the FaissIndex from disk"""
        faiss_index = cls(device=device, string_factory=string_factory, faiss_gpu_options=faiss_gpu_options)
        index = faiss.read_index(file)
        if faiss_index.is_on_gpu():
            index = faiss_index._to_gpu(index)
        faiss_index.faiss_index = index
        return faiss_index


class IndexableMixin:
    """Add indexing features to `nlp.Dataset`"""

    def __init__(self):
        self._indexes: Dict[str, BaseIndex] = {}

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, key):
        raise NotImplementedError

    def is_index_initialized(self, index_name: str) -> bool:
        return index_name in self._indexes

    def _check_index_is_initialized(self, index_name: str):
        if not self.is_index_initialized(index_name):
            raise MissingIndex(
                f"Index with index_name '{index_name}' not initialized yet. Please make sure that you call `add_faiss_index` or `add_elasticsearch_index` first."
            )

    def list_indexes(self) -> List[str]:
        """List the colindex_nameumns/identifiers of all the attached indexes."""
        return list(self._indexes)

    def get_index(self, index_name: str) -> BaseIndex:
        """List the index_name/identifiers of all the attached indexes."""
        self._check_index_is_initialized(index_name)
        return self._indexes[index_name]

    def add_faiss_index(
        self,
        column: str,
        index_name: Optional[str] = None,
        device: Optional[int] = None,
        string_factory: Optional[str] = None,
        faiss_gpu_options: Optional[FaissGpuOptions] = None,
    ):
        """ Add a dense index using Faiss for fast retrieval.
            The index is created using the vectors of the specified column.
            You can specify `device` if you want to run it on GPU (`device` must be the GPU index).
            You can find more information about Faiss here:
            - For `string factory`: https://github.com/facebookresearch/faiss/wiki/The-index-factory
            - For `faiss_gpu_options`'s resource_vec, device_vec and cloner_options: https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU

            Args:
                `column` (`str`): The column of the vectors to add to the index.
                `index_name` (Optional `str`): The index_name/identifier of the index. This is the index_name that is used to call `.get_nearest` or `.search`.
                    By defaul it corresponds to `column`.
                `device` (Optional `int`): If not None, this is the index of the GPU to use. By default it uses the CPU.
                `string_factory` (Optional `str`): This is passed to the index factory of Faiss to create the index. Default index class is IndexFlatIP.
                `faiss_gpu_options` (Optional `FaissGpuOptions`): Options to configure the GPU resources of Faiss.
        """
        index_name = index_name if index_name is not None else column
        self._indexes[index_name] = FaissIndex(device, string_factory, faiss_gpu_options)
        self._indexes[index_name].add_vectors(self, column=column)

    def add_faiss_index_from_external_arrays(
        self,
        external_arrays: np.array,
        index_name: str,
        device: Optional[int] = None,
        string_factory: Optional[str] = None,
        faiss_gpu_options: Optional[FaissGpuOptions] = None,
    ):
        """ Add a dense index using Faiss for fast retrieval.
            The index is created using the vectors of `external_arrays`.
            You can specify `device` if you want to run it on GPU (`device` must be the GPU index).
            You can find more information about Faiss here:
            - For `string factory`: https://github.com/facebookresearch/faiss/wiki/The-index-factory
            - For `faiss_gpu_options`'s resource_vec, device_vec and cloner_options: https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU

            Args:
                `external_arrays` (`np.array`): If you want to use arrays from outside the lib for the index, you can set `external_arrays`.
                    It will use `external_arrays` to create the Faiss index instead of the arrays in the given `column`.
                `index_name` (`str`): The index_name/identifier of the index. This is the index_name that is used to call `.get_nearest` or `.search`.
                `device` (Optional `int`): If not None, this is the index of the GPU to use. By default it uses the CPU.
                `string_factory` (Optional `str`): This is passed to the index factory of Faiss to create the index. Default index class is IndexFlatIP.
                `faiss_gpu_options` (Optional `FaissGpuOptions`): Options to configure the GPU resources of Faiss.
        """
        self._indexes[index_name] = FaissIndex(device, string_factory, faiss_gpu_options)
        self._indexes[index_name].add_vectors(external_arrays, column=None)

    def save_faiss_index(self, index_name: str, file: str):
        """Save a FaissIndex on disk

            Args:
                `index_name` (`str`): The index_name/identifier of the index. This is the index_name that is used to call `.get_nearest` or `.search`.
                `file` (`str`): The path to the serialized faiss index on disk.
        """
        index = self.get_index(index_name)
        if not isinstance(index, FaissIndex):
            raise ValueError("Index '{}' is not a FaissIndex but a '{}'".format(index_name, type(index)))
        index.save(file)
        logger.info("Saved FaissIndex {} at {}".format(index_name, file))

    def load_faiss_index(
        self,
        index_name: str,
        file: str,
        device: Optional[int] = None,
        string_factory: Optional[str] = None,
        faiss_gpu_options: Optional[FaissGpuOptions] = None,
    ):
        """Load a FaissIndex from disk

            Args:
                `index_name` (`str`): The index_name/identifier of the index. This is the index_name that is used to call `.get_nearest` or `.search`.
                `file` (`str`): The path to the serialized faiss index on disk.
                `device` (Optional `int`): If not None, this is the index of the GPU to use. By default it uses the CPU.
                `string_factory` (Optional `str`): This is passed to the index factory of Faiss to create the index. Default index class is IndexFlatIP.
                `faiss_gpu_options` (Optional `FaissGpuOptions`): Options to configure the GPU resources of Faiss.
        """
        index = FaissIndex.load(file)
        assert index.faiss_index.ntotal == len(
            self
        ), "Index size should match Dataset size, but Index '{}' at {} has {} elements while the dataset has {} examples.".format(
            index_name, file, index.faiss_index.ntotal, len(self)
        )
        self._indexes[index_name] = index
        logger.info("Loaded FaissIndex {} from {}".format(index_name, file))

    def add_elasticsearch_index(
        self,
        column: str,
        index_name: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        es_client: Optional["Elasticsearch"] = None,
        es_index_name: Optional[str] = None,
        es_index_config: Optional[dict] = None,
    ):
        """ Add a text index using ElasticSearch for fast retrieval.

            Args:
                `column` (`str`): The column of the documents to add to the index.
                `index_name` (Optional `str`): The index_name/identifier of the index. This is the index name that is used to call `.get_nearest` or `.search`.
                    By defaul it corresponds to `column`.
                `documents` (`Union[List[str], nlp.Dataset]`): The documents to index. It can be a `nlp.Dataset`.
                `es_client` (`elasticsearch.Elasticsearch`): The elasticsearch client used to create the index.
                `es_index_name` (Optional `str`): The elasticsearch index name used to create the index.
                `es_index_config` (Optional `dict`): The configuration of the elasticsearch index.
                    Default config is
                    {
                        "settings": {
                            "number_of_shards": 1,
                            "analysis": {"analyzer": {"stop_standard": {"type": "standard", " stopwords": "_english_"}}},
                        },
                        "mappings": {
                            "properties": {
                                "text": {"type": "text", "analyzer": "standard", "similarity": "BM25"},
                            }
                        },
                    }
        """
        index_name = index_name if index_name is not None else column
        self._indexes[index_name] = ElasticSearchIndex(host, port, es_client, es_index_name, es_index_config)
        self._indexes[index_name].add_documents(self, column=column)

    def drop_index(self, index_name: str):
        """ Drop the index with the specified column.

            Args:
                `index_name` (`str`): The index_name/identifier of the index.
        """
        del self._indexes[index_name]

    def search(self, index_name: str, query: Union[str, np.array], k: int = 10) -> SearchResults:
        """ Find the nearest examples indices in the dataset to the query.

            Args:
                `index_name` (`str`): The name/identifier of the index.
                `query` (`Union[str, np.ndarray]`): The query as a string if `index_name` is a text index or as a numpy array if `index_name` is a vector index.
                `k` (`int`): The number of examples to retrieve.

            Ouput:
                `scores` (`List[List[float]`): The retrieval scores of the retrieved examples.
                `indices` (`List[List[int]]`): The indices of the retrieved examples.
        """
        self._check_index_is_initialized(index_name)
        return self._indexes[index_name].search(query, k)

    def search_batch(self, index_name: str, queries: Union[List[str], np.array], k: int = 10) -> BatchedSearchResults:
        """ Find the nearest examples indices in the dataset to the query.

            Args:
                `index_name` (`str`): The index_name/identifier of the index.
                `queries` (`Union[List[str], np.ndarray]`): The queries as a list of strings if `index_name` is a text index or as a numpy array if `index_name` is a vector index.
                `k` (`int`): The number of examples to retrieve per query.

            Ouput:
                `total_scores` (`List[List[float]`): The retrieval scores of the retrieved examples per query.
                `total_indices` (`List[List[int]]`): The indices of the retrieved examples per query.
        """
        self._check_index_is_initialized(index_name)
        return self._indexes[index_name].search_batch(queries, k)

    def get_nearest_examples(
        self, index_name: str, query: Union[str, np.array], k: int = 10
    ) -> NearestExamplesResults:
        """ Find the nearest examples in the dataset to the query.

            Args:
                `index_name` (`str`): The index_name/identifier of the index.
                `query` (`Union[str, np.ndarray]`): The query as a string if `index_name` is a text index or as a numpy array if `index_name` is a vector index.
                `k` (`int`): The number of examples to retrieve.

            Ouput:
                `scores` (`List[List[float]`): The retrieval scores of the retrieved examples.
                `examples` (`List[List[dict]]`): The retrieved examples.
        """
        self._check_index_is_initialized(index_name)
        scores, indices = self.search(index_name, query, k)
        return NearestExamplesResults(scores, [self[int(i)] for i in indices])

    def get_nearest_examples_batch(
        self, index_name: str, queries: Union[List[str], np.array], k: int = 10
    ) -> BatchedNearestExamplesResults:
        """ Find the nearest examples in the dataset to the query.

            Args:
                `index_name` (`str`): The index_name/identifier of the index.
                `queries` (`Union[List[str], np.ndarray]`): The queries as a list of strings if `index_name` is a text index or as a numpy array if `index_name` is a vector index.
                `k` (`int`): The number of examples to retrieve per query.

            Ouput:
                `total_scores` (`List[List[float]`): The retrieval scores of the retrieved examples per query.
                `total_examples` (`List[List[dict]]`): The retrieved examples per query.
        """
        self._check_index_is_initialized(index_name)
        total_scores, total_indices = self.search_batch(index_name, queries, k)
        return BatchedNearestExamplesResults(
            total_scores, [[self[int(i)] for i in indices] for indices in total_indices]
        )
