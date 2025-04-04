from __future__ import annotations

import io
import logging
import warnings
from contextlib import redirect_stdout
from typing import TYPE_CHECKING

import anndata
import numpy as np
import pandas as pd
import torch
from anndata import AnnData

from scvi import REGISTRY_KEYS, settings
from scvi.data import AnnDataManager
from scvi.data.fields import CategoricalObsField, LayerField
from scvi.dataloaders import DataSplitter
from scvi.model._utils import get_max_epochs_heuristic
from scvi.model.base import BaseModelClass
from scvi.module import Classifier
from scvi.module.base import auto_move_data
from scvi.train import ClassifierTrainingPlan, LoudEarlyStopping, TrainRunner
from scvi.utils import setup_anndata_dsp
from scvi.utils._docstrings import devices_dsp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scvi.model import SCVI

logger = logging.getLogger(__name__)

LABELS_KEY = "_solo_doub_sim"


class SOLO(BaseModelClass):
    """Doublet detection in scRNA-seq :cite:p:`Bernstein20`.

    Original implementation: https://github.com/calico/solo.

    Most users will initialize the model using the class method
    :meth:`~scvi.external.SOLO.from_scvi_model`, which takes as
    input a pre-trained :class:`~scvi.model.SCVI` object.

    Parameters
    ----------
    adata
        AnnData object that has been registered via :meth:`~scvi.model.SCVI.setup_anndata`.
        Object should contain latent representation of real cells and doublets as `adata.X`.
        Object should also be registered, using `.X` and `labels_key="_solo_doub_sim"`.
    **classifier_kwargs
        Keyword args for :class:`~scvi.module.Classifier`

    Examples
    --------
    In the case of scVI trained with multiple batches:

    >>> adata = anndata.read_h5ad(path_to_anndata)
    >>> scvi.model.SCVI.setup_anndata(adata, batch_key="batch")
    >>> vae = scvi.model.SCVI(adata)
    >>> vae.train()
    >>> solo_batch_1 = scvi.external.SOLO.from_scvi_model(vae, restrict_to_batch="batch 1")
    >>> solo_batch_1.train()
    >>> solo_batch_1.predict()

    Otherwise:

    >>> adata = anndata.read_h5ad(path_to_anndata)
    >>> scvi.model.SCVI.setup_anndata(adata)
    >>> vae = scvi.model.SCVI(adata)
    >>> vae.train()
    >>> solo = scvi.external.SOLO.from_scvi_model(vae)
    >>> solo.train()
    >>> solo.predict()

    Notes
    -----
    Solo should be trained on one lane of data at a time. An
    :class:`~scvi.model.SCVI` instance that was trained with multiple
    batches can be used as input, but Solo should be created and run
    multiple times, each with a new `restrict_to_batch` in
    :meth:`~scvi.external.SOLO.from_scvi_model`.
    """

    def __init__(
        self,
        adata: AnnData,
        **classifier_kwargs,
    ):
        # TODO, catch user warning here and logger warning
        # about non count data
        super().__init__(adata)

        self.n_labels = 2
        self.module = Classifier(
            n_input=self.summary_stats.n_vars,
            n_labels=self.n_labels,
            logits=True,
            **classifier_kwargs,
        )
        self._model_summary_string = "Solo model"
        self.init_params_ = self._get_init_params(locals())

    @classmethod
    def from_scvi_model(
        cls,
        scvi_model: SCVI,
        adata: AnnData | None = None,
        restrict_to_batch: str | None = None,
        doublet_ratio: int = 2,
        **classifier_kwargs,
    ):
        """Instantiate a SOLO model from an scvi model.

        Parameters
        ----------
        scvi_model
            Pre-trained :class:`~scvi.model.SCVI` model. The AnnData object used to
            initialize this model should have only been setup with count data, and
            optionally a `batch_key`. Extra categorical and continuous covariates are
            currenty unsupported.
        adata
            Optional AnnData to use that is compatible with `scvi_model`.
        restrict_to_batch
            Batch category to restrict the SOLO model to if `scvi_model` was set up with
            a `batch_key`. This allows the model to be trained on the subset of cells
            belonging to `restrict_to_batch` when `scvi_model` was trained on multiple
            batches. If `None`, all cells are used.
        doublet_ratio
            Ratio of generated doublets to produce relative to number of
            cells in adata or length of indices, if not `None`.
        **classifier_kwargs
            Keyword args for :class:`~scvi.module.Classifier`

        Returns
        -------
        SOLO model
        """
        _validate_scvi_model(scvi_model, restrict_to_batch=restrict_to_batch)
        orig_adata_manager = scvi_model.adata_manager
        orig_batch_key_registry = orig_adata_manager.get_state_registry(REGISTRY_KEYS.BATCH_KEY)
        orig_labels_key_registry = orig_adata_manager.get_state_registry(REGISTRY_KEYS.LABELS_KEY)
        orig_batch_key = orig_batch_key_registry.original_key
        orig_labels_key = orig_labels_key_registry.original_key

        if len(orig_adata_manager.get_state_registry(REGISTRY_KEYS.CONT_COVS_KEY)) > 0:
            raise ValueError(
                "Initializing a SOLO model from SCVI with registered continuous "
                "covariates is currently unsupported."
            )
        if len(orig_adata_manager.get_state_registry(REGISTRY_KEYS.CAT_COVS_KEY)) > 0:
            raise ValueError(
                "Initializing a SOLO model from SCVI with registered categorical "
                "covariates is currently unsupported."
            )
        scvi_trained_with_batch = len(orig_batch_key_registry.categorical_mapping) > 1
        if not scvi_trained_with_batch and restrict_to_batch is not None:
            raise ValueError(
                "Cannot specify `restrict_to_batch` when initializing a SOLO model from SCVI "
                "not trained with multiple batches."
            )
        if scvi_trained_with_batch > 1 and restrict_to_batch is None:
            warnings.warn(
                "`restrict_to_batch` not specified but `scvi_model` was trained with "
                "multiple batches. Doublets will be simulated using the first batch.",
                UserWarning,
                stacklevel=settings.warnings_stacklevel,
            )

        if adata is not None:
            adata_manager = orig_adata_manager.transfer_fields(adata)
            cls.register_manager(adata_manager)
        else:
            adata_manager = orig_adata_manager
        adata = adata_manager.adata

        if restrict_to_batch is not None:
            batch_mask = adata.obs[orig_batch_key] == restrict_to_batch
            if np.sum(batch_mask) == 0:
                raise ValueError(
                    "Batch category given to restrict_to_batch not found.\n"
                    + "Available categories: {}".format(
                        adata.obs[orig_batch_key].astype("category").cat.categories
                    )
                )
            # indices in adata with restrict_to_batch category
            batch_indices = np.where(batch_mask)[0]
        else:
            # use all indices
            batch_indices = None

        # anndata with only generated doublets
        doublet_adata = cls.create_doublets(
            adata_manager, indices=batch_indices, doublet_ratio=doublet_ratio
        )
        # if scvi wasn't trained with batch correction having the
        # zeros here does nothing.
        doublet_adata.obs[orig_batch_key] = (
            restrict_to_batch
            if restrict_to_batch is not None
            else orig_adata_manager.get_state_registry(
                REGISTRY_KEYS.BATCH_KEY
            ).categorical_mapping[0]
        )

        # Create dummy labels column set to first label in adata (does not affect inference).
        dummy_label = orig_labels_key_registry.categorical_mapping[0]
        doublet_adata.obs[orig_labels_key] = dummy_label

        # if model is using observed lib size, needs to get lib sample
        # which is just observed lib size on log scale
        give_mean_lib = not scvi_model.module.use_observed_lib_size

        # get latent representations and make input anndata
        latent_rep = scvi_model.get_latent_representation(adata, indices=batch_indices)
        lib_size = scvi_model.get_latent_library_size(
            adata, indices=batch_indices, give_mean=give_mean_lib
        )
        latent_adata = AnnData(np.concatenate([latent_rep, np.log(lib_size)], axis=1))
        latent_adata.obs[LABELS_KEY] = "singlet"
        orig_obs_names = adata.obs_names
        latent_adata.obs_names = (
            orig_obs_names[batch_indices] if batch_indices is not None else orig_obs_names
        )

        logger.info("Creating doublets, preparing SOLO model.")
        f = io.StringIO()
        with redirect_stdout(f):
            doublet_latent_rep = scvi_model.get_latent_representation(doublet_adata)
            doublet_lib_size = scvi_model.get_latent_library_size(
                doublet_adata, give_mean=give_mean_lib
            )
            doublet_adata = AnnData(
                np.concatenate([doublet_latent_rep, np.log(doublet_lib_size)], axis=1)
            )
            doublet_adata.obs[LABELS_KEY] = "doublet"

            full_adata = anndata.concat([latent_adata, doublet_adata])
            cls.setup_anndata(full_adata, labels_key=LABELS_KEY)
        return cls(full_adata, **classifier_kwargs)

    @classmethod
    def create_doublets(
        cls,
        adata_manager: AnnDataManager,
        doublet_ratio: int,
        indices: Sequence[int] | None = None,
        seed: int = 1,
    ) -> AnnData:
        """Simulate doublets.

        Parameters
        ----------
        adata
            AnnData object setup with setup_anndata.
        doublet_ratio
            Ratio of generated doublets to produce relative to number of
            cells in adata or length of indices, if not `None`.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        seed
            Seed for reproducibility
        """
        adata = adata_manager.adata
        n_obs = adata.n_obs if indices is None else len(indices)
        num_doublets = doublet_ratio * n_obs

        # counts can be in many locations, this uses where it was registered in setup
        x = adata_manager.get_from_registry(REGISTRY_KEYS.X_KEY)
        if indices is not None:
            x = x[indices]

        random_state = np.random.RandomState(seed=seed)
        parent_inds = random_state.choice(n_obs, size=(num_doublets, 2))
        doublets = x[parent_inds[:, 0]] + x[parent_inds[:, 1]]

        doublets_ad = AnnData(doublets)
        doublets_ad.var_names = adata.var_names
        doublets_ad.obs_names = [f"sim_doublet_{i}" for i in range(num_doublets)]

        # if adata setup with a layer, need to add layer to doublets adata
        layer = adata_manager.data_registry[REGISTRY_KEYS.X_KEY].attr_key
        if layer is not None:
            doublets_ad.layers[layer] = doublets

        return doublets_ad

    @devices_dsp.dedent
    def train(
        self,
        max_epochs: int = 400,
        lr: float = 1e-3,
        accelerator: str = "auto",
        devices: int | list[int] | str = "auto",
        train_size: float | None = None,
        validation_size: float | None = None,
        shuffle_set_split: bool = True,
        batch_size: int = 128,
        datasplitter_kwargs: dict | None = None,
        plan_kwargs: dict | None = None,
        early_stopping: bool = True,
        early_stopping_patience: int = 30,
        early_stopping_warmup_epochs: int = 0,
        early_stopping_min_delta: float = 0.0,
        **kwargs,
    ):
        """Trains the model.

        Parameters
        ----------
        max_epochs
            Number of epochs to train for
        lr
            Learning rate for optimization.
        %(param_accelerator)s
        %(param_devices)s
        train_size
            Size of training set in the range [0.0, 1.0].
        validation_size
            Size of the test set. If `None`, defaults to 1 - `train_size`. If
            `train_size + validation_size < 1`, the remaining cells belong to a test set.
        shuffle_set_split
            Whether to shuffle indices before splitting. If `False`, the val, train, and test set
            are split in the sequential order of the data according to `validation_size` and
            `train_size` percentages.
        batch_size
            Minibatch size to use during training.
        datasplitter_kwargs
            Additional keyword arguments passed into :class:`~scvi.dataloaders.DataSplitter`.
        plan_kwargs
            Keyword args for :class:`~scvi.train.ClassifierTrainingPlan`.
        early_stopping
            Adds callback for early stopping on validation_loss
        early_stopping_patience
            Number of times early stopping metric can not improve over early_stopping_min_delta
        early_stopping_warmup_epochs
            Wait for a certain number of warm-up epochs before the early stopping starts monitoring
        early_stopping_min_delta
            Threshold for counting an epoch torwards patience
            `train()` will overwrite values present in `plan_kwargs`, when appropriate.
        **kwargs
            Other keyword args for :class:`~scvi.train.Trainer`.
        """
        update_dict = {
            "lr": lr,
        }
        if plan_kwargs is not None:
            plan_kwargs.update(update_dict)
        else:
            plan_kwargs = update_dict

        datasplitter_kwargs = datasplitter_kwargs or {}

        if early_stopping:
            early_stopping_callback = [
                LoudEarlyStopping(
                    monitor="validation_loss" if train_size != 1.0 else "train_loss",
                    min_delta=early_stopping_min_delta,
                    patience=early_stopping_patience,
                    mode="min",
                    warmup_epochs=early_stopping_warmup_epochs,
                )
            ]
            if "callbacks" in kwargs:
                kwargs["callbacks"] += early_stopping_callback
            else:
                kwargs["callbacks"] = early_stopping_callback
            kwargs["check_val_every_n_epoch"] = 1

        if max_epochs is None:
            max_epochs = get_max_epochs_heuristic(self.adata.n_obs)

        plan_kwargs = plan_kwargs if isinstance(plan_kwargs, dict) else {}

        data_splitter = DataSplitter(
            self.adata_manager,
            train_size=train_size,
            validation_size=validation_size,
            shuffle_set_split=shuffle_set_split,
            batch_size=batch_size,
            **datasplitter_kwargs,
        )
        training_plan = ClassifierTrainingPlan(self.module, **plan_kwargs)
        runner = TrainRunner(
            self,
            training_plan=training_plan,
            data_splitter=data_splitter,
            max_epochs=max_epochs,
            accelerator=accelerator,
            devices=devices,
            **kwargs,
        )
        return runner()

    @torch.inference_mode()
    def predict(
        self,
        soft: bool = True,
        include_simulated_doublets: bool = False,
        return_logits: bool = False,
    ) -> pd.DataFrame:
        """Return doublet predictions.

        Parameters
        ----------
        soft
            Return probabilities instead of class label.
        include_simulated_doublets
            Return probabilities for simulated doublets as well.
        return_logits
            Whether to return logits instead of probabilities when ``soft`` is ``True``.

        Returns
        -------
        DataFrame with prediction, index corresponding to cell barcode.
        """
        warnings.warn(
            "Prior to scvi-tools 1.1.3, `SOLO.predict` with `soft=True` (the default option) "
            "returned logits instead of probabilities. This behavior has since been corrected to "
            "return probabiltiies. The previous behavior can be replicated by passing in "
            "`return_logits=True`.",
            UserWarning,
            stacklevel=settings.warnings_stacklevel,
        )

        adata = self._validate_anndata(None)
        scdl = self._make_data_loader(adata=adata)

        @auto_move_data
        def auto_forward(module, x):
            return module(x)

        y_pred = []
        for _, tensors in enumerate(scdl):
            pred = auto_forward(self.module, tensors[REGISTRY_KEYS.X_KEY])
            pred = torch.nn.functional.softmax(pred, dim=-1) if not return_logits else pred
            y_pred.append(pred.cpu())

        y_pred = torch.cat(y_pred).numpy()

        label = self.adata.obs["_solo_doub_sim"].values.ravel()
        mask = label == "singlet" if not include_simulated_doublets else slice(None)

        preds = y_pred[mask]

        cols = self.adata_manager.get_state_registry(REGISTRY_KEYS.LABELS_KEY).categorical_mapping
        preds_df = pd.DataFrame(preds, columns=cols, index=self.adata.obs_names[mask])

        if not soft:
            preds_df = preds_df.idxmax(axis=1)

        return preds_df

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        labels_key: str | None = None,
        layer: str | None = None,
        **kwargs,
    ):
        """%(summary)s.

        Parameters
        ----------
        %(param_labels_key)s
        %(param_layer)s
        """
        setup_method_args = cls._get_setup_method_args(**locals())
        anndata_fields = [
            LayerField(REGISTRY_KEYS.X_KEY, layer, is_count_data=False),
            CategoricalObsField(REGISTRY_KEYS.LABELS_KEY, labels_key),
        ]
        adata_manager = AnnDataManager(fields=anndata_fields, setup_method_args=setup_method_args)
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)


def _validate_scvi_model(scvi_model: SCVI, restrict_to_batch: str):
    if scvi_model.summary_stats.n_batch > 1 and restrict_to_batch is None:
        warnings.warn(
            "Solo should only be trained on one lane of data using `restrict_to_batch`. "
            "Performance may suffer.",
            UserWarning,
            stacklevel=settings.warnings_stacklevel,
        )
