from itertools import product
from typing import List, Mapping, Union

import numpy as np
import polars as pl
import polars.selectors as cs
from scipy.stats import boxcox_normmax
from typing_extensions import Literal

from functime.base import transformer
from functime.base.model import ModelState
from functime.offsets import _strip_freq_alias


def PL_NUMERIC_COLS(*exclude):
    return cs.numeric() - cs.by_name(exclude)


def reindex(X: pl.DataFrame) -> pl.DataFrame:
    entity_col, time_col = X.columns[:2]
    dtypes = X.dtypes[:2]
    entities = sorted(set(X.get_column(entity_col)))
    timestamps = sorted(set(X.get_column(time_col)))
    X = pl.DataFrame(
        product(entities, timestamps),
        schema={entity_col: dtypes[0], time_col: dtypes[1]},
    ).join(X, how="left", on=[entity_col, time_col])
    return X


@transformer
def coerce_dtypes(schema: Mapping[str, pl.DataType]):
    """Coerces the column datatypes of a DataFrame using the provided schema.

    Parameters
    ----------
    schema : Mapping[str, pl.DataType]
        A dictionary-like object mapping column names to the desired data types.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        X_new = X.cast({pl.col(col).cast(dtype) for col, dtype in schema.items()})
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def time_to_arange(eager: bool = False):
    """Coerces time column into arange per entity.

    Assumes even-spaced time-series and homogenous start dates.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col, time_col = X.columns[:2]
        range_expr = pl.arange(0, pl.col(time_col).count()).alias(time_col)
        other_cols = pl.all().exclude(time_col)
        X_new = (
            X.groupby(entity_col)
            .agg([range_expr, other_cols])
            .explode(pl.all().exclude(entity_col))
            .select(
                [
                    entity_col,
                    pl.col(time_col).cast(pl.Int32),
                    pl.all().exclude([entity_col, time_col]),
                ]
            )
        )
        if eager:
            X_new = X_new.collect(streaming=True)
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def resample(freq: str, agg_method: str, impute_method: Union[str, int, float]):
    """
    Resamples and transforms a DataFrame using the specified frequency, aggregation method, and imputation method.

    Parameters
    ----------
    freq : str
        Offset alias supported by Polars.
    agg_method : str
        The aggregation method to use for resampling. Supported values are 'sum', 'mean', and 'median'.
    impute_method : Union[str, int, float]
        The method used for imputing missing values. If a string, supported values are 'ffill' (forward fill)
        and 'bfill' (backward fill). If an int or float, missing values will be filled with the provided value.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col, time_col, target_col = X.columns
        agg_exprs = {
            "sum": pl.sum(target_col),
            "mean": pl.mean(target_col),
            "median": pl.median(target_col),
        }
        X_new = (
            # Defensive resampling
            X.lazy()
            .groupby_dynamic(time_col, every=freq, by=entity_col)
            .agg(agg_exprs[agg_method])
            # Must defensive sort columns otherwise time_col and target_col
            # positions are incorrectly swapped in lazy
            .select([entity_col, time_col, target_col])
            # Impute gaps after reindex
            .pipe(experimental_impute(impute_method))
            # Defensive fill null with 0 for impute method `ffill`
            .fill_null(0)
        )
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def lag(lags: List[int]):
    """Applies lag transformation to a LazyFrame.

    Parameters
    ----------
    lags : List[int]
        A list of lag values to apply.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col = X.columns[0]
        time_col = X.columns[1]
        max_lag = max(lags)
        lagged_series = [
            (
                pl.all()
                .exclude([entity_col, time_col])
                .shift(lag)
                .over(entity_col)
                .suffix(f"__lag_{lag}")
            )
            for lag in lags
        ]
        X_new = (
            # Pre-sorting seems to improve performance by ~20%
            X.sort(by=[entity_col, time_col])
            .select(
                pl.col(entity_col).set_sorted(),
                pl.col(time_col).set_sorted(),
                *lagged_series,
            )
            .groupby(entity_col)
            .agg(pl.all().slice(max_lag))
            .explode(pl.all().exclude(entity_col))
        )
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def one_hot_encode(drop_first: bool = False):
    """Encode categorical features as a one-hot numeric array.

    Parameters
    ----------
    drop_first : bool
        Drop the first one hot feature.

    Raises
    ------
    ValueError
        if X passed into `transform_new` contains unknown categories.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        # NOTE: You can't do lazy one hot encoding because
        # polars needs to know the unique values in the selected columns
        cat_cols = X.select(pl.col(pl.Categorical)).columns
        X_new = X.collect().to_dummies(
            columns=cat_cols, drop_first=drop_first, separator="__"
        )
        artifacts = {
            "X_new": X_new,
            "dummy_cols": X_new.columns,
        }
        return artifacts

    def invert(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        return NotImplemented

    def transform_new(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        cat_cols = X.select(pl.col(pl.Categorical)).columns
        dummy_cols = state.artifacts["dummy_cols"]
        X_new = X.collect().to_dummies(columns=cat_cols, separator="__")
        if len(set(dummy_cols) & set(X_new.columns)) < len(dummy_cols):
            raise ValueError(
                f"Missing categories: {set(dummy_cols) & set(X_new.columns)}"
            )
        return X_new

    return transform, invert, transform_new


@transformer
def roll(
    window_sizes: List[int],
    stats: List[Literal["mean", "min", "max", "mlm", "sum", "std", "cv"]],
    freq: str,
):
    """
    Performs rolling window calculations on specified columns of a DataFrame.

    Parameters
    ----------
    window_sizes : List[int]
        A list of integers representing the window sizes for the rolling calculations.
    stats : List[Literal["mean", "min", "max", "mlm", "sum", "std", "cv"]]
        A list of statistical measures to calculate for each rolling window.\n
        Supported values are:\n
        - 'mean' for mean
        - 'min' for minimum
        - 'max' for maximum
        - 'mlm' for maximum minus minimum
        - 'sum' for sum
        - 'std' for standard deviation
        - 'cv' for coefficient of variation
    freq : str
        Offset alias supported by Polars.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col, time_col = X.columns[:2]
        offset_n, offset_alias = _strip_freq_alias(freq)
        values = pl.all().exclude([entity_col, time_col])
        stat_exprs = {
            "mean": lambda w: values.mean().suffix(f"__rolling_mean_{w}"),
            "min": lambda w: values.min().suffix(f"__rolling_min_{w}"),
            "max": lambda w: values.max().suffix(f"__rolling_max_{w}"),
            "mlm": lambda w: (values.max() - values.min()).suffix(f"__rolling_mlm_{w}"),
            "sum": lambda w: values.sum().suffix(f"__rolling_sum_{w}"),
            "std": lambda w: values.std().suffix(f"__rolling_std_{w}"),
            "cv": lambda w: (values.std() / values.mean()).suffix(f"__rolling_cv_{w}"),
        }
        # Degrees of freedom
        X_all = [
            (
                X.sort([entity_col, time_col])
                .groupby_dynamic(
                    index_column=time_col,
                    by=entity_col,
                    every=freq,
                    period=f"{offset_n * w}{offset_alias}",
                )
                .agg([stat_exprs[stat](w) for stat in stats])
                # NOTE: Must lag by 1 to avoid data leakage
                .select([entity_col, time_col, values.shift(w).over(entity_col)])
            )
            for w in window_sizes
        ]
        # Join all window lazy operations
        X_rolling = X_all[0]
        for X_window in X_all[1:]:
            X_rolling = X_rolling.join(X_window, on=[entity_col, time_col], how="outer")
        # Defensive join to match original X index
        X_new = X.join(X_rolling, on=[entity_col, time_col], how="left").select(
            X_rolling.columns
        )

        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def scale(use_mean: bool = True, use_std: bool = True, rescale_bool: bool = False):
    """
    Performs scaling and rescaling operations on the numeric columns of a DataFrame.

    Parameters
    ----------
    use_mean : bool
        Whether to subtract the mean from the numeric columns. Defaults to True.
    use_std : bool
        Whether to divide the numeric columns by the standard deviation. Defaults to True.
    rescale_bool : bool
        Whether to rescale boolean columns to the range [-1, 1]. Defaults to False.
    """

    if not (use_mean or use_std):
        raise ValueError("At least one of `use_mean` or `use_std` must be set to True")

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        idx_cols = X.columns[:2]
        entity_col, time_col = idx_cols
        numeric_cols = X.select(PL_NUMERIC_COLS(entity_col, time_col)).columns
        boolean_cols = None
        _mean = None
        _std = None
        if use_mean:
            _mean = X.groupby(entity_col).agg(
                PL_NUMERIC_COLS(entity_col, time_col).mean().suffix("_mean")
            )
            X = X.join(_mean, on=entity_col).select(
                idx_cols + [pl.col(col) - pl.col(f"{col}_mean") for col in numeric_cols]
            )
        if use_std:
            _std = X.groupby(entity_col).agg(
                PL_NUMERIC_COLS(entity_col, time_col).mean().suffix("_std")
            )
            X = X.join(_std, on=entity_col).select(
                idx_cols + [pl.col(col) - pl.col(f"{col}_std") for col in numeric_cols]
            )
        if rescale_bool:
            boolean_cols = X.select(pl.col(pl.Boolean)).columns
            X = X.with_columns(pl.col(pl.Boolean).cast(pl.Int8) * 2 - 1)
        artifacts = {
            "X_new": X,
            "numeric_cols": numeric_cols,
            "boolean_cols": boolean_cols,
            "_mean": _mean,
            "_std": _std,
        }
        return artifacts

    def invert(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        idx_cols = X.columns[:2]
        entity_col = idx_cols[0]
        artifacts = state.artifacts
        numeric_cols = artifacts["numeric_cols"]
        if use_std:
            _std = artifacts["_std"]
            X = X.join(_std, on=entity_col, how="left").select(
                idx_cols + [pl.col(col) * pl.col(f"{col}_std") for col in numeric_cols]
            )
        if use_mean:
            _mean = artifacts["_mean"]
            X = X.join(_mean, on=entity_col, how="left").select(
                idx_cols + [pl.col(col) + pl.col(f"{col}_mean") for col in numeric_cols]
            )
        if rescale_bool:
            X = X.with_columns(pl.col(artifacts["boolean_cols"]).cast(pl.Int8))
        return X

    def transform_new(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        artifacts = state.artifacts
        idx_cols = X.columns[:2]
        numeric_cols = state.artifacts["numeric_cols"]
        _mean = artifacts["_mean"]
        _std = artifacts["_std"]
        if use_mean:
            X = X.join(_mean, on=idx_cols, how="left").select(
                idx_cols + [pl.col(col) - pl.col(f"{col}_mean") for col in numeric_cols]
            )
        if use_std:
            X = X.join(_std, on=idx_cols, how="left").select(
                idx_cols + [pl.col(col) / pl.col(f"{col}_std") for col in numeric_cols]
            )
        if rescale_bool:
            X = X.with_columns(pl.col(pl.Boolean).cast(pl.Int8) * 2 - 1)
        return X

    return transform, invert, transform_new


@transformer
def experimental_impute(
    method: Union[
        Literal["mean", "median", "fill", "ffill", "bfill", "interpolate"],
        Union[int, float],
    ]
):
    """
    [EXPERIMENTAL] Performs missing value imputation on numeric columns of a DataFrame grouped by entity.

    Parameters
    ----------
    method : Union[str, int, float]
        The imputation method to use.

        Supported methods are:\n
        - 'mean': Replace missing values with the mean of the corresponding column.
        - 'median': Replace missing values with the median of the corresponding column.
        - 'fill': Replace missing values with the mean for float columns and the median for integer columns.
        - 'ffill': Forward fill missing values.
        - 'bfill': Backward fill missing values.
        - 'interpolate': Interpolate missing values using linear interpolation.
        - int or float: Replace missing values with the specified constant.
    """

    def method_to_expr(entity_col, time_col):
        """Fill-in methods."""
        return {
            "mean": PL_NUMERIC_COLS(entity_col, time_col).fill_null(
                PL_NUMERIC_COLS(entity_col, time_col).mean().over(entity_col)
            ),
            "median": PL_NUMERIC_COLS(entity_col, time_col).fill_null(
                PL_NUMERIC_COLS(entity_col, time_col).median().over(entity_col)
            ),
            "fill": [
                cs.float().fill_null(cs.float().mean().over(entity_col)),
                cs.integer().fill_null(cs.integer().median().over(entity_col)),
            ],
            "ffill": PL_NUMERIC_COLS(entity_col, time_col)
            .fill_null(strategy="forward")
            .over(entity_col),
            "bfill": PL_NUMERIC_COLS(entity_col, time_col)
            .fill_null(strategy="backward")
            .over(entity_col),
            "interpolate": PL_NUMERIC_COLS(entity_col, time_col)
            .interpolate()
            .over(entity_col),
        }

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col, time_col = X.columns[:2]
        if isinstance(method, int) or isinstance(method, float):
            expr = PL_NUMERIC_COLS(entity_col, time_col).fill_null(pl.lit(method))
        else:
            expr = method_to_expr(entity_col, time_col)[method]
        X_new = X.with_columns(expr)
        return {"X_new": X_new}

    return transform


@transformer
def diff(order: int, sp: int = 1):
    """Difference time-series in panel data given order and seasonal period.

    Parameters
    ----------
    order : int
        The order to difference.
    sp : int
        Seasonal periodicity.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        def _diff(X):
            X_new = (
                X.groupby(entity_col, maintain_order=True)
                .agg([pl.col(time_col), cs.float() - cs.float().shift(sp)])
                .explode(pl.all().exclude(entity_col))
            )
            return X_new

        idx_cols = X.columns[:2]
        entity_col = idx_cols[0]
        time_col = idx_cols[1]
        X = X.with_columns(pl.col(pl.Categorical).cast(pl.Utf8))

        X_first, X_last = pl.collect_all(
            [
                X.groupby(entity_col).head(1),
                X.groupby(entity_col).tail(1),
            ]
        )
        for _ in range(order):
            X = _diff(X)

        # Drop null
        artifacts = {
            "X_new": X.drop_nulls(),
            "X_first": X_first.lazy(),
            "X_last": X_last.lazy(),
        }
        return artifacts

    def invert(
        state: ModelState, X: pl.LazyFrame, from_last: bool = False
    ) -> pl.LazyFrame:
        def _inverse_diff(X: pl.LazyFrame):
            X_new = (
                X.groupby(entity_col, maintain_order=True)
                .agg([pl.col(time_col), cs.float().cumsum()])
                .explode(pl.all().exclude(entity_col))
            )
            return X_new

        artifacts = state.artifacts
        entity_col = X.columns[0]
        time_col = X.columns[1]
        idx_cols = entity_col, time_col

        X_cutoff = artifacts["X_last"] if from_last else artifacts["X_first"]
        X_new = (
            pl.concat(
                [
                    X,
                    X_cutoff.select(
                        pl.col(col).cast(dtype) for col, dtype in X.schema.items()
                    ),
                ],
                how="diagonal",
            )
            .drop_nulls()
            .sort(idx_cols)
        )
        for _ in range(order):
            X_new = _inverse_diff(X_new)  # noqa: F821

        return X.select(idx_cols).join(X_new, on=idx_cols, how="left")

    return transform, invert


@transformer
def boxcox(method: str = "mle"):
    """Applies the Box-Cox transformation to numeric columns in a DataFrame.

    Parameters
    ----------
    method : str
        The method used to determine the lambda parameter of the Box-Cox transformation.

        Supported methods:\n
        - `mle`: maximum likelihood estimation
        - `pearsonr`: Pearson correlation coefficient
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        idx_cols = X.columns[:2]
        entity_col, time_col = idx_cols
        gb = X.groupby(X.columns[0])
        # Step 1. Compute optimal lambdas
        lmbds = gb.agg(
            PL_NUMERIC_COLS(entity_col, time_col)
            .apply(lambda x: boxcox_normmax(x, method=method))
            .cast(pl.Float64())
            .suffix("__lmbd")
        )
        # Step 2. Transform
        cols = X.select(PL_NUMERIC_COLS(entity_col, time_col)).columns
        X_new = X.join(lmbds, on=entity_col, how="left").select(
            idx_cols
            + [
                pl.when(pl.col(f"{col}__lmbd") == 0)
                .then(pl.col(col).log())
                .otherwise(
                    (pl.col(col) ** pl.col(f"{col}__lmbd") - 1) / pl.col(f"{col}__lmbd")
                )
                for col in cols
            ]
        )
        artifacts = {"X_new": X_new, "lmbds": lmbds}
        return artifacts

    def invert(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        idx_cols = X.columns[:2]
        lmbds = state.artifacts["lmbds"]
        cols = X.select(PL_NUMERIC_COLS(state.time)).columns
        X_new = X.join(lmbds, on=X.columns[0], how="left", suffix="__lmbd").select(
            idx_cols
            + [
                pl.when(pl.col(f"{col}__lmbd") == 0)
                .then(pl.col(col).exp())
                .otherwise(
                    (pl.col(f"{col}__lmbd") * pl.col(col) + 1)
                    ** (1 / pl.col(f"{col}__lmbd"))
                )
                for col in cols
            ]
        )
        return X_new

    return transform, invert


@transformer
def trim(direction: Literal["both", "left", "right"] = "both"):
    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        entity_col, time_col = X.columns[:2]
        maxmin = (
            X.groupby(entity_col)
            .agg(pl.col(time_col).min())
            .select(pl.col(time_col).max())
        )
        minmax = (
            X.groupby(entity_col)
            .agg(pl.col(time_col).max())
            .select(pl.col(time_col).min())
        )
        start, end = pl.collect_all([minmax, maxmin])
        start, end = start.item(), end.item()
        if direction == "both":
            expr = (pl.col(time_col) >= start) & (pl.col(time_col) <= end)
        elif direction == "left":
            expr = pl.col(time_col) >= start
        else:
            expr = pl.col(time_col) <= start
        X_new = X.filter(expr)
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def add_fourier_coefs(period:Union[float, int], no_terms:int, cos_terms:int = None, sin_terms:int = None):
    """Applies lag transformation to a LazyFrame.

    Parameters
    ----------
    period: int
        the assumed period for seasonality
    no_terms : int
        the number of fourier terms to include
    cos_terms : int
        the number of fourier cosine terms to include
    sin_terms : int
        the number of fourier cosine terms to include
    """
    if no_terms > (period - 1):
        raise Warning("The number of Fourier coefficients must be less than period")
    if (sin_terms is not None and cos_terms is not None):
        if sin_terms + cos_terms != no_terms:
            raise Warning("The number of Fourier sine coefficients and cosine coefficients must sum to no_terms")
        
    if cos_terms is None and sin_terms is None:
        cos_terms = no_terms  // 2 + no_terms % 2
        sin_terms = no_terms // 2
    elif cos_terms is None and sin_terms is not None:
        cos_terms = no_terms - sin_terms
    elif cos_terms is not None and sin_terms is None:
        sin_terms = no_terms - cos_terms

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        nonlocal cos_terms, sin_terms

        entity_col = X.columns[0]
        time_col = X.columns[1]

        new_col_names = ([f'fourier_coef_cos_period_{period}_k_{k}' for k in range(1, cos_terms + 1)] 
                        + [f'fourier_coef_sin_period_{period}_k_{k}' for k in range(1, sin_terms + 1)]
        )

        X_new = X.with_columns(
            pl.col(time_col)
            .arg_sort()
            .over(entity_col)
            .alias('fourier_coef')
            ).with_columns(
                [
                    np.cos(2 * np.pi * k * pl.col('fourier_coef') / period)
                    .cast(pl.Float32)
                    .alias(f'fourier_coef_cos_period_{period}_k_{k}')
                    for k in range(1, cos_terms + 1)
                ]
                + [
                    np.sin(2 * np.pi * k * pl.col('fourier_coef') / period)
                    .cast(pl.Float32)
                    .alias(f'fourier_coef_sin_period_{period}_k_{k}')
                    for k in range(1, sin_terms + 1)
                ]
            )
        X_new = X_new.with_columns(
            [pl.when(pl.col(col).abs() < 1e-8)
            .then(pl.lit(0).alias(col))
            .otherwise(pl.col(col))
            for col in new_col_names]
        )

        artifacts = {"X_new": X_new, 'fourier_cols': new_col_names}
        return artifacts

    return transform


@transformer
def add_changes(lags:List[int],
                stats:List[Literal['diff', 'pct_change', 'log_change', 'log1_change']],
                cols:List[str] = None):
    """Applies differencing transformation to a LazyFrame.

    Parameters
    ----------
    lags: List[int]
        A list of lags denoting the differences to compute
    stats: List[Literal]
        List of differencing functions to create features for.
    cols: List[str]
        List of columns to compute differences for. By default, all columns are included
    """


    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        nonlocal cols
        entity_col = X.columns[0]
        time_col = X.columns[1]

        if cols is None:
            values = pl.all().exclude([entity_col, time_col])
        else:
            values = pl.col(cols)

        stat_exprs = {
            "diff": lambda lag: values.diff(lag),
            "pct_change": lambda lag: values.pct_change(lag),
            "log_change": lambda lag: pl.log(values).diff(lag),
            "log1_change": lambda lag: pl.log(1 + values).diff(lag),
        }

        X_new = X.sort(by=[entity_col, time_col]).with_columns(
            [stat_exprs[stat](lag).over(entity_col).suffix(f"__{stat}_{lag}") for lag in lags for stat in stats]
            )
        
        artifacts = {"X_new": X_new}
        return artifacts

    return transform


@transformer
def add_fractional_differences(orders:List[int], windows: int, cols:List[str] = None):
    """Applies fractional differencing transformation to a LazyFrame.

    Parameters
    ----------
    orders: List[int]
        A list of differencing orders to compute
    windows: List[int]
        Maximum cutoff window to compute fractional difference
    cols: List[str]
        List of columns to compute differences for. By default, all columns are included
    """

    def _fdiff(order ,window):
        def fdiff(x):
            coefs = order + np.array([1 - order] +  list(range(0, -window + 1, -1)))
            coefs *= ((-1) **   np.arange(window))
            coefs = np.cumprod(coefs / np.array([1] + list(range(1, window))))
            return (x * coefs[::-1]).sum()
        return fdiff
    
    

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        nonlocal cols
        entity_col = X.columns[0]
        time_col = X.columns[1]

        if cols is None:
            values = pl.all().exclude([entity_col, time_col])
        else:
            values = pl.col(cols)


        X_new = X.sort(by=[entity_col, time_col]).with_columns(
            [values.rolling_apply(_fdiff(order ,window), window).suffix(f'__fdiff_d{order}_{window}') for order in orders for window in windows]
        )
        artifacts = {"X_new": X_new}
        return artifacts

    return transform



@transformer
def detrend_ols(group_col:Union[List[str], str] = None, with_intercept:bool = False, cols:List[str]=None):
    """Encode categorical features as a one-hot numeric array.

    Parameters
    ----------
    group_col: Union[List[str], str]
        Column to compute
    with_intercept: bool
        linearly detrend assuming with intercept
    cols : List[str]
        Which columns to detrend

    Raises
    ------
    ValueError
        if X passed into `transform_new` contains unknown categories.
    """

    def transform(X: pl.LazyFrame) -> pl.LazyFrame:
        nonlocal group_col, cols

        entity_col = X.columns[0]
        time_col = X.columns[1]

        if cols is None:
            cols = list(X.columns[2:])

        if group_col is None:
            group_col = entity_col
        

        X_new = X.sort(by=[entity_col, time_col])
        unique = X.groupby(group_col, maintain_order=True).agg(pl.col(time_col).unique()).explode(time_col)
        unique = unique.with_columns((pl.col(time_col).rank().over(group_col) - 1).cast(pl.Float32).alias('index'))
        X_new = X.join(unique, on = [group_col, time_col], how = 'left')
    


        if with_intercept:
            means = X_new.groupby(group_col).agg(pl.col(['index'] + cols)).mean()
            X_new = X_new.with_columns(
                [pl.col(col).apply(lambda x: x - x.mean()).over(group_col) for col in ['index'] + cols]
                )
        else:
            means = X_new.select(pl.col(group_col)).unique().with_columns([pl.lit(0.0).alias(col) for col in ['index'] + cols])

        betas = X_new.groupby(group_col).agg([(pl.cov(pl.col(col), pl.col('index')) / pl.col('index').var()).cast(pl.Float32) for col in cols])   
        X_new = X_new.with_columns(
            [pl.col(col) - (pl.cov(pl.col(col), pl.col('index')) / pl.col('index').var()).over(group_col)  * pl.col('index')  for  col in cols]
            )
            
        # keep tarck
        artifacts = {
            "X_new": X_new,
            "means": means,
            "betas": betas,
            'group_col': group_col,
            'cols': cols,
            'time_mapping':unique
        }
        return artifacts

    def invert(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        means = state.artifacts["means"]
        betas = state.artifacts["betas"] # (y - ybar) - Beta(x - xbar) 
        cols = X.select(PL_NUMERIC_COLS(state.time)).columns
        X_new = (X.join(means, on=state.artifacts['group_col'], how="left", suffix="__mean")
                .join(betas, on=state.artifacts['group_col'], how="left", suffix="__beta")
                .with_columns(
            [pl.col(col) + pl.col(col + "__mean") + pl.col(col + "__beta") * (pl.col('index') - pl.col('index__mean')) for col in state.artifacts['cols']]
        )
        )
        return X_new

    def transform_new(state: ModelState, X: pl.LazyFrame) -> pl.LazyFrame:
        means = state.artifacts["means"].with_columns(pl.col(pl.Float32).cast(pl.Float64))
        betas = state.artifacts["betas"].with_columns(pl.col(pl.Float32).cast(pl.Float64)) # (y - ybar) - Beta(x - xbar) 
        
        date_mapping = state.artifacts['time_mapping']
        entity_col = X.columns[0]
        time_col = X.columns[1]

        X_new = X.join(date_mapping, on = [state.artifacts['group_col'], time_col], how = 'outer')
        X_new = X_new.sort(by=[entity_col, time_col])

        ### TODO: by default, resets unmapped dates to 1 + the latest time index

        unique_unmapped = X_new.filter(pl.col('index').is_null()).groupby(group_col, maintain_order=True).agg(pl.col(time_col).unique()).explode(time_col)
        unique_unmapped = unique_unmapped.with_columns((pl.col(time_col).rank().over(group_col) - 1).cast(pl.Float32).alias('index'))
        X_new = X_new.join(unique_unmapped, on = [group_col, time_col], how = 'left')
        X_new = X_new.with_columns(pl.col('index').fill_null(pl.col('index_right')) + pl.col('index').max().over(group_col))
        


        X_new = (X_new.join(means, on=state.artifacts['group_col'], how="left", suffix="__mean")
                .join(betas, on=state.artifacts['group_col'], how="left", suffix="__beta")
                .with_columns(
            [pl.col(col) - pl.col(col + "__mean") - pl.col(col + "__beta") * (pl.col('index') - pl.col('index__mean')) for col in state.artifacts['cols']]
        )
        )
        return X_new

    return transform, invert, transform_new