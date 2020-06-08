import warnings
from typing import List, Tuple, Union

import numpy as np
from scipy import sparse as sps

from . import MatrixBase
from .dense_glm_matrix import DenseGLMDataMatrix
from .mkl_sparse_matrix import MKLSparseMatrix


def split_sparse_and_dense_parts(
    arg1: sps.csc_matrix, threshold: float = 0.1
) -> Tuple[DenseGLMDataMatrix, MKLSparseMatrix, np.ndarray, np.ndarray]:
    if not isinstance(arg1, sps.csc_matrix):
        raise TypeError(
            f"X must be of type scipy.sparse.csc_matrix or matrix.MKLSparseMatrix, not {type(arg1)}"
        )
    if not 0 <= threshold <= 1:
        raise ValueError("Threshold must be between 0 and 1.")
    densities = np.diff(arg1.indptr) / arg1.shape[0]
    dense_indices = np.where(densities > threshold)[0]
    sparse_indices = np.setdiff1d(np.arange(densities.shape[0]), dense_indices)

    X_dense_F = DenseGLMDataMatrix(np.asfortranarray(arg1.toarray()[:, dense_indices]))
    X_sparse = MKLSparseMatrix(arg1[:, sparse_indices])
    return X_dense_F, X_sparse, dense_indices, sparse_indices


def csc_to_split(mat: sps.csc_matrix, threshold=0.1):
    dense, sparse, dense_idx, sparse_idx = split_sparse_and_dense_parts(mat, threshold)
    return SplitMatrix([dense, sparse], [dense_idx, sparse_idx])


def mat_sandwich(mat_i: MatrixBase, mat_j: MatrixBase, d: np.ndarray) -> np.ndarray:
    if type(mat_i) == type(mat_j):
        return mat_i.sandwich(d)
    elif isinstance(mat_i, MKLSparseMatrix) and isinstance(mat_j, DenseGLMDataMatrix):
        return mat_i.sandwich_dense(mat_j, d)
    elif isinstance(mat_i, DenseGLMDataMatrix) and isinstance(mat_j, MKLSparseMatrix):
        return mat_j.sandwich_dense(mat_i, d).T
    else:
        raise NotImplementedError


class SplitMatrix(MatrixBase):
    def __init__(self, matrices: List[MatrixBase], indices: List[np.ndarray]):
        assert isinstance(indices, list)
        n_row = matrices[0].shape[0]
        dtype = matrices[0].dtype

        for i, (mat, idx) in enumerate(zip(matrices, indices)):
            if not mat.shape[0] == n_row:
                raise ValueError(
                    f"""
                    All matrices should have the same first dimension,
                    but the first matrix has first dimension {n_row} and matrix {i} has
                    first dimension {mat.shape[0]}."""
                )
            if not isinstance(mat, MatrixBase):
                raise ValueError(
                    "Expected all elements of matrices to be subclasses of MatrixBase."
                )
            if isinstance(mat, SplitMatrix):
                raise ValueError("Elements of matrices cannot be SplitMatrix.")
            if not mat.shape[1] == len(idx):
                raise ValueError(
                    f"""Element {i} of indices should should have length {mat.shape[1]},
                but it has shape {idx.shape}"""
                )
            if mat.dtype != dtype:
                warnings.warn(
                    f"""Matrices do not all have the same dtype. Dtypes are
                {[elt.dtype for elt in matrices]}."""
                )

        self.matrices = matrices
        self.indices = indices
        self.dtype = dtype
        self.shape = (n_row, sum([len(elt) for elt in indices]))
        assert self.shape[1] > 0

    def astype(self, dtype, order="K", casting="unsafe", copy=True):
        if copy:
            new_matrices = [
                mat.astype(dtype=dtype, order=order, casting=casting, copy=True)
                for mat in self.matrices
            ]
            return SplitMatrix(new_matrices, self.indices)
        for i in len(self.matrices):
            self.matrices[i] = self.matrices[i].astype(
                dtype=dtype, order=order, casting=casting, copy=False
            )
        return SplitMatrix(self.matrices, self.indices)

    def toarray(self) -> np.ndarray:
        out = np.empty(self.shape)
        for mat, idx in zip(self.matrices, self.indices):
            out[:, idx] = mat.A
        return out

    def getcol(self, i: int) -> Union[np.ndarray, sps.csr_matrix]:
        # wrap-around indexing
        i %= self.shape[1]
        for mat, idx in zip(self.matrices, self.indices):
            if i in idx:
                loc = np.where(idx == i)[0][0]
                return mat.getcol(loc)
        raise RuntimeError(f"Column {i} was not found.")

    def sandwich(self, d: np.ndarray) -> np.ndarray:
        out = np.empty((self.shape[1], self.shape[1]))
        for i in range(len(self.indices)):
            for j in range(i, len(self.indices)):
                idx_i = self.indices[i]
                mat_i = self.matrices[i]
                idx_j = self.indices[j]
                mat_j = self.matrices[j]
                res = mat_sandwich(mat_i, mat_j, d)
                out[np.ix_(idx_i, idx_j)] = res
                out[np.ix_(idx_j, idx_i)] = res.T
        return out

    def get_col_means(self, weights: np.ndarray) -> np.ndarray:
        col_means = np.empty(self.shape[1], dtype=self.dtype)
        for idx, mat in zip(self.indices, self.matrices):
            col_means[idx] = mat.get_col_means(weights)
        return col_means

    def get_col_stds(self, weights: np.ndarray, col_means: np.ndarray) -> np.ndarray:
        col_stds = np.empty(self.shape[1], dtype=self.dtype)
        for idx, mat in zip(self.indices, self.matrices):
            col_stds[idx] = mat.get_col_stds(weights, col_means[idx])

        return col_stds

    def dot(self, v: np.ndarray) -> np.ndarray:
        assert not isinstance(v, sps.spmatrix)
        v = np.asarray(v)
        if v.shape[0] != self.shape[1]:
            raise ValueError(f"shapes {self.shape} and {v.shape} not aligned")
        out_shape = (self.shape[0],) if v.ndim == 1 else (self.shape[0], v.shape[1])
        out = np.zeros(out_shape, np.result_type(self.dtype, v.dtype))
        for idx, mat in zip(self.indices, self.matrices):
            out += mat.dot(v[idx, ...])
        return out

    def transpose_dot(self, vec: Union[np.ndarray, List]) -> np.ndarray:
        """
        self.T.dot(vec)[i] = sum_k self[k, i] vec[k]
        = sum_{k in self.dense_indices} self[k, i] vec[k] +
          sum_{k in self.sparse_indices} self[k, i] vec[k]
        = self.X_dense.T.dot(vec) + self.X_sparse.T.dot(vec)
        """
        vec = np.asarray(vec)
        if vec.ndim == 1:
            out_shape = (self.shape[1],)
        elif vec.ndim == 2:
            out_shape = (self.shape[1], vec.shape[1])  # type: ignore
        else:
            raise NotImplementedError

        out = np.empty(out_shape, dtype=vec.dtype)

        for idx, mat in zip(self.indices, self.matrices):
            out[idx, ...] = mat.transpose_dot(vec)
        return out

    def scale_cols_inplace(self, col_scaling: np.ndarray):
        for idx, mat in zip(self.indices, self.matrices):
            mat.scale_cols_inplace(col_scaling[idx])

    def __getitem__(self, key):
        if isinstance(key, tuple):
            row, col = key
        else:
            row = key
            col = slice(None, None, None)  # all columns

        if col == slice(None, None, None):
            if isinstance(row, int):
                row = [row]

            return SplitMatrix([mat[row, :] for mat in self.matrices], self.indices)
        else:
            raise NotImplementedError(
                f"Only row indexing is supported. Index passed was {key}."
            )

    __array_priority__ = 13
