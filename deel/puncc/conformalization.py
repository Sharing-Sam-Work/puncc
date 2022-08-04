#
# This module provides the canvas for conformal prediction.
#


from deel.puncc.calibration import (
    BaseCalibrator,
    CvPlusCalibrator,
)
from deel.puncc.splitting import BaseSplitter
from deel.puncc.prediction import BasePredictor
import numpy as np
from copy import deepcopy


class ConformalPredictor:
    """Conformal predictor class.
    Attributes:
        predictor: point-based or interval-based model wrapper
        calibrator: nonconformity computation strategy and interval predictor
        splitter: fit/calibration split strategy
        method: method to handle the ensemble prediction and calibration
                in case the splitter is a K-fold-like strategy.
                - 'cv+': follow cv+ procedure to construct PIs based on the
                    k-fold estimators
        train: if False, prediction model(s) will not be (re)trained
    """

    def __init__(
        self,
        calibrator: BaseCalibrator,
        predictor: BasePredictor,
        splitter: BaseSplitter,
        method: str = "cv+",
        train: bool = True,
    ):
        self.calibrator = calibrator
        self.predictor = predictor
        self.splitter = splitter
        self.method = method
        self.train = train
        self._cv_cp_agg = None

    def get_residuals(self):
        if self._cv_cp_agg is None:
            return RuntimeError("Error: call 'fit' method first.")
        return self._cv_cp_agg.get_residuals()

    def get_weights(self):
        if self._cv_cp_agg is None:
            return RuntimeError("Error: call 'fit' method first.")
        return self._cv_cp_agg.get_weights()

    def fit(self, X: np.ndarray, y: np.ndarray, **kwargs) -> None:
        # Get split folds. Each fold split is a iterable of a quadruple that
        # contains fit and calibration data.
        splits = self.splitter(X, y)

        # Make local copies of the structure of the predictor and the calibrator.
        # In case of a K-fold like splitting strategy, these structures are
        # inherited by the predictor/calibrator used in each fold.
        predictor = deepcopy(self.predictor)
        calibrator = deepcopy(self.calibrator)

        # The Cross validation aggregator will aggregate the predictors and
        # calibrators fitted on each of the K splits.
        self._cv_cp_agg = CrossValCpAggregator(K=len(splits), method=self.method)

        # In case of multiple split folds, the predictor require training.
        # Having 'self.train' set to False is therefore an inconsistency
        if len(splits) > 1 and not self.train:
            raise Exception(
                "Model already trained. This is inconsistent with the"
                + "cross-validation strategy."
            )

        # Core loop: for each split (that contains fit and calib data):
        #   1- The predictor f_i is fitted of (X_fit, y_fit) (if necessary)
        #   2- The tuple (y_pred, y_pred_lo, y_pred_hi, var_pred) is predicted by f_i
        #   3- The calibrator is fitted to approximate the distribution of nonconformity scores
        for i, (X_fit, y_fit, X_calib, y_calib) in enumerate(splits):
            if self.train:
                predictor.fit(X_fit, y_fit, **kwargs)  # Fit K-fold predictor
            # Make sure that predictor is already trained if train arg is False
            elif self.train is False and predictor.is_trained is False:
                raise RuntimeError(
                    f"'train' argument is set to 'False' but model is not pre-trained"
                )
            if self.calibrator is not None:
                # Call predictor to estimate point, variability and/or
                # interval predictions
                (y_pred, y_pred_lo, y_pred_hi, var_pred) = predictor.predict(X_calib)

                # Compte/calibrate PIs
                calibrator.fit(
                    y_true=y_calib,
                    y_pred=y_pred,
                    X=X_calib,
                    var_pred=var_pred,
                    y_pred_lo=y_pred_lo,
                    y_pred_hi=y_pred_hi,
                )
            # Add predictor and calibrator to the collection that is used later
            # by the predict method
            self._cv_cp_agg.append_predictor(i, predictor)
            self._cv_cp_agg.append_calibrator(i, calibrator)

    def predict(
        self, X: np.ndarray, alpha: float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray,]:
        """Predict point, interval and variability estimates for X data.
        Args:
            X: features
            alpha: significance level
        Returns:
            A tuple composed of:
                y_pred (point prediction),
                y_pred_lower (lower PI bound),
                y_pred_upper (upper PI bound),
                var_pred (variability prediction)
        """
        if self._cv_cp_agg is None:
            raise RuntimeError("Error: call 'fit' method first.")
        return self._cv_cp_agg.predict(X, alpha)


class CrossValCpAggregator:
    """This class enables to aggregate predictions and calibrations
    from different K-folds.

    Attributes:
        K: number of folds
        predictors: collection of predictors fitted on the K-folds
        calibrators: collection of calibrators fitted on the K-folds
        agg_func: function called to aggregate the predictions of the K-folds
            estimators. Used only when method is 'aggregation'.
        method: method to handle the ensemble prediction and calibration.
                - 'cv+': follow cv+ procedure to construct PIs based on the
                k-fold estimators
    """

    def __init__(
        self,
        K: int,
        method: str = "cv+",
    ):
        self.K = K  # Number of K-folds
        self._predictors = dict()
        self._calibrators = dict()

        if method not in ("cv+"):
            return NotImplemented(
                f"Method {method} is not implemented. " + "Please choose 'cv+'."
            )
        self.method = method

    def append_predictor(self, id, predictor):
        self._predictors[id] = deepcopy(predictor)

    def append_calibrator(self, id, calibrator):
        self._calibrators[id] = deepcopy(calibrator)

    def get_residuals(self):
        """Get a dictionnary of residuals computed on the K-folds
        Returns:
            Dict of residual. Key: K-fold index, value: residuals iterable.
        """
        return {k: calibrator._residuals for k, calibrator in self._calibrators.items()}

    def get_weights(self):
        """Get a dictionnary of normalized weights computed on the K-folds
        Returns:
            Dict of normalized weights. Key: K-fold index,
                                        value: residuals iterable.
        """
        return {
            k: calibrator.get_weights() for k, calibrator in self._calibrators.items()
        }

    def predict(
        self, X: np.ndarray, alpha: float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:  #  type: ignore
        """Predict point, interval and variability estimates for X data.
        Args:
            X: features
            alpha: significance level
        Returns:
            A tuple composed of:
                y_pred (point prediction),
                y_pred_lower (lower PI bound),
                y_pred_upper (upper PI bound),
                var_pred (variability prediction)
        """
        assert (
            self._predictors.keys() == self._calibrators.keys()
        ), "K-fold predictors are not well calibrated."

        K = len(self._predictors.keys())  # Number of folds

        # No cross-val strategy if K = 1
        if K == 1:
            for k in self._predictors.keys():
                predictor = self._predictors[k]
                calibrator = self._calibrators[k]
                y_pred, y_pred_lo, y_pred_hi, var_pred = predictor.predict(X=X)
                y_lo, y_hi = calibrator.calibrate(
                    X=X,
                    alpha=alpha,
                    y_pred=y_pred,
                    var_pred=var_pred,
                    y_pred_lo=y_pred_lo,
                    y_pred_hi=y_pred_hi,
                )
                return (y_pred, y_lo, y_hi, var_pred)
        else:
            y_pred = None
            if self.method == "cv+":
                cvp_calibrator = CvPlusCalibrator(self._calibrators)
                y_lo, y_hi = cvp_calibrator.calibrate(
                    X=X,
                    kfold_predictors_dict=self._predictors,
                    alpha=alpha,
                )
                return (y_pred, y_lo, y_hi, None)  # type: ignore

            else:
                raise RuntimeError(
                    f"Method {self.method} is not implemented" + "Please choose 'cv+'."
                )
