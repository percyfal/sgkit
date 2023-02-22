from typing import Any, Hashable, Mapping, Tuple

import numpy as np
import pandas as pd
import xarray as xr

from sgkit.accelerate import numba_guvectorize
from sgkit.typing import ArrayLike


class GenotypeDisplay:
    """
    A printable object to display genotype information.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        shape: Tuple[int, int],
        max_variants: int,
        max_samples: int,
    ):
        self.df = df
        self.shape = shape
        self.max_variants = max_variants
        self.max_samples = max_samples
        self.pd_options = [
            "display.max_rows",
            self.max_variants,
            "display.min_rows",
            self.max_variants,
            "display.max_columns",
            self.max_samples,
            "display.show_dimensions",
            False,
        ]

    def __repr__(self) -> Any:
        with pd.option_context(*self.pd_options):
            if (
                len(self.df) > self.max_variants
                or len(self.df.columns) > self.max_samples
            ):
                return (
                    self.df.__repr__()
                    + f"\n\n[{self.shape[0]} rows x {self.shape[1]} columns]"
                )
            return self.df.__repr__()

    def _repr_html_(self) -> Any:
        with pd.option_context(*self.pd_options):
            if (
                len(self.df) > self.max_variants
                or len(self.df.columns) > self.max_samples
            ):
                return self.df._repr_html_().replace(
                    "</div>",
                    f"<p>{self.shape[0]} rows x {self.shape[1]} columns</p></div>",
                )
            return self.df._repr_html_()


@numba_guvectorize(  # type: ignore
    [
        "void(uint8[:], uint8[:], boolean[:], uint8[:], uint8[:])",
    ],
    "(b),(),(),(c)->(c)",
)
def _format_genotype_bytes(
    chars: ArrayLike, ploidy: int, phased: bool, _: ArrayLike, out: ArrayLike
) -> None:  # pragma: no cover
    ploidy = ploidy[0]
    sep = 124 if phased[0] else 47  # "|" or "/"
    chars_per_allele = len(chars) // ploidy
    slot = 0
    for slot in range(ploidy):
        offset_inp = slot * chars_per_allele
        offset_out = slot * (chars_per_allele + 1)
        if slot > 0:
            out[offset_out - 1] = sep
        for char in range(chars_per_allele):
            i = offset_inp + char
            j = offset_out + char
            val = chars[i]
            if val == 45:  # "-"
                if chars[i + 1] == 49:  # "1"
                    # this is an unknown allele
                    out[j] = 46  # "."
                    out[j + 1 : j + chars_per_allele] = 0
                    break
                else:
                    # < -1 indicates a gap
                    out[j : j + chars_per_allele] = 0
                    if slot > 0:
                        # remove separator
                        out[offset_out - 1] = 0
                    break
            else:
                out[j] = val
    # shuffle zeros to end
    c = len(out)
    for i in range(c):
        if out[i] == 0:
            for j in range(i + 1, c):
                if out[j] != 0:
                    out[i] = out[j]
                    out[j] = 0
                    break


def genotype_as_bytes(
    genotype: ArrayLike,
    phased: ArrayLike,
    max_allele_chars: int = 2,
) -> ArrayLike:
    """Convert integer encoded genotype calls to (unphased)
    VCF style byte strings.

    Parameters
    ----------
    genotype
        Genotype call.
    phased
        Boolean indicating if genotype is phased.
    max_allele_chars
        Maximum number of chars required for any allele.
        This should include signed sentinel values.

    Returns
    -------
    genotype_string
        Genotype encoded as byte string.
    """
    ploidy = genotype.shape[-1]
    b = genotype.astype("|S{}".format(max_allele_chars))
    b.dtype = np.uint8
    n_num = b.shape[-1]
    n_char = n_num + ploidy - 1
    dummy = np.empty(n_char, np.uint8)
    c = _format_genotype_bytes(b, ploidy, phased, dummy)
    c.dtype = "|S{}".format(n_char)
    return np.squeeze(c)


def truncate(ds: xr.Dataset, max_sizes: Mapping[Hashable, int]) -> xr.Dataset:
    """Truncate a dataset along two dimensions into a form suitable for display.

    Truncation involves taking four rectangles from each corner of the dataset array
    (or arrays) and combining them into a smaller dataset array (or arrays).

    Parameters
    ----------
    ds
        The dataset to be truncated.
    max_sizes : Mapping[Hashable, int]
        A dict with keys matching dimensions and integer values indicating
        the maximum size of the dimension after truncation.

    Returns
    -------
    Dataset
        A truncated dataset.

    Warnings
    --------
    A maximum size of `n` may result in the array having size `n + 2` (and not `n`).
    The reason for this is so that pandas can be used to display the array as a table,
    and correctly truncate rows or columns (shown as ellipses ...).
    """
    sel = dict()
    for dim, size in max_sizes.items():
        if ds.dims[dim] <= size:
            # No truncation required
            pass
        else:
            n_head = size // 2 + size % 2 + 1  # + 1 for ellipses
            n_tail = -size // 2
            head = ds[dim][0:n_head]
            tail = ds[dim][n_tail:]
            sel[dim] = np.append(head, tail)
    return ds.sel(sel)


def set_index_if_unique(ds: xr.Dataset, dim: str, index: str) -> xr.Dataset:
    ds_with_index = ds.set_index({dim: index})
    idx = ds_with_index.get_index(dim)
    if len(idx) != len(idx.unique()):
        # index is not unique so don't use it
        return ds
    return ds_with_index


def display_genotypes(
    ds: xr.Dataset, max_variants: int = 60, max_samples: int = 10
) -> GenotypeDisplay:
    """Display genotype calls.

    Display genotype calls in a tabular format, with rows for variants,
    and columns for samples. Genotypes are displayed in the same manner
    as in VCF. For example, `1/0` is a diploid call of the first alternate
    allele and the reference allele (0). Phased calls are denoted by a `|`
    separator. Missing values are denoted by `.`.

    Parameters
    ----------
    ds
        The dataset containing genotype calls in the `call/genotype`
        variable, and (optionally) phasing information in the
        `call/genotype_phased` variable. If no phasing information is
        present genotypes are assumed to be unphased.
    max_variants
        The maximum number of variants (rows) to display. If there are
        more variants than this then the table is truncated.
    max_samples
        The maximum number of samples (columns) to display. If there are
        more samples than this then the table is truncated.

    Returns
    -------
    A printable object to display genotype information.
    """

    # Create a copy to avoid clobbering original indexes
    ds_calls = ds.copy()

    # Set indexes only if not already set (allows users to have different row/col labels)
    # and if setting them produces a unique index
    if isinstance(ds_calls.get_index("samples"), pd.RangeIndex):
        ds_calls = set_index_if_unique(ds_calls, "samples", "sample_id")
    if isinstance(ds_calls.get_index("variants"), pd.RangeIndex):
        variant_index = "variant_id" if "variant_id" in ds_calls else "variant_position"
        ds_calls = set_index_if_unique(ds_calls, "variants", variant_index)

    # Restrict to genotype call variables
    if "call_genotype_phased" in ds_calls:
        ds_calls = ds_calls[
            ["call_genotype", "call_genotype_mask", "call_genotype_phased"]
        ]
    else:
        ds_calls = ds_calls[["call_genotype", "call_genotype_mask"]]

    # Truncate the dataset then convert to a dataframe
    ds_abbr = truncate(
        ds_calls, max_sizes={"variants": max_variants, "samples": max_samples}
    )
    df = ds_abbr.to_dataframe().unstack(level="ploidy")

    # Convert each genotype to a string representation
    def calls_to_str(r: pd.DataFrame) -> str:
        gt = r["call_genotype"].astype(str)
        gt_mask = r["call_genotype_mask"].astype(bool)
        gt[gt_mask] = "."
        if "call_genotype_phased" in r and r["call_genotype_phased"][0]:
            return "|".join(gt)
        return "/".join(gt)

    df = df.apply(calls_to_str, axis=1).unstack("samples")

    return GenotypeDisplay(
        df,
        (ds.sizes["variants"], ds.sizes["samples"]),
        max_variants,
        max_samples,
    )
