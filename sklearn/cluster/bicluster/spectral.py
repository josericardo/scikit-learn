"""Implements spectral biclustering algorithms.

Authors : Kemal Eren
License: BSD 3 clause

"""
from abc import ABCMeta

import numpy as np

from scipy.sparse import dia_matrix
from scipy.sparse import issparse

from sklearn.base import BaseEstimator, BiclusterMixin
from sklearn.externals import six
from sklearn.utils.arpack import svds
from sklearn.cluster import KMeans
from sklearn.cluster import MiniBatchKMeans

from sklearn.utils.extmath import randomized_svd
from sklearn.utils.extmath import safe_sparse_dot
from sklearn.utils.extmath import make_nonnegative
from sklearn.utils.extmath import norm

from sklearn.utils.validation import assert_all_finite
from sklearn.utils.validation import check_arrays

from .utils import check_array_ndim


def _scale_normalize(X):
    """Normalize `X` by scaling rows and columns independently.

    Returns the normalized matrix and the row and column scaling
    factors.

    """
    X = make_nonnegative(X)
    row_diag = np.asarray(1.0 / np.sqrt(X.sum(axis=1))).squeeze()
    col_diag = np.asarray(1.0 / np.sqrt(X.sum(axis=0))).squeeze()
    row_diag = np.where(np.isnan(row_diag), 0, row_diag)
    col_diag = np.where(np.isnan(col_diag), 0, col_diag)
    if issparse(X):
        n_rows, n_cols = X.shape
        r = dia_matrix((row_diag, [0]), shape=(n_rows, n_rows))
        c = dia_matrix((col_diag, [0]), shape=(n_cols, n_cols))
        an = r * X * c
    else:
        an = row_diag[:, np.newaxis] * X * col_diag
    return an, row_diag, col_diag


def _bistochastic_normalize(X, maxiter=1000, tol=1e-5):
    """Normalize rows and columns of `X` simultaneously so that all
    rows sum to one constant and all columns sum to a different
    constant.

    """
    # According to paper, this can also be done more efficiently with
    # deviation reduction and balancing algorithms.
    X = make_nonnegative(X)
    X_scaled = X
    dist = None
    for _ in range(maxiter):
        X_new, _, _ = _scale_normalize(X_scaled)
        if issparse(X):
            dist = norm(X_scaled.data - X.data)
        else:
            dist = norm(X_scaled - X_new)
        X_scaled = X_new
        if dist is not None and dist < tol:
            break
    return X_scaled


def _log_normalize(X):
    """Normalize `X` according to Kluger's log-interactions scheme."""
    X = make_nonnegative(X, min_value=1)
    if issparse(X):
        raise ValueError("Cannot compute log of a sparse matrix,"
                         " because log(x) diverges to -infinity as x"
                         " goes to 0.")
    L = np.log(X)
    row_avg = L.mean(axis=1)[:, np.newaxis]
    col_avg = L.mean(axis=0)
    avg = L.mean()
    return L - row_avg - col_avg + avg


class BaseSpectral(six.with_metaclass(ABCMeta, BaseEstimator,
                                      BiclusterMixin)):
    """Base class for spectral biclustering."""

    def __init__(self, n_clusters=3, svd_method="randomized",
                 n_svd_vecs=None, mini_batch=False, init="k-means++",
                 n_init=10, n_jobs=1, random_state=None):
        self.n_clusters = n_clusters
        self.svd_method = svd_method
        self.n_svd_vecs = n_svd_vecs
        self.mini_batch = mini_batch
        self.init = init
        self.n_init = n_init
        self.n_jobs = n_jobs
        self.random_state = random_state

    def _check_parameters(self):
        legal_svd_methods = ('randomized', 'arpack')
        if self.svd_method not in legal_svd_methods:
            raise ValueError("Unknown SVD method: '{}'. `svd_method` must be"
                             " one of {}.".format(self.svd_method,
                                                  legal_svd_methods))

    def fit(self, X):
        """Creates a biclustering for X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)

        """
        X, = check_arrays(X, sparse_format='csr', dtype=np.float64)
        check_array_ndim(X)
        self._check_parameters()
        self._fit(X)

    def _svd(self, array, n_components, n_discard):
        """Returns first `n_components` left and right singular
        vectors u and v, discarding the first `n_discard`.

        """
        if self.svd_method == 'randomized':
            kwargs = {}
            if self.n_svd_vecs is not None:
                kwargs['n_oversamples'] = self.n_svd_vecs
            u, _, vt = randomized_svd(array, n_components,
                                      random_state=self.random_state,
                                      **kwargs)

        elif self.svd_method == 'arpack':
            u, _, vt = svds(array, k=n_components, ncv=self.n_svd_vecs)

        assert_all_finite(u)
        assert_all_finite(vt)
        u = u[:, n_discard:]
        vt = vt[n_discard:]
        return u, vt.T

    def _k_means(self, data, n_clusters):
        if self.mini_batch:
            model = MiniBatchKMeans(n_clusters,
                                    init=self.init,
                                    n_init=self.n_init,
                                    random_state=self.random_state)
        else:
            model = KMeans(n_clusters, init=self.init,
                           n_init=self.n_init, n_jobs=self.n_jobs,
                           random_state=self.random_state)
        model.fit(data)
        centroid = model.cluster_centers_
        labels = model.labels_
        return centroid, labels


class SpectralCoclustering(BaseSpectral):
    """Spectral Co-Clustering algorithm (Dhillon, 2001).

    Clusters rows and columns of an array `X` to solve the relaxed
    normalized cut of the bipartite graph created from `X` as follows:
    the edge between row vertex `i` and column vertex `j` has weight
    `X[i, j]`.

    The resulting bicluster structure is block-diagonal, since each
    row and each column belongs to exactly one bicluster.

    Supports sparse matrices, as long as they are nonnegative.

    Parameters
    ----------
    n_clusters : integer
        The number of biclusters to find.

    svd_method : string, optional, default: 'randomized'
        Selects the algorithm for finding singular vectors. May be
        'randomized' or 'arpack'. If 'randomized', use
        `sklearn.utils.extmath.randomized_svd`, which may be faster
        for large matrices. If 'arpack', use
        `sklearn.utils.arpack.svds`, which is more accurate, but
        possibly slower in some cases.

    n_svd_vecs : int, optional, default: None
        Number of vectors to use in calculating the SVD. Corresponds
        to `ncv` when `svd_method=arpack` and `n_oversamples` when
        `svd_method` is 'randomized`.

    mini_batch : bool, optional, default: False
        Whether to use mini-batch k-means, which is faster but may get
        different results.

    init : {'k-means++', 'random' or an ndarray}
         Method for initialization of k-means algorithm; defaults to
         'k-means++

    n_init : int, optional, default: 10
        Number of random initializations that are tried with the
        k-means algorithm.

        If mini-batch k-means is used, the best initialization is
        chosen and the algorithm runs once. Otherwise, the algorithm
        is run for each initialization and the best solution chosen.

    n_jobs : int, optional, default: 1
        The number of jobs to use for the computation. This works by breaking
        down the pairwise matrix into n_jobs even slices and computing them in
        parallel.

        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debuging. For n_jobs below -1,
        (n_cpus + 1 + n_jobs) are used. Thus for n_jobs = -2, all CPUs but one
        are used.

    random_state : int seed, RandomState instance, or None (default)
        A pseudo random number generator used by the K-Means
        initialization.

    Attributes
    ----------
    `rows_` : array-like, shape (n_row_clusters, n_rows)
        Results of the clustering. `rows[i, r]` is True if cluster `i`
        contains row `r`. Available only after calling ``fit``.

    `columns_` : array-like, shape (n_column_clusters, n_columns)
        Results of the clustering, like `rows`.

    `row_labels_` : array-like, shape (n_rows,)
        The bicluster label of each row.

    `column_labels_` : array-like, shape (n_cols,)
        The bicluster label of each column.

    References
    ----------

    * Dhillon, Inderjit S, 2001. `Co-clustering documents and words using
      bipartite spectral graph partitioning
      <http://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.140.3011>`__.

    """
    def __init__(self, n_clusters=3, svd_method='randomized',
                 n_svd_vecs=None, mini_batch=False, init='k-means++',
                 n_init=10, n_jobs=1, random_state=None):
        super(SpectralCoclustering, self).__init__(n_clusters,
                                                   svd_method,
                                                   n_svd_vecs,
                                                   mini_batch,
                                                   init,
                                                   n_init,
                                                   n_jobs,
                                                   random_state)

    def _fit(self, X):
        normalized_data, row_diag, col_diag = _scale_normalize(X)
        n_sv = 1 + int(np.ceil(np.log2(self.n_clusters)))
        u, v = self._svd(normalized_data, n_sv, n_discard=1)
        z = np.vstack((row_diag[:, np.newaxis] * u,
                       col_diag[:, np.newaxis] * v))

        _, labels = self._k_means(z, self.n_clusters)

        n_rows = X.shape[0]
        self.row_labels_ = labels[:n_rows]
        self.column_labels_ = labels[n_rows:]

        self.rows_ = np.vstack(self.row_labels_ == c
                               for c in range(self.n_clusters))
        self.columns_ = np.vstack(self.column_labels_ == c
                                  for c in range(self.n_clusters))


class SpectralBiclustering(BaseSpectral):
    """Spectral biclustering (Kluger, 2003).

    Partitions rows and columns under the assumption that the data has
    an underlying checkerboard structure. For instance, if there are
    two row partitions and three column partitions, each row will
    belong to three biclusters, and each column will belong to two
    biclusters. The outer product of the corresponding row and column
    label vectors gives this checkerboard structure.

    Parameters
    ----------
    n_clusters : integer or tuple (n_row_clusters, n_column_clusters)
        The number of row and column clusters in the checkerboard
        structure.

    method : string
        Method of normalizing and converting singular vectors into
        biclusters. May be one of 'scale', 'bistochastic', or 'log'.
        CAUTION: if `method='log'`, the data must not be sparse.

    n_components : integer
        Number of singular vectors to check.

    n_best : integer
        Number of best singular vectors to which to project the data
        for clustering.

    svd_method : string, optional, default: 'randomized'
        Selects the algorithm for finding singular vectors. May be
        'randomized' or 'arpack'. If 'randomized', uses
        `sklearn.utils.extmath.randomized_svd`, which may be faster
        for large matrices. If 'arpack', uses
        `sklearn.utils.arpack.svds`, which is more accurate, but
        possibly slower in some cases.

    n_svd_vecs : int, optional, default: None
        Number of vectors to use in calculating the SVD. Corresponds
        to `ncv` when `svd_method=arpack` and `n_oversamples` when
        `svd_method` is 'randomized`.

    mini_batch : bool, optional, default: False
        Whether to use mini-batch k-means, which is faster but may get
        different results.

    random_state : int seed, RandomState instance, or None (default)
        A pseudo random number generator used by the K-Means
        initialization.

    Attributes
    ----------
    `rows_` : array-like, shape (n_row_clusters, n_rows)
        Results of the clustering. `rows[i, r]` is True if cluster `i`
        contains row `r`. Available only after calling ``fit``.

    `columns_` : array-like, shape (n_column_clusters, n_columns)
        Results of the clustering, like `rows`.

    `row_labels_` : array-like, shape (n_rows,)
        Row partition labels.

    `column_labels_` : array-like, shape (n_cols,)
        Column partition labels.


    References
    ----------

    * Kluger, Yuval, et. al., 2003. `Spectral biclustering of microarray
      data: coclustering genes and conditions
      <http://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.135.1608>`__.

    """
    def __init__(self, n_clusters=3, method='bistochastic',
                 n_components=6, n_best=3, svd_method='randomized',
                 n_svd_vecs=None, mini_batch=False, init='k-means++',
                 n_init=10, n_jobs=1, random_state=None):
        super(SpectralBiclustering, self).__init__(n_clusters,
                                                   svd_method,
                                                   n_svd_vecs,
                                                   mini_batch,
                                                   init,
                                                   n_init,
                                                   n_jobs,
                                                   random_state)
        self.method = method
        self.n_components = n_components
        self.n_best = n_best

    def _check_parameters(self):
        super(SpectralBiclustering, self)._check_parameters()
        legal_methods = ('bistochastic', 'scale', 'log')
        if self.method not in legal_methods:
            raise ValueError("Unknown method: '{}'. `method` must be"
                             " one of {}.".format(self.method, legal_methods))
        try:
            int(self.n_clusters)
        except TypeError:
            try:
                r, c = self.n_clusters
                int(r)
                int(c)
            except (ValueError, TypeError):
                raise ValueError("Incorrect parameter `n_clusters` has value:"
                                 " {}. It should either be a single integer"
                                 " or an iterable with two integers:"
                                 " `(n_row_clusters, n_column_clusters)`")
        if self.n_best > self.n_components:
            raise ValueError("`n_best` cannot be larger than"
                             " `n_components`, but {} >  {}"
                             "".format(self.n_best, self.n_components))

    def _fit(self, X):
        n_sv = self.n_components
        if self.method == 'bistochastic':
            normalized_data = _bistochastic_normalize(X)
            n_sv += 1
        elif self.method == 'scale':
            normalized_data, _, _ = _scale_normalize(X)
            n_sv += 1
        elif self.method == 'log':
            normalized_data = _log_normalize(X)
        n_discard = 0 if self.method == 'log' else 1
        u, v = self._svd(normalized_data, n_sv, n_discard)
        ut = u.T
        vt = v.T

        try:
            n_row_clusters, n_col_clusters = self.n_clusters
        except TypeError:
            n_row_clusters = n_col_clusters = self.n_clusters

        best_ut = self._fit_best_piecewise(ut, self.n_best,
                                           n_row_clusters)

        best_vt = self._fit_best_piecewise(vt, self.n_best,
                                           n_col_clusters)

        self.row_labels_ = self._project_and_cluster(X, best_vt.T,
                                                     n_row_clusters)

        self.column_labels_ = self._project_and_cluster(X.T, best_ut.T,
                                                        n_col_clusters)

        self.rows_ = np.vstack(self.row_labels_ == label
                               for label in range(n_row_clusters)
                               for _ in range(n_col_clusters))
        self.columns_ = np.vstack(self.column_labels_ == label
                                  for _ in range(n_row_clusters)
                                  for label in range(n_col_clusters))

    def _fit_best_piecewise(self, vectors, n_best, n_clusters):
        """Find the `n_best` vectors that are best approximated by piecewise
        constant vectors.

        The piecewise vectors are found by k-means; the best is chosen
        according to Euclidean distance.

        """
        def make_piecewise(v):
            centroid, labels = self._k_means(v.reshape(-1, 1), n_clusters)
            return centroid[labels].ravel()
        piecewise_vectors = np.apply_along_axis(make_piecewise,
                                                axis=1, arr=vectors)
        dists = np.apply_along_axis(norm, 1,
                                    vectors - piecewise_vectors)
        result = vectors[np.argsort(dists)[:n_best]]
        return result

    def _project_and_cluster(self, data, vectors, n_clusters):
        """Project `data` to `vectors` and cluster the result."""
        projected = safe_sparse_dot(data, vectors)
        _, labels = self._k_means(projected, n_clusters)
        return labels