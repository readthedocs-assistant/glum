import time
from typing import Any, Callable, Dict, Tuple

import numpy as np
import pandas as pd
from scipy import sparse as sps

from glm_benchmarks.problems import load_narrow_insurance_data
from glm_benchmarks.sandwich.sandwich import (
    dense_sandwich,
    sparse_dense_sandwich,
    sparse_sandwich,
)


def load_data(n_rows: int) -> Tuple[Any, np.ndarray]:
    x = sps.csc_matrix(load_narrow_insurance_data(n_rows)["X"])
    np.random.seed(0)
    d = np.random.uniform(0, 1, n_rows)
    return x, d


def naive_sandwich(x, d):
    return (x.T @ (x.multiply(d[:, None]))).toarray()


def _fast_sandwich(X, d):
    return sparse_sandwich(X, X.XT, d)


def split(X):
    # TODO: this splitting function is super inefficient. easy to optimize though...
    densities = (X.indptr[1:] - X.indptr[:-1]) / X.shape[0]
    sorted_indices = np.argsort(densities)[::-1]
    sorted_densities = densities[sorted_indices]

    threshold_density = 0.1
    dense_indices = sorted_indices[sorted_densities > threshold_density]
    sparse_indices = np.setdiff1d(sorted_indices, dense_indices)

    X_dense_C = X.toarray()[:, dense_indices].copy()
    X_dense = np.asfortranarray(X_dense_C)
    X_sparse = sps.csc_matrix(X.toarray()[:, sparse_indices])
    X_sparse_csr = X_sparse.tocsr()

    def f(d):
        SS = sparse_sandwich(X_sparse, X_sparse_csr, d)
        DD = dense_sandwich(X_dense, d)
        # DS2 = dot_product_mkl(X_sparse.T, d[:,np.newaxis] * X_dense)
        DS = sparse_dense_sandwich(X_sparse, X_dense_C, d)
        # DS2 = sparse_dense_sandwich(X_sparse_csr, X_dense, d)
        # X_sparse_csr.data *= d[X_sparse_csr.indices]
        # DS3 = dot_product_mkl(X_sparse_csr.T, X_dense)
        # X_sparse_csr.data /= d[X_sparse_csr.indices]
        # np.testing.assert_almost_equal(DS2, DS)

        out = np.empty((X.shape[1], X.shape[1]))
        out[np.ix_(dense_indices, dense_indices)] = DD
        out[np.ix_(sparse_indices, sparse_indices)] = SS
        out[np.ix_(sparse_indices, dense_indices)] = DS
        out[np.ix_(dense_indices, sparse_indices)] = DS.T

        return out

    X.split_sandwich = f


def split_sandwich(X, d):
    return X.split_sandwich(d)


def _dense_sandwich(X, d):
    return dense_sandwich(X.X_dense, d)


def run_one_problem_all_methods(x, d, include_naive, dtype) -> pd.DataFrame:
    x = x.astype(dtype)
    d = d.astype(dtype)
    x.XT = x.T.tocsc()
    split(x)
    x.X_dense = np.asfortranarray(x.toarray())
    funcs: Dict[str, Callable[[Any, np.ndarray], Any]] = {
        "fast_sandwich": _fast_sandwich,
        "dense_sandwich": _dense_sandwich,
        "split_sandwich": split_sandwich,
    }
    if include_naive:
        funcs["naive"] = naive_sandwich

    info: Dict[str, Any] = {}
    for name, func in funcs.items():
        ts = []
        for i in range(3):
            start = time.perf_counter()
            res = func(x, d)
            ts.append(time.perf_counter() - start)
        elapsed = np.min(ts)

        info[name] = {}
        info[name]["res"] = res
        info[name]["time"] = elapsed

    if include_naive:
        naive_result = info["naive"]["res"]
        for k in info:
            np.testing.assert_allclose(naive_result, info[k]["res"], 4)

    return pd.DataFrame(
        {"method": list(info.keys()), "time": [elt["time"] for elt in info.values()]}
    )


def main() -> None:
    # "killed" with 1e7 and 4 * 1e6
    row_counts = [
        int(1e4),
        # int(1e5),
        int(3e5),
        int(1e6),
        int(2e6),
    ]  # , int(2e6), int(4e6), int(10e6)]
    benchmarks = []

    x, d = load_data(row_counts[-1])

    for i, n_rows in enumerate(row_counts):
        for dtype in [np.float64]:
            benchmarks.append(
                run_one_problem_all_methods(
                    x[:n_rows, :].copy(), d[:n_rows].copy(), i == 0, dtype
                )
            )
            benchmarks[-1]["dtype"] = str(dtype)
            benchmarks[-1]["n_rows"] = n_rows

    result_df = pd.concat(benchmarks).sort_values(["n_rows", "method"])
    print(result_df.set_index(["n_rows", "method"]))


if __name__ == "__main__":
    main()
