from typing import Hashable, Optional

import dask.array as da
import numpy as np
from numba import guvectorize
from xarray import Dataset

from sgkit.stats.utils import assert_array_shape
from sgkit.typing import ArrayLike
from sgkit.utils import conditional_merge_datasets, define_variable_if_absent
from sgkit.window import has_windows, window_statistic

from .. import variables
from .aggregation import count_cohort_alleles, count_variant_alleles


def diversity(
    ds: Dataset,
    *,
    cohort_allele_count: Hashable = variables.cohort_allele_count,
    merge: bool = True,
) -> Dataset:
    """Compute diversity from cohort allele counts.

    By default, values of this statistic are calculated per variant.
    To compute values in windows, call :func:`window` before calling
    this function.

    Parameters
    ----------
    ds
        Genotype call dataset.
    cohort_allele_count
        Cohort allele count variable to use or calculate. Defined by
        :data:`sgkit.variables.cohort_allele_count_spec`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`count_cohort_alleles`.
    merge
        If True (the default), merge the input dataset and the computed
        output variables into a single dataset, otherwise return only
        the computed output variables.
        See :ref:`dataset_merge` for more details.

    Returns
    -------
    A dataset containing the diversity values, as defined by :data:`sgkit.variables.stat_diversity_spec`.
    Shape (variants, cohorts), or (windows, cohorts) if windowing information is available.

    Warnings
    --------
    This method does not currently support datasets that are chunked along the
    samples dimension.

    Examples
    --------

    >>> import numpy as np
    >>> import sgkit as sg
    >>> import xarray as xr
    >>> ds = sg.simulate_genotype_call_dataset(n_variant=5, n_sample=4)

    >>> # Divide samples into two cohorts
    >>> sample_cohort = np.repeat([0, 1], ds.dims["samples"] // 2)
    >>> ds["sample_cohort"] = xr.DataArray(sample_cohort, dims="samples")

    >>> sg.diversity(ds)["stat_diversity"].values # doctest: +NORMALIZE_WHITESPACE
    array([[0.5       , 0.66666667],
        [0.66666667, 0.5       ],
        [0.66666667, 0.66666667],
        [0.5       , 0.5       ],
        [0.5       , 0.5       ]])

    >>> # Divide into windows of size three (variants)
    >>> ds = sg.window(ds, size=3, step=3)
    >>> sg.diversity(ds)["stat_diversity"].values # doctest: +NORMALIZE_WHITESPACE
    array([[1.83333333, 1.83333333],
        [1.        , 1.        ]])
    """
    ds = define_variable_if_absent(
        ds, variables.cohort_allele_count, cohort_allele_count, count_cohort_alleles
    )
    variables.validate(ds, {cohort_allele_count: variables.cohort_allele_count_spec})

    ac = ds[cohort_allele_count]
    an = ac.sum(axis=2)
    n_pairs = an * (an - 1) / 2
    n_same = (ac * (ac - 1) / 2).sum(axis=2)
    n_diff = n_pairs - n_same
    # replace zeros to avoid divide by zero error
    n_pairs_na = n_pairs.where(n_pairs != 0)
    pi = n_diff / n_pairs_na

    if has_windows(ds):
        div = window_statistic(
            pi,
            np.sum,
            ds.window_start.values,
            ds.window_stop.values,
            dtype=pi.dtype,
            axis=0,
        )
        new_ds = Dataset(
            {
                variables.stat_diversity: (
                    ("windows", "cohorts"),
                    div,
                )
            }
        )
    else:
        new_ds = Dataset(
            {
                variables.stat_diversity: (
                    ("variants", "cohorts"),
                    pi,
                )
            }
        )
    return conditional_merge_datasets(ds, variables.validate(new_ds), merge)


# c = cohorts, k = alleles
@guvectorize(  # type: ignore
    ["void(int64[:, :], float64[:,:])"], "(c, k)->(c,c)", nopython=True, cache=True
)
def _divergence(ac: ArrayLike, out: ArrayLike) -> None:
    """Generalized U-function for computing divergence.

    Parameters
    ----------
    ac
        Allele counts of shape (cohorts, alleles) containing per-cohort allele counts.
    out
        Pairwise divergence stats with shape (cohorts, cohorts), where the entry at
        (i, j) is the divergence between cohort i and cohort j.
    """
    an = ac.sum(axis=-1)
    out[:, :] = np.nan  # (cohorts, cohorts)
    n_cohorts = ac.shape[0]
    n_alleles = ac.shape[1]
    # calculate the divergence for each cohort pair
    for i in range(n_cohorts):
        for j in range(i + 1, n_cohorts):
            n_pairs = an[i] * an[j]
            n_same = 0
            for k in range(n_alleles):
                n_same += ac[i, k] * ac[j, k]
            n_diff = n_pairs - n_same
            div = n_diff / n_pairs
            out[i, j] = div
            out[j, i] = div

    # calculate the diversity for each cohort
    for i in range(n_cohorts):
        n_pairs = an[i] * (an[i] - 1)
        n_same = 0
        for k in range(n_alleles):
            n_same += ac[i, k] * (ac[i, k] - 1)
        n_diff = n_pairs - n_same
        if n_pairs != 0.0:
            div = n_diff / n_pairs
            out[i, i] = div


def divergence(
    ds: Dataset,
    *,
    cohort_allele_count: Hashable = variables.cohort_allele_count,
    merge: bool = True,
) -> Dataset:
    """Compute divergence between pairs of cohorts.

    The entry at (i, j) is the divergence between for cohort i and cohort j,
    except for the case where i and j are the same, in which case the entry
    is the diversity for cohort i.

    By default, values of this statistic are calculated per variant.
    To compute values in windows, call :func:`window` before calling
    this function.

    Parameters
    ----------
    ds
        Genotype call dataset.
    cohort_allele_count
        Cohort allele count variable to use or calculate. Defined by
        :data:`sgkit.variables.cohort_allele_count_spec`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`count_cohort_alleles`.
    merge
        If True (the default), merge the input dataset and the computed
        output variables into a single dataset, otherwise return only
        the computed output variables.
        See :ref:`dataset_merge` for more details.

    Returns
    -------
    A dataset containing the divergence value between pairs of cohorts, as defined by
    :data:`sgkit.variables.stat_divergence_spec`.
    Shape (variants, cohorts, cohorts), or (windows, cohorts, cohorts) if windowing
    information is available.

    Warnings
    --------
    This method does not currently support datasets that are chunked along the
    samples dimension.

    Examples
    --------

    >>> import numpy as np
    >>> import sgkit as sg
    >>> import xarray as xr
    >>> ds = sg.simulate_genotype_call_dataset(n_variant=5, n_sample=4)

    >>> # Divide samples into two cohorts
    >>> sample_cohort = np.repeat([0, 1], ds.dims["samples"] // 2)
    >>> ds["sample_cohort"] = xr.DataArray(sample_cohort, dims="samples")

    >>> sg.divergence(ds)["stat_divergence"].values # doctest: +NORMALIZE_WHITESPACE
    array([[[0.5       , 0.5       ],
            [0.5       , 0.66666667]],
    <BLANKLINE>
        [[0.66666667, 0.5       ],
            [0.5       , 0.5       ]],
    <BLANKLINE>
        [[0.66666667, 0.5       ],
            [0.5       , 0.66666667]],
    <BLANKLINE>
        [[0.5       , 0.375     ],
            [0.375     , 0.5       ]],
    <BLANKLINE>
        [[0.5       , 0.625     ],
            [0.625     , 0.5       ]]])

    >>> # Divide into windows of size three (variants)
    >>> ds = sg.window(ds, size=3, step=3)
    >>> sg.divergence(ds)["stat_divergence"].values # doctest: +NORMALIZE_WHITESPACE
    array([[[1.83333333, 1.5       ],
            [1.5       , 1.83333333]],
    <BLANKLINE>
        [[1.        , 1.        ],
            [1.        , 1.        ]]])
    """

    ds = define_variable_if_absent(
        ds, variables.cohort_allele_count, cohort_allele_count, count_cohort_alleles
    )
    variables.validate(ds, {cohort_allele_count: variables.cohort_allele_count_spec})
    ac = ds[cohort_allele_count]

    n_variants = ds.dims["variants"]
    n_cohorts = ds.dims["cohorts"]
    ac = da.asarray(ac)
    shape = (ac.chunks[0], n_cohorts, n_cohorts)
    d = da.map_blocks(_divergence, ac, chunks=shape, dtype=np.float64)
    assert_array_shape(d, n_variants, n_cohorts, n_cohorts)

    if has_windows(ds):
        div = window_statistic(
            d,
            np.sum,
            ds.window_start.values,
            ds.window_stop.values,
            dtype=d.dtype,
            axis=0,
        )
        new_ds = Dataset(
            {
                variables.stat_divergence: (
                    ("windows", "cohorts_0", "cohorts_1"),
                    div,
                )
            }
        )
    else:
        new_ds = Dataset(
            {
                variables.stat_divergence: (
                    ("variants", "cohorts_0", "cohorts_1"),
                    d,
                )
            }
        )
    return conditional_merge_datasets(ds, variables.validate(new_ds), merge)


# c = cohorts
@guvectorize(  # type: ignore
    [
        "void(float32[:,:], float32[:,:])",
        "void(float64[:,:], float64[:,:])",
    ],
    "(c,c)->(c,c)",
    nopython=True,
    cache=True,
)
def _Fst_Hudson(d: ArrayLike, out: ArrayLike) -> None:
    """Generalized U-function for computing Fst using Hudson's estimator.

    Parameters
    ----------
    d
        Pairwise divergence values of shape (cohorts, cohorts),
        with diversity values on the diagonal.
    out
        Pairwise Fst with shape (cohorts, cohorts), where the entry at
        (i, j) is the Fst for cohort i and cohort j.
    """
    out[:, :] = np.nan  # (cohorts, cohorts)
    n_cohorts = d.shape[0]
    for i in range(n_cohorts):
        for j in range(i + 1, n_cohorts):
            if d[i, j] != 0.0:
                fst = 1 - ((d[i, i] + d[j, j]) / 2) / d[i, j]
                out[i, j] = fst
                out[j, i] = fst


# c = cohorts
@guvectorize(  # type: ignore
    [
        "void(float32[:,:], float32[:,:])",
        "void(float64[:,:], float64[:,:])",
    ],
    "(c,c)->(c,c)",
    nopython=True,
    cache=True,
)
def _Fst_Nei(d: ArrayLike, out: ArrayLike) -> None:
    """Generalized U-function for computing Fst using Nei's estimator.

    Parameters
    ----------
    d
        Pairwise divergence values of shape (cohorts, cohorts),
        with diversity values on the diagonal.
    out
        Pairwise Fst with shape (cohorts, cohorts), where the entry at
        (i, j) is the Fst for cohort i and cohort j.
    """
    out[:, :] = np.nan  # (cohorts, cohorts)
    n_cohorts = d.shape[0]
    for i in range(n_cohorts):
        for j in range(i + 1, n_cohorts):
            den = d[i, i] + 2 * d[i, j] + d[j, j]
            if den != 0.0:
                fst = 1 - (2 * (d[i, i] + d[j, j]) / den)
                out[i, j] = fst
                out[j, i] = fst


def Fst(
    ds: Dataset,
    *,
    estimator: Optional[str] = None,
    stat_divergence: Hashable = variables.stat_divergence,
    merge: bool = True,
) -> Dataset:
    """Compute Fst between pairs of cohorts.

    By default, values of this statistic are calculated per variant.
    To compute values in windows, call :func:`window` before calling
    this function.

    Parameters
    ----------
    ds
        Genotype call dataset.
    estimator
        Determines the formula to use for computing Fst.
        If None (the default), or ``Hudson``, Fst is calculated
        using the method of Hudson (1992) elaborated by Bhatia et al. (2013),
        (the same estimator as scikit-allel).
        Other supported estimators include ``Nei`` (1986), (the same estimator
        as tskit).
    stat_divergence
        Divergence variable to use or calculate. Defined by
        :data:`sgkit.variables.stat_divergence_spec`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`divergence`.
    merge
        If True (the default), merge the input dataset and the computed
        output variables into a single dataset, otherwise return only
        the computed output variables.
        See :ref:`dataset_merge` for more details.

    Returns
    -------
    A dataset containing the Fst value between pairs of cohorts, as defined by
    :data:`sgkit.variables.stat_Fst_spec`.
    Shape (variants, cohorts, cohorts), or (windows, cohorts, cohorts) if windowing
    information is available.

    Warnings
    --------
    This method does not currently support datasets that are chunked along the
    samples dimension.

    Examples
    --------

    >>> import numpy as np
    >>> import sgkit as sg
    >>> import xarray as xr
    >>> ds = sg.simulate_genotype_call_dataset(n_variant=5, n_sample=4)

    >>> # Divide samples into two cohorts
    >>> sample_cohort = np.repeat([0, 1], ds.dims["samples"] // 2)
    >>> ds["sample_cohort"] = xr.DataArray(sample_cohort, dims="samples")

    >>> sg.Fst(ds)["stat_Fst"].values # doctest: +NORMALIZE_WHITESPACE
    array([[[        nan, -0.16666667],
            [-0.16666667,         nan]],
    <BLANKLINE>
        [[        nan, -0.16666667],
            [-0.16666667,         nan]],
    <BLANKLINE>
        [[        nan, -0.33333333],
            [-0.33333333,         nan]],
    <BLANKLINE>
        [[        nan, -0.33333333],
            [-0.33333333,         nan]],
    <BLANKLINE>
        [[        nan,  0.2       ],
            [ 0.2       ,         nan]]])

    >>> # Divide into windows of size three (variants)
    >>> ds = sg.window(ds, size=3, step=3)
    >>> sg.Fst(ds)["stat_Fst"].values # doctest: +NORMALIZE_WHITESPACE
    array([[[        nan, -0.22222222],
            [-0.22222222,         nan]],
    <BLANKLINE>
        [[        nan,  0.        ],
            [ 0.        ,         nan]]])
    """
    known_estimators = {"Hudson": _Fst_Hudson, "Nei": _Fst_Nei}
    if estimator is not None and estimator not in known_estimators:
        raise ValueError(
            f"Estimator '{estimator}' is not a known estimator: {known_estimators.keys()}"
        )
    estimator = estimator or "Hudson"
    ds = define_variable_if_absent(
        ds, variables.stat_divergence, stat_divergence, divergence
    )
    variables.validate(ds, {stat_divergence: variables.stat_divergence_spec})

    n_cohorts = ds.dims["cohorts"]
    gs = da.asarray(ds.stat_divergence)
    shape = (gs.chunks[0], n_cohorts, n_cohorts)
    fst = da.map_blocks(known_estimators[estimator], gs, chunks=shape, dtype=np.float64)
    # TODO: reinstate assert (first dim could be either variants or windows)
    # assert_array_shape(fst, n_windows, n_cohorts, n_cohorts)
    new_ds = Dataset({variables.stat_Fst: (("windows", "cohorts_0", "cohorts_1"), fst)})
    return conditional_merge_datasets(ds, variables.validate(new_ds), merge)


def Tajimas_D(
    ds: Dataset,
    *,
    variant_allele_count: Hashable = variables.variant_allele_count,
    stat_diversity: Hashable = variables.stat_diversity,
    merge: bool = True,
) -> Dataset:
    """Compute Tajimas' D for a genotype call dataset.

    By default, values of this statistic are calculated per variant.
    To compute values in windows, call :func:`window` before calling
    this function.

    Parameters
    ----------
    ds
        Genotype call dataset.
    variant_allele_count
        Variant allele count variable to use or calculate. Defined by
        :data:`sgkit.variables.variant_allele_count`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`count_variant_alleles`.
    stat_diversity
        Diversity variable to use or calculate. Defined by
        :data:`sgkit.variables.stat_diversity_spec`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`diversity`.
    merge
        If True (the default), merge the input dataset and the computed
        output variables into a single dataset, otherwise return only
        the computed output variables.
        See :ref:`dataset_merge` for more details.

    Returns
    -------
    A dataset containing the Tajimas' D value, as defined by :data:`sgkit.variables.stat_Tajimas_D_spec`.
    Shape (variants, cohorts), or (windows, cohorts) if windowing information is available.

    Warnings
    --------
    This method does not currently support datasets that are chunked along the
    samples dimension.

    Examples
    --------

    >>> import numpy as np
    >>> import sgkit as sg
    >>> import xarray as xr
    >>> ds = sg.simulate_genotype_call_dataset(n_variant=5, n_sample=4)

    >>> # Divide samples into two cohorts
    >>> sample_cohort = np.repeat([0, 1], ds.dims["samples"] // 2)
    >>> ds["sample_cohort"] = xr.DataArray(sample_cohort, dims="samples")

    >>> sg.Tajimas_D(ds)["stat_Tajimas_D"].values # doctest: +NORMALIZE_WHITESPACE
    array([[-3.35891429, -2.96698697],
        [-2.96698697, -3.35891429],
        [-2.96698697, -2.96698697],
        [-3.35891429, -3.35891429],
        [-3.35891429, -3.35891429]])

    >>> # Divide into windows of size three (variants)
    >>> ds = sg.window(ds, size=3, step=3)
    >>> sg.Tajimas_D(ds)["stat_Tajimas_D"].values # doctest: +NORMALIZE_WHITESPACE
    array([[-0.22349574, -0.22349574],
        [-2.18313233, -2.18313233]])
    """
    ds = define_variable_if_absent(
        ds, variables.variant_allele_count, variant_allele_count, count_variant_alleles
    )
    ds = define_variable_if_absent(
        ds, variables.stat_diversity, stat_diversity, diversity
    )
    variables.validate(
        ds,
        {
            variant_allele_count: variables.variant_allele_count_spec,
            stat_diversity: variables.stat_diversity_spec,
        },
    )

    ac = ds[variant_allele_count]

    # count segregating
    S = ((ac > 0).sum(axis=1) > 1).sum()

    # assume number of chromosomes sampled is constant for all variants
    # NOTE: even tho ac has dtype uint, we promote the sum to float
    #       because the computation below requires floats
    n = ac.sum(axis=1, dtype="float").max()

    # (n-1)th harmonic number
    a1 = (1 / da.arange(1, n)).sum()

    # calculate Watterson's theta (absolute value)
    theta = S / a1

    # get diversity
    div = ds[stat_diversity]

    # N.B., both theta estimates are usually divided by the number of
    # (accessible) bases but here we want the absolute difference
    d = div - theta

    # calculate the denominator (standard deviation)
    a2 = (1 / (da.arange(1, n) ** 2)).sum()
    b1 = (n + 1) / (3 * (n - 1))
    b2 = 2 * (n ** 2 + n + 3) / (9 * n * (n - 1))
    c1 = b1 - (1 / a1)
    c2 = b2 - ((n + 2) / (a1 * n)) + (a2 / (a1 ** 2))
    e1 = c1 / a1
    e2 = c2 / (a1 ** 2 + a2)
    d_stdev = np.sqrt((e1 * S) + (e2 * S * (S - 1)))

    if d_stdev == 0:
        D = np.nan
    else:
        # finally calculate Tajima's D
        D = d / d_stdev

    new_ds = Dataset({variables.stat_Tajimas_D: D})
    return conditional_merge_datasets(ds, variables.validate(new_ds), merge)


# c = cohorts
@guvectorize(  # type: ignore
    ["void(float32[:, :], float32[:,:,:])", "void(float64[:, :], float64[:,:,:])"],
    "(c,c)->(c,c,c)",
    nopython=True,
    cache=True,
)
def _pbs(t: ArrayLike, out: ArrayLike) -> None:
    """Generalized U-function for computing PBS."""
    out[:, :, :] = np.nan  # (cohorts, cohorts, cohorts)
    n_cohorts = t.shape[0]
    # calculate PBS for each cohort triple
    for i in range(n_cohorts):
        for j in range(i + 1, n_cohorts):
            for k in range(j + 1, n_cohorts):
                ret = (t[i, j] + t[i, k] - t[j, k]) / 2
                norm = 1 + (t[i, j] + t[i, k] + t[j, k]) / 2
                ret = ret / norm
                out[i, j, k] = ret


def pbs(
    ds: Dataset,
    *,
    stat_Fst: Hashable = variables.stat_Fst,
    merge: bool = True,
) -> Dataset:
    """Compute the population branching statistic (PBS) between cohort triples.

    By default, values of this statistic are calculated per variant.
    To compute values in windows, call :func:`window` before calling
    this function.

    Parameters
    ----------
    ds
        Genotype call dataset.
    stat_Fst
        Fst variable to use or calculate. Defined by
        :data:`sgkit.variables.stat_Fst_spec`.
        If the variable is not present in ``ds``, it will be computed
        using :func:`Fst`.
    merge
        If True (the default), merge the input dataset and the computed
        output variables into a single dataset, otherwise return only
        the computed output variables.
        See :ref:`dataset_merge` for more details.

    Returns
    -------
    A dataset containing the PBS value between cohort triples, as defined by
    :data:`sgkit.variables.stat_pbs_spec`.
    Shape (variants, cohorts, cohorts, cohorts), or
    (windows, cohorts, cohorts, cohorts) if windowing information is available.

    Warnings
    --------
    This method does not currently support datasets that are chunked along the
    samples dimension.

    Examples
    --------

    >>> import numpy as np
    >>> import sgkit as sg
    >>> import xarray as xr
    >>> ds = sg.simulate_genotype_call_dataset(n_variant=5, n_sample=6)

    >>> # Divide samples into three named cohorts
    >>> n_cohorts = 3
    >>> sample_cohort = np.repeat(range(n_cohorts), ds.dims["samples"] // n_cohorts)
    >>> ds["sample_cohort"] = xr.DataArray(sample_cohort, dims="samples")
    >>> cohort_names = [f"co_{i}" for i in range(n_cohorts)]
    >>> ds = ds.assign_coords({"cohorts_0": cohort_names, "cohorts_1": cohort_names, "cohorts_2": cohort_names})

    >>> # Divide into two windows of size three (variants)
    >>> ds = sg.window(ds, size=3, step=3)
    >>> sg.pbs(ds)["stat_pbs"].sel(cohorts_0="co_0", cohorts_1="co_1", cohorts_2="co_2").values # doctest: +NORMALIZE_WHITESPACE
    array([ 0.      , -0.160898])
    """

    ds = define_variable_if_absent(ds, variables.stat_Fst, stat_Fst, Fst)
    variables.validate(ds, {stat_Fst: variables.stat_Fst_spec})

    fst = ds[variables.stat_Fst]
    fst = fst.clip(min=0, max=(1 - np.finfo(float).epsneg))

    t = -np.log(1 - fst)
    n_cohorts = ds.dims["cohorts"]
    n_windows = ds.dims["windows"]
    assert_array_shape(t, n_windows, n_cohorts, n_cohorts)

    # calculate PBS triples
    t = da.asarray(t)
    shape = (t.chunks[0], n_cohorts, n_cohorts, n_cohorts)
    p = da.map_blocks(_pbs, t, chunks=shape, new_axis=3, dtype=np.float64)
    assert_array_shape(p, n_windows, n_cohorts, n_cohorts, n_cohorts)

    new_ds = Dataset(
        {variables.stat_pbs: (["windows", "cohorts_0", "cohorts_1", "cohorts_2"], p)}
    )
    return conditional_merge_datasets(ds, variables.validate(new_ds), merge)