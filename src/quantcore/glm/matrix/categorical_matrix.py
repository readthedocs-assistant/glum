from typing import List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy import sparse as sps

from quantcore.glm.matrix.matrix_base import MatrixBase
from quantcore.glm.matrix.sandwich.categorical_sandwich import sandwich_categorical


def sandwich_python(
    indices: np.ndarray, indptr: np.ndarray, d: np.ndarray
) -> np.ndarray:
    """
    Returns a 1d array. The sandwich output is a diagonal matrix with this array on
    the diagonal.
    """
    tmp = d[indices]
    res = np.zeros(len(indptr) - 1, dtype=d.dtype)
    for i in range(len(res)):
        res[i] = tmp[indptr[i] : indptr[i + 1]].sum()
    return res


class CategoricalCSRMatrix(MatrixBase):
    def __init__(
        self,
        cat_vec: Union[List, np.ndarray, pd.Categorical],
        col_mult: Optional[Union[List, np.ndarray]] = None,
    ):
        if isinstance(cat_vec, pd.Categorical):
            self.cat = cat_vec
        else:
            self.cat = pd.Categorical(cat_vec)

        self.shape = (len(self.cat), len(self.cat.categories))
        self.indices = self.cat.codes
        self.x_csc: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self.col_mult = None if col_mult is None else np.squeeze(col_mult)
        if self.col_mult is None:
            # Indices may actually be stored as int8, but that is not Numexpr compatible
            self.dtype = np.dtype("float64")
        else:
            self.dtype = self.col_mult.dtype

    def recover_orig(self) -> np.ndarray:
        return self.cat.categories[self.cat.codes]

    def dot(self, other: Union[List, np.ndarray]) -> np.ndarray:
        """
        When other is 1d:
        mat.dot(other)[i] = sum_j mat[i, j] other[j] * col_mult[j]
                          = (other * col_mult)[mat.indices[i]]

        When other is 2d:
        mat.dot(other)[i, k] = sum_j mat[i, j] other[j, k] * col_mult
                            = (other * col_mult[None, :])[mat.indices[i], k]
        """
        other = np.asarray(other)
        if other.shape[0] != self.shape[1]:
            raise ValueError(
                f"""Needed other to have first dimension {self.shape[1]},
                but it has shape {other.shape}"""
            )
        if self.col_mult is None:
            other_m = other
        elif other.ndim == 1:
            other_m = other * self.col_mult
        else:
            other_m = other * self.col_mult[:, None]
        return other_m[self.indices, ...]

    def _check_csc(self) -> Tuple[np.ndarray, np.ndarray]:
        if self.x_csc is None:
            csc = self.tocsr().tocsc()
            self.x_csc = (csc.indices, csc.indptr)
        return self.x_csc

    # TODO: best way to return this depends on the use case. See what that is
    # See how csr getcol works
    def getcol(self, i: int) -> np.ndarray:
        i %= self.shape[1]  # wrap-around indexing
        col_i = (self.indices == i).astype(int)[:, None]
        if self.col_mult is None:
            return col_i
        return col_i * self.col_mult[i]

    def sandwich(self, d: Union[np.ndarray, List]) -> np.ndarray:
        d = np.asarray(d)
        indices, indptr = self._check_csc()
        res_diag = sandwich_categorical(indices, indptr, d)
        if self.col_mult is not None:
            res_diag *= self.col_mult ** 2
        # TODO: this is very inefficient downstream
        return np.diag(res_diag)

    def sandwich_python(self, d: Union[np.ndarray, List]) -> np.ndarray:
        """
        sandwich(self, d)[i, j] = (self.T @ diag(d) @ self)[i, j]
            = sum_k (self[k, i] (diag(d) @ self)[k, j])
            = sum_k self[k, i] sum_m diag(d)[k, m] self[m, j]
            = sum_k self[k, i] d[k] self[k, j]
            = 0 if i != j
        sandwich(self, d)[i, i] = sum_k self[k, i] ** 2 * d(k)
               = col_mult[i] ** 2 *  sum_k self.mat[k, i]** 2
        """
        d = np.asarray(d)
        indices, indptr = self._check_csc()
        res = sandwich_python(indices, indptr, d)
        if self.col_mult is not None:
            res *= self.col_mult ** 2
        return np.diag(res)

    def tocsr(self) -> sps.csr_matrix:
        # TODO: write a test for this
        # TODO: data should be uint8
        if self.col_mult is None:
            data = np.ones(self.shape[0], dtype=int)
        else:
            data = self.col_mult[self.indices]

        return sps.csr_matrix(
            (data, self.indices, np.arange(self.shape[0] + 1, dtype=int),)
        )

    def toarray(self) -> np.ndarray:
        return self.tocsr().A

    def transpose_dot(self, vec: Union[np.ndarray, List]) -> np.ndarray:
        # TODO: there is probably a more efficient method for this
        return self.tocsr().T.dot(vec)

    def astype(self, dtype, order="K", casting="unsafe", copy=True):
        """
        This method doesn't make a lot of sense since indices needs to be of int dtype,
        but it needs to be implemented.
        """
        if self.col_mult is not None:
            self.col_mult = self.col_mult.astype(dtype, order, casting, copy)
        self.dtype = dtype
        return self

    def get_col_stds(self, weights: np.ndarray, col_means: np.ndarray) -> np.ndarray:
        one = self.transpose_dot(weights)
        if self.col_mult is not None:
            one *= self.col_mult

        return np.sqrt(one - col_means ** 2)

    def scale_cols_inplace(self, col_scaling: np.ndarray) -> None:
        if self.col_mult is None:
            self.col_mult = col_scaling
        else:
            self.col_mult *= col_scaling
            # If we have standardized then undstandardized, col_mult
            # should become 1, which is the same as not having a col_mult.
            if np.all(np.abs(self.col_mult - 1) < 1e-12):
                self.col_mult = None

    def __getitem__(self, item):
        if isinstance(item, tuple):
            row, col = item
            if not col == slice(None, None, None):
                raise IndexError("Only column indexing is supported.")
        else:
            row = item
        if isinstance(row, int):
            row = [row]
        return CategoricalCSRMatrix(self.cat[row], self.col_mult)
