# %%
"""Train VAE with archetypes vectors."""

# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.4
#   kernelspec:
#     display_name: scvi
#     language: python
#     name: python3
# ---

# %%
""" DO NOT REMOVE THIS COMMENT!!!
TO use this script, you need to add the training plan to use the DualVAETrainingPlan (version 1.2.2.post2) class in scVI library.
in _training_mixin.py, line 131, you need to change the line:
training_plan = self._training_plan_cls(self.module, **plan_kwargs) # existing line
self._training_plan = training_plan # add this line

"""
import importlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from pprint import pprint
from time import time

import anndata
import mlflow
import numpy as np
import pandas as pd
import scanpy as sc
import torch
from sklearn.preprocessing import normalize

from CODEX_RNA_seq.neighborhood_utils import clean_uns_for_h5ad

# Add repository root to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Set working directory to project root
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from pathlib import Path

import plotting_functions as pf

import bar_nick_utils
import CODEX_RNA_seq.logging_functions
import CODEX_RNA_seq.metrics
import CODEX_RNA_seq.training_utils

importlib.reload(pf)
importlib.reload(bar_nick_utils)
importlib.reload(CODEX_RNA_seq.logging_functions)
importlib.reload(CODEX_RNA_seq.metrics)
importlib.reload(CODEX_RNA_seq.training_utils)

from CODEX_RNA_seq.logging_functions import log_epoch_end, log_step

# Import training utilities
from CODEX_RNA_seq.training_utils import (
    Tee,
    calculate_post_training_metrics,
    clear_memory,
    generate_visualizations,
    log_memory_usage,
    log_parameters,
    match_cells_and_calculate_distances,
    process_latent_spaces,
    run_cell_type_clustering_loss,
    save_results,
    setup_and_train_model,
)


def validate_scvi_training_mixin():
    """Validate that the required line exists in scVI's _training_mixin.py file."""
    try:
        # Import scvi to get the actual module path
        import scvi

        scvi_path = scvi.__file__
        base_dir = os.path.dirname(os.path.dirname(scvi_path))
        training_mixin_path = os.path.join(base_dir, "scvi", "model", "base", "_training_mixin.py")

        if not os.path.exists(training_mixin_path):
            raise FileNotFoundError(f"Could not find _training_mixin.py at {training_mixin_path}")

        # Read the file
        with open(training_mixin_path, "r") as f:
            lines = f.readlines()

        # Check if the required line exists
        required_line = "self._training_plan = training_plan"
        line_found = any(required_line in line for line in lines)

        if not line_found:
            raise RuntimeError(
                f"Required line '{required_line}' not found in {training_mixin_path}. "
                "Please add this line after the training_plan assignment."
            )
        print("✓ scVI training mixin validation passed")
    except Exception as e:
        raise RuntimeError(
            f"Failed to validate scVI training mixin: {str(e)}\n"
            "Please ensure you have modified the scVI library as described in the comment above."
        )


# Validate scVI training mixin before proceeding
validate_scvi_training_mixin()

# Set up paths once
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.chdir(project_root)
import warnings
from datetime import datetime
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import plotting_functions as pf
import scanpy as sc
import scvi
import torch
from anndata import AnnData
from plotting_functions import (
    plot_latent_pca_both_modalities_by_celltype,
    plot_latent_pca_both_modalities_cn,
    plot_rna_protein_matching_means_and_scale,
    plot_similarity_loss_history,
)
from scipy.sparse import issparse
from scvi.model import SCVI
from scvi.train import TrainingPlan
from torch.nn.functional import normalize

import bar_nick_utils

# Force reimport internal modules
importlib.reload(pf)
importlib.reload(bar_nick_utils)
import CODEX_RNA_seq.logging_functions

importlib.reload(CODEX_RNA_seq.logging_functions)

importlib.reload(CODEX_RNA_seq.logging_functions)

from bar_nick_utils import (
    compute_pairwise_kl,
    compute_pairwise_kl_two_items,
    get_latest_file,
    get_umap_filtered_fucntion,
    select_gene_likelihood,
)

if not hasattr(sc.tl.umap, "_is_wrapped"):
    sc.tl.umap = get_umap_filtered_fucntion()
    sc.tl.umap._is_wrapped = True
np.random.seed(42)
torch.manual_seed(42)
pd.set_option("display.max_columns", 10)
pd.set_option("display.max_rows", 10)
warnings.filterwarnings("ignore")
pd.options.display.max_rows = 10
pd.options.display.max_columns = 10
np.set_printoptions(threshold=100)

np.random.seed(0)

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
if os.path.exists(config_path):
    with open(config_path, "r") as f:
        config = json.load(f)
    num_rna_cells = config["subsample"]["num_rna_cells"]
    num_protein_cells = config["subsample"]["num_protein_cells"]
    plot_flag = config["plot_flag"]
else:
    num_rna_cells = num_protein_cells = 2000
    plot_flag = True
# Subsample to 20% of the cells for testing
# For reproducibility


# %%
# Define the DualVAETrainingPlan class
class DualVAETrainingPlan(TrainingPlan):
    def __init__(self, rna_module, **kwargs):
        protein_vae = kwargs.pop("protein_vae")
        rna_vae = kwargs.pop("rna_vae")
        self.plot_x_times = kwargs.pop("plot_x_times", 5)
        contrastive_weight = kwargs.pop("contrastive_weight", 1.0)
        self.batch_size = kwargs.pop("batch_size", 1000)
        self.max_epochs = kwargs.pop("max_epochs", 1)
        self.similarity_weight = kwargs.pop("similarity_weight")
        self.cell_type_clustering_weight = kwargs.pop("cell_type_clustering_weight", 1000.0)
        self.cross_modal_cell_type_weight = kwargs.pop("cross_modal_cell_type_weight", 1000.0)
        self.variance_similarity_weight = kwargs.pop("variance_similarity_weight", 0.5)
        self.lr = kwargs.pop("lr", 0.001)
        self.kl_weight_rna = kwargs.pop("kl_weight_rna", 1.0)
        self.kl_weight_prot = kwargs.pop("kl_weight_prot", 1.0)
        self.matching_weight = kwargs.pop("matching_weight", 1000.0)
        train_size = kwargs.pop("train_size", 0.9)
        self.check_val_every_n_epoch = kwargs["check_val_every_n_epoch"]

        self.validation_step_ = 0
        self.train_step_ = 0  # Initialize train_step_ counter
        validation_size = kwargs.pop("validation_size", 0.1)
        device = kwargs.pop("device", "cuda:0" if torch.cuda.is_available() else "cpu")
        # Verify train and validation sizes sum to 1
        self.metrics_history = []
        self.gradient_clip_val = kwargs.pop("gradient_clip_val", 0.8)

        if abs(train_size + validation_size - 1.0) > 1e-6:
            raise ValueError("train_size + validation_size must sum to 1.0")

        super().__init__(rna_module, **kwargs)
        self.rna_vae = rna_vae
        self.protein_vae = protein_vae

        # Create train/validation splits
        n_rna = len(self.rna_vae.adata)
        n_prot = len(self.protein_vae.adata)

        # Create indices for RNA data
        rna_indices = np.arange(n_rna)
        np.random.shuffle(rna_indices)
        n_train_rna = int(n_rna * train_size)
        self.train_indices_rna = rna_indices[:n_train_rna]
        self.val_indices_rna = rna_indices[n_train_rna:]

        # Create indices for protein data
        prot_indices = np.arange(n_prot)
        np.random.shuffle(prot_indices)
        n_train_prot = int(n_prot * train_size)
        self.train_indices_prot = prot_indices[:n_train_prot]
        self.val_indices_prot = prot_indices[n_train_prot:]

        num_batches = 2
        latent_dim = self.rna_vae.module.n_latent
        self.batch_classifier = torch.nn.Linear(latent_dim, num_batches)
        self.contrastive_weight = contrastive_weight
        self.protein_vae.module.to(device)
        self.rna_vae.module = self.rna_vae.module.to(device)
        self.first_step = True

        n_samples = len(self.train_indices_rna)  # Use training set size
        steps_per_epoch = int(np.ceil(n_samples / self.batch_size))
        self.steps_per_epoch = steps_per_epoch  # Store steps_per_epoch for later use
        self.total_steps = steps_per_epoch * (self.max_epochs)
        self.similarity_loss_history = []
        self.steady_state_window = 5
        self.steady_state_tolerance = 0.5
        self.similarity_active = True
        self.reactivation_threshold = 0.1

        # Add parameters for improved similarity loss activation mechanism
        self.similarity_loss_steady_counter = 0  # Counter for steps in steady state
        self.similarity_loss_steady_threshold = (
            10  # Deactivate after this many steps in steady state
        )
        self.similarity_weight = self.similarity_weight  # Store original weight

        self.active_similarity_loss_active_history = []
        self.train_losses = []
        self.val_losses = []
        self.mode = "training"
        self.similarity_losses = []  # Store similarity losses
        self.similarity_losses_raw = []  # Store raw similarity losses
        self.similarity_weight_history = []  # Store similarity weights
        self.train_rna_losses = []
        self.train_protein_losses = []
        self.train_matching_losses = []
        self.train_contrastive_losses = []
        self.train_adv_losses = []
        self.train_cell_type_clustering_losses = []  # New list for cell type clustering losses
        self.train_cross_modal_cell_type_losses = []  # New list for cross-modal cell type alignment
        self.val_rna_losses = []
        self.val_protein_losses = []
        self.val_matching_losses = []
        self.val_contrastive_losses = []
        self.val_adv_losses = []
        self.val_cell_type_clustering_losses = []  # New list for cell type clustering losses
        self.val_cross_modal_cell_type_losses = []  # New list for cross-modal cell type alignment
        # Add new validation lists
        self.val_similarity_losses = []
        self.val_similarity_losses_raw = []
        self.val_latent_distances = []
        self.early_stopping_callback = None  # Will be set by trainer

        # iLISI tracking
        self.last_ilisi_score = 0.0  # Initialize last iLISI score
        self.ilisi_check_frequency = max(
            1, int(self.total_steps / 100)
        )  # Check ~20 times during training
        # Setup logging

        # Track if on_train_end_custom has been called
        self.on_train_end_custom_called = False

        # Create run directory for checkpoint saves
        self.run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.checkpoint_dir = Path(f"CODEX_RNA_seq/data/checkpoints/run_{self.run_timestamp}")
        os.makedirs(self.checkpoint_dir, exist_ok=True)

        # Save training parameters
        self.save_training_parameters(kwargs)

    def save_training_parameters(self, kwargs):
        """Save training parameters to a JSON file in the checkpoint directory."""
        # Add the parameters from self that aren't in kwargs
        params = {
            "batch_size": self.batch_size,
            "max_epochs": self.total_steps
            // int(np.ceil(len(self.rna_vae.adata) / self.batch_size)),
            "similarity_weight": self.similarity_weight,
            "cell_type_clustering_weight": self.cell_type_clustering_weight,
            "cross_modal_cell_type_weight": self.cross_modal_cell_type_weight,
            "lr": self.lr,
            "kl_weight_rna": self.kl_weight_rna,
            "kl_weight_prot": self.kl_weight_prot,
            "contrastive_weight": self.contrastive_weight,
            "plot_x_times": self.plot_x_times,
            "steady_state_window": self.steady_state_window,
            "steady_state_tolerance": self.steady_state_tolerance,
            "reactivation_threshold": self.reactivation_threshold,
            "rna_adata_shape": list(self.rna_vae.adata.shape),
            "protein_adata_shape": list(self.protein_vae.adata.shape),
            "latent_dim": self.rna_vae.module.n_latent,
            "device": str(self.device),
            "timestamp": self.run_timestamp,
        }

        # Remove non-serializable objects
        params_to_save = {
            k: v for k, v in params.items() if isinstance(v, (str, int, float, bool, list, dict))
        }

        # Save parameters to JSON file
        with open(f"{self.checkpoint_dir}/training_parameters.json", "w") as f:
            json.dump(params_to_save, f, indent=4)

        print(f"✓ Training parameters saved to {self.checkpoint_dir}/training_parameters.json")

        # Also save parameters to a separate txt file in readable format instead of appending to the log file
        with open(f"{self.checkpoint_dir}/training_parameters.txt", "w") as f:
            f.write("Training Parameters:\n")
            for key, value in params_to_save.items():
                f.write(f"{key}: {value}\n")

    def configure_optimizers(self):
        """Configure optimizers for the model.

        Returns:
            dict: Dictionary with optimizer and lr scheduler.
        """
        # Use separate learning rates for RNA (higher) and protein (standard)
        rna_params = self.rna_vae.module.parameters()
        protein_params = self.protein_vae.module.parameters()

        # Use a higher learning rate for RNA to help unstick it
        rna_lr = (
            self.lr * 10.0
        )  # can even be higher 12 or 15, but 10 is good #todo change back to 10
        protein_lr = self.lr

        # Combined parameters with different parameter groups
        all_params = [
            {"params": rna_params, "lr": rna_lr},
            {"params": protein_params, "lr": protein_lr},
        ]

        optimizer = torch.optim.AdamW(
            list(all_params),
            lr=self.lr,
            weight_decay=1e-6,
        )

        d = {
            "optimizer": optimizer,
            "gradient_clip_val": self.gradient_clip_val,
            "gradient_clip_algorithm": "value",
        }

        return d

    def training_step(self, batch, batch_idx):
        self.mode = "training"
        self.rna_vae.module.train()
        self.protein_vae.module.train()
        indices = range(self.batch_size)

        # Increment train_step_ counter
        self.train_step_ += 1

        indices_rna = np.random.choice(
            self.train_indices_rna,
            size=len(indices),
            replace=True if len(indices) > len(self.train_indices_rna) else False,
        )
        indices_prot = np.random.choice(
            self.train_indices_prot,
            size=len(indices),
            replace=True if len(indices) > len(self.train_indices_prot) else False,
        )

        # Debug print - model identity verification

        if not self.rna_vae.module.training:
            print("WARNING: RNA VAE was in eval mode during training step! Setting to train mode.")
            self.rna_vae.module.train()

        if not self.protein_vae.module.training:
            print(
                "WARNING: Protein VAE was in eval mode during training step! Setting to train mode."
            )
            self.protein_vae.module.train()

        rna_batch = self._get_rna_batch(batch, indices_rna)
        protein_batch = self._get_protein_batch(batch, indices_prot)

        # Determine if we should check iLISI this step
        check_ilisi = self.global_step % self.ilisi_check_frequency == 0
        to_plot = self.global_step % (1 + int(self.total_steps / self.plot_x_times)) == 0
        # Calculate all losses using the new function
        losses = calculate_losses(
            self,
            rna_batch=rna_batch,
            protein_batch=protein_batch,
            rna_vae=self.rna_vae,
            protein_vae=self.protein_vae,
            device=self.device,
            similarity_weight=self.similarity_weight,
            similarity_active=self.similarity_active,
            contrastive_weight=self.contrastive_weight,
            matching_weight=self.matching_weight,
            cell_type_clustering_weight=self.cell_type_clustering_weight,
            cross_modal_cell_type_weight=self.cross_modal_cell_type_weight,
            kl_weight_rna=self.kl_weight_rna,
            kl_weight_prot=self.kl_weight_prot,
            global_step=self.global_step,
            total_steps=self.total_steps,
            to_plot=to_plot,
            check_ilisi=check_ilisi,
        )

        # Update last_ilisi_score if we calculated a new one
        if "ilisi_score" in losses:
            self.last_ilisi_score = losses["ilisi_score"]
            ilisi_threshold = 1.5  # use tp be 1.7
            if self.last_ilisi_score < ilisi_threshold:
                # If iLISI is too low, increase the similarity weight
                self.similarity_weight = min(1e7, self.similarity_weight * 2)
                print(
                    f"[Step {self.global_step}] iLISI score is {self.last_ilisi_score:.4f} (< {ilisi_threshold}), increasing similarity weight to {self.similarity_weight}"
                )
                # Also ensure similarity loss is active
                self.similarity_active = True
                self.similarity_loss_steady_counter = 0  # Reset steady state counter
            elif self.similarity_weight > 10 and self.last_ilisi_score >= ilisi_threshold:
                # If iLISI is good and weight is high, reduce it gradually
                self.similarity_weight = self.similarity_weight / 2
                print(
                    f"[Step {self.global_step}] iLISI score is {self.last_ilisi_score:.4f} (>= {ilisi_threshold}), reducing similarity weight to {self.similarity_weight}"
                )
        else:
            # Always include the last iLISI score in the losses
            losses["ilisi_score"] = self.last_ilisi_score

        # Update similarity loss history
        if len(self.similarity_loss_history) >= self.steady_state_window:
            self.similarity_loss_history.pop(0)
        self.similarity_loss_history.append(losses["similarity_loss_raw"])

        # Check if we're in steady state and update counter/status
        in_steady_state = False
        if len(self.similarity_loss_history) == self.steady_state_window:
            # Calculate mean and standard deviation over the window
            mean_loss = sum(self.similarity_loss_history) / self.steady_state_window
            std_loss = (
                sum((x - mean_loss) ** 2 for x in self.similarity_loss_history)
                / self.steady_state_window
            ) ** 0.5

            # Check if variation is small enough to be considered steady state
            coeff_of_variation = std_loss / mean_loss if mean_loss > 0 else float("inf")
            in_steady_state = coeff_of_variation < self.steady_state_tolerance

        # Update steady state counter and status
        if in_steady_state and self.similarity_active:
            self.similarity_loss_steady_counter += 1
            if self.similarity_loss_steady_counter >= self.similarity_loss_steady_threshold:
                self.similarity_active = False
                print(
                    f"[Step {self.global_step}] DEACTIVATING similarity loss - In steady state for {self.similarity_loss_steady_counter} steps"
                )
        elif not in_steady_state and self.similarity_active:
            self.similarity_loss_steady_counter = 0  # Reset counter if not in steady state

        # Check for loss increase if similarity is currently inactive
        if not self.similarity_active and len(self.similarity_loss_history) > 0:
            recent_loss = losses["similarity_loss_raw"].item()
            min_steady_loss = min(self.similarity_loss_history)

            if recent_loss > min_steady_loss * (1 + self.reactivation_threshold):
                # Loss has increased significantly, reactivate similarity loss
                self.similarity_active = True
                self.similarity_loss_steady_counter = 0
                print(
                    f"[Step {self.global_step}] REACTIVATING similarity loss - Loss increased from {min_steady_loss:.4f} to {recent_loss:.4f}"
                )

        # Store losses in history
        self.similarity_losses.append(losses["similarity_loss"].item())
        self.similarity_losses_raw.append(losses["similarity_loss_raw"].item())
        self.similarity_weight_history.append(self.similarity_weight)
        self.active_similarity_loss_active_history.append(self.similarity_active)
        self.train_losses.append(losses["total_loss"].item())
        self.train_rna_losses.append(losses["rna_loss"].item())
        self.train_protein_losses.append(losses["protein_loss"].item())
        self.train_matching_losses.append(losses["matching_loss"].item())
        self.train_contrastive_losses.append(losses["contrastive_loss"].item())
        self.train_adv_losses.append(losses["adv_loss"].item() if "adv_loss" in losses else 0.0)
        self.train_cell_type_clustering_losses.append(losses["cell_type_clustering_loss"].item())
        self.train_cross_modal_cell_type_losses.append(losses["cross_modal_cell_type_loss"].item())
        to_plot = self.global_step % (1 + int(self.total_steps / self.plot_x_times)) == 0
        # Log metrics
        print(f"train step to_plot before: {to_plot}")
        if to_plot:
            plot_similarity_loss_history(
                self.similarity_losses, self.active_similarity_loss_active_history, self.global_step
            )

        # Always save on first and last steps
        if self.global_step == 0 or self.global_step == self.total_steps - 1:
            to_plot = True
        print(f"global_step: {self.global_step}")

        # Always save on last step of epoch - use train_step_ to detect epoch boundary
        steps_in_epoch = int(np.ceil(len(self.train_indices_rna) / self.batch_size))
        is_last_step_of_epoch = self.train_step_ >= steps_in_epoch

        if is_last_step_of_epoch:
            to_plot = True
            # Reset train_step_ counter for next epoch
            self.train_step_ = 0

        print(f"train step to_plot end: {to_plot}")
        log_step(
            losses,
            metrics=None,
            global_step=self.global_step,
            current_epoch=self.current_epoch,
            is_validation=False,
            total_steps=self.total_steps,
            print_to_console=to_plot,
        )

        # Check if this is the last step of the epoch
        is_last_step_of_training = (
            self.current_epoch + 1 >= self.max_epochs
        ) and is_last_step_of_epoch

        if is_last_step_of_epoch:
            print(
                f"Completed last step of epoch {self.current_epoch} in global step {self.global_step}/{self.total_steps}"
            )
            # self.on_epoch_end_custom()
        else:
            print(f"global step {self.global_step}/{self.total_steps}")
        if is_last_step_of_training:
            print(f"Completed last step of training at epoch {self.current_epoch}")
            # Call the custom end of training function if it hasn't been called yet
            if not self.on_train_end_custom_called:
                self.on_train_end_custom(plot_flag=True)
                self.on_train_end_custom_called = True

        return losses["total_loss"]

    def validation_step(self, batch, batch_idx):
        self.mode = "validation"
        """Validation step using the same loss calculations as training.

        Args:
            batch: The batch of data
            batch_idx: The index of the batch

        Returns:
            The total validation loss
        """
        # Get validation batches
        self.rna_vae.module.eval()  # Keep models in eval mode for validation
        self.protein_vae.module.eval()
        self.rna_vae.module.train()
        self.protein_vae.module.train()
        # Remove the lines that set models back to train mode
        indices = range(self.batch_size)
        self.validation_step_ += 1
        indices_prot = np.random.choice(
            self.val_indices_prot,
            size=len(indices),
            replace=True if len(indices) > len(self.val_indices_prot) else False,
        )
        indices_rna = np.random.choice(
            self.val_indices_rna,
            size=len(indices),
            replace=True if len(indices) > len(self.val_indices_rna) else False,
        )
        rna_batch = self._get_rna_batch(batch, indices_rna)
        protein_batch = self._get_protein_batch(batch, indices_prot)

        # Check if we should calculate iLISI for this validation step
        # Calculate iLISI every 5 validation steps or at the start and end of validation
        val_steps_per_epoch = int(np.ceil(len(self.val_indices_rna) / self.batch_size))
        check_ilisi = (
            self.validation_step_ % 5 == 0
            or self.validation_step_ == 1  # First step
            or self.validation_step_ >= val_steps_per_epoch  # Last step
        )
        to_plot = self.global_step % (1 + int(self.total_steps / self.plot_x_times)) == 0
        # Calculate all losses using the same function as training
        with torch.no_grad():
            losses = calculate_losses(
                self,
                rna_batch=rna_batch,
                protein_batch=protein_batch,
                rna_vae=self.rna_vae,
                protein_vae=self.protein_vae,
                device=self.device,
                similarity_weight=self.similarity_weight,
                similarity_active=self.similarity_active,
                contrastive_weight=self.contrastive_weight,
                matching_weight=self.matching_weight,
                cell_type_clustering_weight=self.cell_type_clustering_weight,
                cross_modal_cell_type_weight=self.cross_modal_cell_type_weight,
                kl_weight_rna=self.kl_weight_rna,
                kl_weight_prot=self.kl_weight_prot,
                check_ilisi=check_ilisi,
                to_plot=to_plot,
                global_step=self.global_step,
                total_steps=self.total_steps,
            )

        # We'll accumulate losses in self.current_val_losses rather than self.val_losses directly
        # This accumulation will be handled in on_validation_epoch_end
        if not hasattr(self, "current_val_losses"):
            # Initialize dictionary
            self.current_val_losses = {}

        # Process each loss value
        for k, v in losses.items():
            value = v.item() if hasattr(v, "item") else float(v)

            # Add to current_val_losses
            if k not in self.current_val_losses:
                self.current_val_losses[k] = [value]
            else:
                self.current_val_losses[k].append(value)

        # Debug log periodically
        if self.validation_step_ % 20 == 0:
            print(
                f"Validation step {self.validation_step_}, accumulated {len(self.current_val_losses.get('total_loss', []))} validation samples"
            )
            if "ilisi_score" in losses:
                print(f"Validation iLISI score: {losses['ilisi_score']:.4f}")

        # Log metrics
        metrics = {}

        # Use validation_step_ to determine the last batch
        # Calculate total validation steps needed
        val_steps_per_epoch = int(np.ceil(len(self.val_indices_rna) / self.batch_size))
        is_last_batch = self.validation_step_ >= val_steps_per_epoch

        print(
            f"validation_step_: {self.validation_step_}, val_steps_per_epoch: {val_steps_per_epoch}"
        )
        print(f"is_last_batch: {is_last_batch}")

        if is_last_batch:
            to_plot = True
            # Reset validation_step_ counter for the next validation phase
            self.validation_step_ = 0

            # Just log a summary of validation at the end of validation
            mean_total_loss = sum(self.current_val_losses.get("total_loss", [0])) / max(
                1, len(self.current_val_losses.get("total_loss", []))
            )
            print(f"\nVALIDATION Step {self.global_step}, Epoch {self.current_epoch}")
            print(f"Validation total loss: {mean_total_loss:.4f}")

            # Call on_validation_epoch_end_custom at the end of the last validation batch
            print(f"Last validation batch reached. Calling on_validation_epoch_end_custom...")
            self.on_validation_epoch_end_custom()

        log_step(
            losses,
            metrics=metrics,
            global_step=self.global_step,
            current_epoch=self.current_epoch,
            is_validation=True,
            total_steps=self.total_steps,
            print_to_console=to_plot,
        )

        return losses["total_loss"]

    def calculate_metrics_for_data(
        self, rna_latent_adata, prot_latent_adata, prefix="", global_step=None
    ):
        """Calculate metrics for given RNA and protein data.

        Args:
            rna_latent_adata: RNA AnnData object
            prot_latent_adata: Protein AnnData object
            prefix: Prefix for metric names (e.g., "train_" or "val_")
            subsample_size: If not None, subsample the data to this size
        """
        print(f"   Calculating {prefix}metrics...")

        # Calculate matching accuracy
        accuracy = CODEX_RNA_seq.metrics.matching_accuracy(
            rna_latent_adata, prot_latent_adata, global_step
        )
        print(f"   ✓ {prefix}matching accuracy calculated")

        # Calculate silhouette F1
        silhouette_f1 = CODEX_RNA_seq.metrics.compute_silhouette_f1(
            rna_latent_adata, prot_latent_adata
        )
        print(f"   ✓ {prefix}silhouette F1 calculated")

        # Calculate ARI F1
        combined_latent = anndata.concat(
            [rna_latent_adata, prot_latent_adata],
            join="outer",
            label="modality",
            keys=["RNA", "Protein"],
        )

        # Force recomputation of neighbors with cosine metric for better integration
        # This helps with modality alignment in UMAP visualization
        combined_latent.obsm.pop("X_pca", None) if "X_pca" in combined_latent.obsm else None
        combined_latent.obsp.pop(
            "connectivities", None
        ) if "connectivities" in combined_latent.obsp else None
        combined_latent.obsp.pop("distances", None) if "distances" in combined_latent.obsp else None
        combined_latent.uns.pop("neighbors", None) if "neighbors" in combined_latent.uns else None

        # Calculate with parameters optimized for integration
        sc.pp.neighbors(combined_latent, use_rep="X")

        # pf.plot_end_of_val_epoch_pca_umap_latent_space(
        #     prefix, combined_latent, epoch=self.current_epoch
        # )

        ari_f1 = CODEX_RNA_seq.metrics.compute_ari_f1(combined_latent)
        print(f"   ✓ {prefix}ARI F1 calculated")

        return {
            f"{prefix}cell_type_matching_accuracy": accuracy,
            f"{prefix}silhouette_f1_score": silhouette_f1.mean(),
            f"{prefix}ari_f1_score": ari_f1,
        }

    def on_validation_epoch_end_custom(self):
        """Calculate and store metrics at the end of each validation epoch."""
        print(f"\nProcessing validation epoch {self.current_epoch}...")
        self.rna_vae.module.eval()
        self.protein_vae.module.eval()
        self.rna_vae.module.train()
        self.protein_vae.module.train()

        subsample_size = min(len(self.val_indices_rna), len(self.val_indices_prot), 3000)
        val_indices_rna = np.random.choice(self.val_indices_rna, size=subsample_size, replace=False)
        val_indices_prot = np.random.choice(
            self.val_indices_prot, size=subsample_size, replace=False
        )
        rna_batch = self._get_rna_batch(None, val_indices_rna)
        prot_batch = self._get_protein_batch(None, val_indices_prot)

        # Get latent representations
        with torch.no_grad():
            rna_inference_outputs, _, _ = self.rna_vae.module(rna_batch)
            rna_latent = rna_inference_outputs["qz"].mean.detach().cpu().numpy()
            prot_inference_outputs, _, _ = self.protein_vae.module(prot_batch)
            prot_latent = prot_inference_outputs["qz"].mean.detach().cpu().numpy()

        # Create AnnData objects with ONLY the observations for the selected indices
        rna_latent_adata = AnnData(rna_latent)
        prot_latent_adata = AnnData(prot_latent)

        # Use only the observations corresponding to the selected indices
        rna_latent_adata.obs = self.rna_vae.adata[val_indices_rna].obs.copy()
        prot_latent_adata.obs = self.protein_vae.adata[val_indices_prot].obs.copy()

        # Calculate validation metrics
        val_metrics = self.calculate_metrics_for_data(
            rna_latent_adata,
            prot_latent_adata,
            prefix="val_",
            global_step=self.global_step,
        )
        # now for train
        subsample_size = min(len(self.train_indices_rna), len(self.train_indices_prot), 3000)
        train_indices_rna = np.random.choice(
            self.train_indices_rna, size=subsample_size, replace=False
        )
        train_indices_prot = np.random.choice(
            self.train_indices_prot, size=subsample_size, replace=False
        )
        rna_batch = self._get_rna_batch(None, train_indices_rna)
        prot_batch = self._get_protein_batch(None, train_indices_prot)

        # Calculate training metrics with subsampling
        print("Calculating training metrics...")
        train_metrics = self.calculate_metrics_for_data(
            rna_latent_adata,
            prot_latent_adata,
            prefix="train_",
            global_step=self.global_step,
        )

        # Combine metrics
        epoch_metrics = {**val_metrics, **train_metrics}

        # Store in history
        self.metrics_history.append(epoch_metrics)
        print("   ✓ Metrics: ", epoch_metrics)

        # Now process accumulated validation losses from validation_step
        # These are the epoch-level means of the batch-level losses
        if hasattr(self, "current_val_losses") and self.current_val_losses:
            validation_sample_count = len(self.current_val_losses.get("total_loss", []))
            print(f"   ✓ Processing {validation_sample_count} validation samples")

            # Calculate mean for each loss type
            epoch_val_losses = {}
            for loss_type, values in self.current_val_losses.items():
                if values:
                    mean_value = sum(values) / len(values)
                    epoch_val_losses[loss_type] = mean_value

                    # Append to the appropriate list
                    if loss_type == "total_loss":
                        self.val_losses.append(mean_value)
                    elif loss_type == "rna_loss":
                        self.val_rna_losses.append(mean_value)
                    elif loss_type == "protein_loss":
                        self.val_protein_losses.append(mean_value)
                    elif loss_type == "matching_loss":
                        self.val_matching_losses.append(mean_value)
                    elif loss_type == "contrastive_loss":
                        self.val_contrastive_losses.append(mean_value)
                    elif loss_type == "cell_type_clustering_loss":
                        self.val_cell_type_clustering_losses.append(mean_value)
                    elif loss_type == "similarity_loss":
                        self.val_similarity_losses.append(mean_value)
                    elif loss_type == "similarity_loss_raw":
                        self.val_similarity_losses_raw.append(mean_value)
                    elif loss_type == "latent_distances":
                        self.val_latent_distances.append(mean_value)
                    elif loss_type == "ilisi_score":
                        # Store validation iLISI scores
                        if not hasattr(self, "val_ilisi_scores"):
                            self.val_ilisi_scores = []
                        self.val_ilisi_scores.append(mean_value)
                        # Also log to MLflow
                        mlflow.log_metric("val_ilisi_score", mean_value, step=self.current_epoch)
                    elif loss_type == "cross_modal_cell_type_loss":
                        self.val_cross_modal_cell_type_losses.append(mean_value)

            # Reset current_val_losses for next epoch
            self.current_val_losses = {}

            print(f"   ✓ Validation losses stored (epoch {self.current_epoch})")
            print(f"   ✓ Current validation history length: {len(self.val_losses)}")
        else:
            print("WARNING: No validation losses to process for this epoch!")

        # Debug print validation losses after processing
        print(f"DEBUG: After processing - Validation loss array lengths:")
        print(f"  val_losses: {len(self.val_losses)}")
        print(f"  val_rna_losses: {len(self.val_rna_losses)}")
        print(f"  val_similarity_losses: {len(self.val_similarity_losses)}")

        # Reset validation step counter
        self.validation_step_ = 0

        # Log metrics to MLflow
        mlflow.log_metrics(epoch_metrics, step=self.current_epoch)

        print(f"✓ Validation epoch {self.current_epoch} completed successfully!")

    # Add the standard PyTorch Lightning hook that calls our custom method
    def on_validation_epoch_end(self):
        """Standard PyTorch Lightning hook for validation epoch end.
        This will be called automatically by the PyTorch Lightning trainer.
        """
        print(f"PyTorch Lightning on_validation_epoch_end hook called (epoch {self.current_epoch})")
        self.on_validation_epoch_end_custom()

    def on_train_end_custom(self, plot_flag=True):
        """Called when training ends."""
        self.rna_vae.module.eval()
        self.protein_vae.module.eval()
        self.rna_vae.module.train()
        self.protein_vae.module.train()
        self.mode = "validation"
        if self.on_train_end_custom_called:
            print("on_train_end_custom already called, skipping")
            return

        print("\nTraining completed!")

        # Get final latent representations
        with torch.no_grad():
            # Process RNA data in batches to avoid memory overflow
            rna_latent_list = []
            # Adjust batch size if needed
            for i in range(0, self.rna_vae.adata.shape[0], self.batch_size):
                batch_indices = np.arange(i, min(i + self.batch_size, self.rna_vae.adata.shape[0]))
                rna_batch = self._get_rna_batch(None, batch_indices)
                rna_inference_outputs, _, _ = self.rna_vae.module(rna_batch)
                rna_latent_list.append(rna_inference_outputs["qz"].mean.detach().cpu().numpy())
            rna_latent = np.concatenate(rna_latent_list, axis=0)

            # Process protein data in batches to avoid memory overflow
            prot_latent_list = []
            for i in range(0, self.protein_vae.adata.shape[0], self.batch_size):
                batch_indices = np.arange(
                    i, min(i + self.batch_size, self.protein_vae.adata.shape[0])
                )
                protein_batch = self._get_protein_batch(None, batch_indices)
                prot_inference_outputs, _, _ = self.protein_vae.module(protein_batch)
                prot_latent_list.append(prot_inference_outputs["qz"].mean.detach().cpu().numpy())
            prot_latent = np.concatenate(prot_latent_list, axis=0)

            # take a random batch
            rna_batch = self._get_rna_batch(
                None,
                np.random.choice(
                    range(self.rna_vae.adata.shape[0]), size=self.batch_size, replace=False
                ),
            )
            protein_batch = self._get_protein_batch(
                None,
                np.random.choice(
                    range(self.protein_vae.adata.shape[0]), size=self.batch_size, replace=False
                ),
            )
        with torch.no_grad():
            losses = calculate_losses(  # for the plots
                self,
                rna_batch=rna_batch,
                protein_batch=protein_batch,
                rna_vae=self.rna_vae,
                protein_vae=self.protein_vae,
                device=self.device,
                similarity_weight=self.similarity_weight,
                similarity_active=self.similarity_active,
                contrastive_weight=self.contrastive_weight,
                matching_weight=self.matching_weight,
                cell_type_clustering_weight=self.cell_type_clustering_weight,
                cross_modal_cell_type_weight=self.cross_modal_cell_type_weight,
                kl_weight_rna=self.kl_weight_rna,
                kl_weight_prot=self.kl_weight_prot,
                check_ilisi=True,
                to_plot=True,
                global_step=self.global_step,
                total_steps=self.total_steps,
            )
        print(f"end of training, losses: {losses}")
        # Store in adata
        self.rna_vae.adata.obsm["X_scVI"] = rna_latent
        self.protein_vae.adata.obsm["X_scVI"] = prot_latent

        # Save final checkpoint with 'final' tag
        final_checkpoint_path = f"{self.checkpoint_dir}/final"
        os.makedirs(final_checkpoint_path, exist_ok=True)

        # Save the final checkpoint
        self.save_checkpoint()

        # Plot metrics over time
        if plot_flag and hasattr(self, "metrics_history"):
            pf.plot_training_metrics_history(
                self.metrics_history,
            )
        history = self.get_history()

        # Save the training history to a JSON file for reference
        history_path = f"{self.checkpoint_dir}/training_history.json"
        try:
            # Convert numpy arrays to lists for JSON serialization
            json_history = {}
            for key, value in history.items():
                if isinstance(value, np.ndarray):
                    json_history[key] = value.tolist()
                elif isinstance(value, list) and value and isinstance(value[0], np.ndarray):
                    json_history[key] = [v.tolist() for v in value]
                else:
                    json_history[key] = value

            with open(history_path, "w") as f:
                json.dump(json_history, f, indent=2)
            print(f"✓ Training history saved to {history_path}")

            # Log history JSON as artifact
            mlflow.log_artifact(history_path)
        except Exception as e:
            print(f"Warning: Failed to save training history: {str(e)}")

        # Plot the losses
        pf.plot_train_val_normalized_losses(history)
        print("   ✓ Train/validation normalized losses plotted")

        # Find best metrics
        if hasattr(self, "metrics_history") and len(self.metrics_history) > 0:
            best_metrics = {}
            for metric in self.metrics_history[0].keys():
                values = [epoch[metric] for epoch in self.metrics_history]
                if "loss" in metric or "error" in metric:
                    best_metrics[metric] = min(values)
                else:
                    best_metrics[metric] = max(values)

            # Log best metrics to MLflow
            mlflow.log_metrics({f"best_{k}": v for k, v in best_metrics.items()})
            print("✓ Best metrics logged to MLflow")

    def save_checkpoint(self):
        """Save the model checkpoint including AnnData objects with latent representations."""
        print(f"\nSaving checkpoint at epoch {self.current_epoch}...")

        # Create checkpoint directory if it doesn't exist
        checkpoint_path = f"{self.checkpoint_dir}/epoch_{self.current_epoch}"
        os.makedirs(checkpoint_path, exist_ok=True)

        rna_adata_save = self.rna_vae.adata.copy()
        protein_adata_save = self.protein_vae.adata.copy()

        # Clean for h5ad saving
        clean_uns_for_h5ad(rna_adata_save)
        clean_uns_for_h5ad(protein_adata_save)

        # Save AnnData objects with latent representations
        sc.write(f"{checkpoint_path}/rna_adata.h5ad", rna_adata_save)
        sc.write(f"{checkpoint_path}/protein_adata.h5ad", protein_adata_save)

        # Save model state dictionaries
        torch.save(self.rna_vae.module.state_dict(), f"{checkpoint_path}/rna_vae_model.pt")
        torch.save(self.protein_vae.module.state_dict(), f"{checkpoint_path}/protein_vae_model.pt")

        # Save training plan state
        training_state = {
            "epoch": self.current_epoch,
            "global_step": self.global_step,
            "optimizer_state": self.configure_optimizers()["optimizer"].state_dict(),
            "similarity_weight": self.similarity_weight,
            "similarity_active": self.similarity_active,
            "similarity_loss_history": self.similarity_loss_history,
            "similarity_loss_steady_counter": self.similarity_loss_steady_counter,
            "last_ilisi_score": self.last_ilisi_score,
        }

        torch.save(training_state, f"{checkpoint_path}/training_state.pt")

        # Save training parameters and configs
        params = {
            "batch_size": self.batch_size,
            "max_epochs": self.max_epochs,
            "similarity_weight": self.similarity_weight,
            "cell_type_clustering_weight": self.cell_type_clustering_weight,
            "cross_modal_cell_type_weight": self.cross_modal_cell_type_weight,
            "contrastive_weight": self.contrastive_weight,
            "matching_weight": self.matching_weight,
            "kl_weight_rna": self.kl_weight_rna,
            "kl_weight_prot": self.kl_weight_prot,
            "lr": self.lr,
            "latent_dim": self.rna_vae.module.n_latent,
            "device": str(self.device),
        }

        with open(f"{checkpoint_path}/model_config.json", "w") as f:
            json.dump(params, f, indent=4)

        print(f"✓ Checkpoint saved at {checkpoint_path}")

        # Log path as MLflow artifact if MLflow is active
        try:
            mlflow.log_artifact(checkpoint_path)
            print("✓ Checkpoint logged to MLflow")
        except:
            print("Note: MLflow logging skipped")

    def _get_protein_batch(self, batch, indices):
        protein_data = self.protein_vae.adata[indices]
        X = protein_data.X
        if issparse(X):
            X = X.toarray()
        X = torch.tensor(X, dtype=torch.float32).to(self.device)
        batch_indices = torch.tensor(protein_data.obs["_scvi_batch"].values, dtype=torch.long).to(
            self.device
        )
        archetype_vec = torch.tensor(
            protein_data.obsm["archetype_vec"].values, dtype=torch.float32
        ).to(self.device)

        protein_batch = {
            "X": X,
            "batch": batch_indices,
            "labels": indices,
            "archetype_vec": archetype_vec,
        }
        return protein_batch

    def _get_rna_batch(self, batch, indices):
        rna_data = self.rna_vae.adata[indices]
        X = rna_data.X
        if issparse(X):
            X = X.toarray()
        X = torch.tensor(X, dtype=torch.float32).to(self.device)
        batch_indices = torch.tensor(rna_data.obs["_scvi_batch"].values, dtype=torch.long).to(
            self.device
        )
        archetype_vec = torch.tensor(rna_data.obsm["archetype_vec"].values, dtype=torch.float32).to(
            self.device
        )
        rna_batch = {
            "X": X,
            "batch": batch_indices,
            "labels": indices,
            "archetype_vec": archetype_vec,
        }
        return rna_batch

    def get_history(self):
        """Return the training history including similarity losses with proper epoch alignment"""
        # Calculate steps per epoch
        steps_per_epoch = int(np.ceil(len(self.train_indices_rna) / self.batch_size))
        # Debug print raw loss history arrays
        print(f"DEBUG: Raw loss history arrays")
        print(f"  train_losses: {len(self.train_losses)} items")
        print(f"  val_losses: {len(self.val_losses)} items")

        # Make sure validation losses are properly processed
        if len(self.val_losses) > 0:
            print(f"  Found {len(self.val_losses)} validation loss entries")
        else:
            print("  No validation losses in val_losses array")
            # Process accumulated validation data from current_val_losses if available
            if (
                hasattr(self, "current_val_losses")
                and self.current_val_losses
                and len(self.current_val_losses) > 0
            ):
                print(f"  Processing accumulated validation data from current_val_losses")
                # Call validation epoch end to process the validation data

        # Group losses by epoch to get mean values
        def get_epoch_means(loss_list):
            """Calculate epoch means from a list of loss values"""
            # Skip if no data
            if not loss_list:
                return []

            # Convert to numpy array
            loss_array = np.array(loss_list)
            # Filter out invalid values
            loss_array = loss_array[~np.isinf(loss_array) & ~np.isnan(loss_array)]

            # Return empty list if no valid values
            if len(loss_array) == 0:
                return []

            # Split by epoch and take mean
            num_epochs = max(1, len(loss_array) // steps_per_epoch)
            epoch_means = []

            for i in range(num_epochs):
                start_idx = i * steps_per_epoch
                end_idx = min((i + 1) * steps_per_epoch, len(loss_array))
                if start_idx < end_idx:  # Only compute if we have data
                    epoch_mean = np.mean(loss_array[start_idx:end_idx])
                    epoch_means.append(epoch_mean)

            return epoch_means

        # Calculate validation epochs - default to every 2 epochs
        # Try to get check_val_every_n_epoch from trainer, or use default value of 2
        val_epochs = [i * self.check_val_every_n_epoch for i in range(len(self.val_losses))]

        # If we don't have val_epochs but we have validation losses, create them
        if not val_epochs and len(self.val_losses) > 0:
            val_epochs = list(range(len(self.val_losses)))

        # Create history dictionary with epoch means
        history = {
            "train_similarity_loss": get_epoch_means(self.similarity_losses),
            "train_similarity_loss_raw": get_epoch_means(self.similarity_losses_raw),
            "train_total_loss": get_epoch_means(self.train_losses),
            "train_rna_loss": get_epoch_means(self.train_rna_losses),
            "train_protein_loss": get_epoch_means(self.train_protein_losses),
            "train_matching_loss": get_epoch_means(self.train_matching_losses),
            "train_contrastive_loss": get_epoch_means(self.train_contrastive_losses),
            "train_cell_type_clustering_loss": get_epoch_means(
                self.train_cell_type_clustering_losses
            ),
            "train_cross_modal_cell_type_loss": get_epoch_means(
                self.train_cross_modal_cell_type_losses
            ),
            "val_total_loss": self.val_losses,  # Validation losses are already per-epoch
            "val_rna_loss": self.val_rna_losses,
            "val_protein_loss": self.val_protein_losses,
            "val_matching_loss": self.val_matching_losses,
            "val_contrastive_loss": self.val_contrastive_losses,
            "val_similarity_loss": self.val_similarity_losses,
            "val_similarity_loss_raw": self.val_similarity_losses_raw,
            "val_cell_type_clustering_loss": self.val_cell_type_clustering_losses,
            "val_cross_modal_cell_type_loss": self.val_cross_modal_cell_type_losses,
            # Also include the validation epoch indices
            "val_epochs": val_epochs,
        }

        # Add iLISI scores if available
        if hasattr(self, "val_ilisi_scores"):
            history["val_ilisi_scores"] = self.val_ilisi_scores
        return history

    def on_early_stopping(self):
        """Called when early stopping is triggered."""
        print("\nEarly stopping triggered!")

        print("✓ Early stopping artifacts saved")

    def on_epoch_end(self):
        """Called at the end of each training epoch."""
        # Fix the log_epoch_end call to match the function signature
        log_epoch_end(self.current_epoch, self.train_losses, self.val_losses)
        print(f"\nEnd of epoch {self.current_epoch}")
        print(
            f"Average train loss: {sum(self.train_losses)/len(self.train_losses) if self.train_losses else 0:.4f}"
        )
        print(
            f"Average validation loss: {sum(self.val_losses)/len(self.val_losses) if self.val_losses else 0:.4f}"
        )

        # Save checkpoint every 5 epochs or on the first and last epochs
        if self.current_epoch % 5 == 0 and self.current_epoch > 5:
            start_time = time()
            self.save_checkpoint()
            print(f"Checkpoint saved in {time() - start_time:.2f} seconds")

    @staticmethod
    def load_from_checkpoint(
        checkpoint_path,
        rna_vae,
        protein_vae,
        device="cuda:0" if torch.cuda.is_available() else "cpu",
    ):
        """Load a checkpoint from disk.

        Args:
            checkpoint_path: Path to the checkpoint directory
            device: Device to load the models to

        Returns:
            rna_vae: Loaded RNA VAE model
            protein_vae: Loaded protein VAE model
            training_state: Dictionary with training state
        """
        print(f"Loading checkpoint from {checkpoint_path}...")

        # Load AnnData objects
        # rna_adata = sc.read_h5ad(f"{checkpoint_path}/rna_adata.h5ad")
        # protein_adata = sc.read_h5ad(f"{checkpoint_path}/protein_adata.h5ad")

        # Load configuration
        # with open(f"{checkpoint_path}/model_config.json", "r") as f:
        #     config = json.load(f)

        # # Setup anndata for scVI
        # SCVI.setup_anndata(rna_adata, labels_key="index_col")
        # SCVI.setup_anndata(protein_adata, labels_key="index_col")

        # Create models
        # rna_vae = SCVI(
        #     rna_adata,
        #     gene_likelihood=select_gene_likelihood(rna_adata),
        #     n_hidden=config.get("n_hidden_rna", 128),
        #     n_layers=config.get("n_layers", 3),
        #     n_latent=config.get("latent_dim", 10),
        # )

        # protein_vae = SCVI(
        #     protein_adata,
        #     gene_likelihood="normal",
        #     n_hidden=config.get("n_hidden_prot", 50),
        #     n_layers=config.get("n_layers", 3),
        #     n_latent=config.get("latent_dim", 10),
        # )

        # Load state dictionaries
        rna_vae.module.load_state_dict(
            torch.load(f"{checkpoint_path}/rna_vae_model.pt", map_location=device)
        )
        protein_vae.module.load_state_dict(
            torch.load(f"{checkpoint_path}/protein_vae_model.pt", map_location=device)
        )

        # Load training state
        training_state = torch.load(f"{checkpoint_path}/training_state.pt", map_location=device)

        # Set models as trained
        rna_vae.is_trained_ = False
        protein_vae.is_trained_ = False

        # Move models to device
        rna_vae.module.to(device)
        protein_vae.module.to(device)

        print(f"✓ Models loaded successfully from {checkpoint_path}")

        return rna_vae, protein_vae, training_state


# %%
def train_vae(
    adata_rna_subset,
    adata_prot_subset,
    model_checkpoints_folder=None,
    max_epochs=1,
    batch_size=128,
    lr=1e-3,
    contrastive_weight=1.0,
    similarity_weight=1000.0,
    diversity_weight=0.1,
    matching_weight=100.0,
    cell_type_clustering_weight=1.0,
    cross_modal_cell_type_weight=1.0,
    train_size=0.9,
    check_val_every_n_epoch=1,
    adv_weight=0.1,
    n_hidden_rna=128,
    n_hidden_prot=50,
    n_layers=3,
    latent_dim=10,
    validation_size=0.1,
    gradient_clip_val=1.0,
    accumulate_grad_batches=1,
    kl_weight_rna=1.0,
    kl_weight_prot=1.0,
    device="cuda:0" if torch.cuda.is_available() else "cpu",
    **kwargs,
):
    """Train the VAE models."""
    print("Initializing VAEs...")
    # setup adata for scvi
    if adata_rna_subset.X.min() < 0:
        adata_rna_subset.X = adata_rna_subset.X - adata_rna_subset.X.min()
    if adata_prot_subset.X.min() < 0:
        adata_prot_subset.X = adata_prot_subset.X - adata_prot_subset.X.min()
    adata_rna_subset.obs["index_col"] = range(len(adata_rna_subset.obs.index))
    adata_prot_subset.obs["index_col"] = range(len(adata_prot_subset.obs.index))
    SCVI.setup_anndata(
        adata_rna_subset,
        labels_key="index_col",
    )
    if adata_prot_subset.X.min() < 0:
        adata_prot_subset.X = adata_prot_subset.X - adata_prot_subset.X.min()

    SCVI.setup_anndata(
        adata_prot_subset,
        labels_key="index_col",
    )
    rna_vae = scvi.model.SCVI(
        adata_rna_subset,
        gene_likelihood=select_gene_likelihood(adata_rna_subset),
        n_hidden=n_hidden_rna,
        n_layers=n_layers,
        n_latent=latent_dim,
    )
    protein_vae = scvi.model.SCVI(
        adata_prot_subset,
        gene_likelihood="normal",
        n_hidden=n_hidden_prot,
        n_layers=n_layers,
        n_latent=latent_dim,
    )
    print("VAEs initialized")

    rna_vae._training_plan_cls = DualVAETrainingPlan

    # Set up training parameters

    # Create training plan with both VAEs
    plan_kwargs = {
        "protein_vae": protein_vae,
        "rna_vae": rna_vae,
        "contrastive_weight": contrastive_weight,
        "similarity_weight": similarity_weight,
        "diversity_weight": diversity_weight,
        "cell_type_clustering_weight": cell_type_clustering_weight,
        "cross_modal_cell_type_weight": cross_modal_cell_type_weight,
        "matching_weight": matching_weight,
        "adv_weight": adv_weight,
        "plot_x_times": kwargs.pop("plot_x_times", 5),
        "batch_size": batch_size,
        "max_epochs": max_epochs,
        "lr": lr,
        "kl_weight_rna": kl_weight_rna,
        "kl_weight_prot": kl_weight_prot,
        "gradient_clip_val": gradient_clip_val,
        "check_val_every_n_epoch": check_val_every_n_epoch,
    }
    train_kwargs = {
        "max_epochs": max_epochs,
        "batch_size": batch_size,
        "train_size": train_size,
        "validation_size": validation_size,
        "accumulate_grad_batches": accumulate_grad_batches,
        "check_val_every_n_epoch": check_val_every_n_epoch,
    }
    print("Plan parameters:")
    pprint(plan_kwargs)
    # Create training plan instance
    print("Creating training plan for initial latent computation...")
    training_plan = DualVAETrainingPlan(rna_vae.module, **plan_kwargs)
    rna_vae._training_plan = training_plan
    print("Training plan created")

    # Train the model
    print("Starting training...")
    rna_vae.is_trained_ = True
    protein_vae.is_trained_ = True
    rna_vae.module.cpu()
    protein_vae.module.cpu()

    if model_checkpoints_folder is not None:
        rna_vae, protein_vae, training_state = DualVAETrainingPlan.load_from_checkpoint(
            model_checkpoints_folder, rna_vae, protein_vae
        )
    else:
        rna_vae.module.to(device)
        protein_vae.module.to(device)
        rna_vae.is_trained_ = False
        protein_vae.is_trained_ = False
    rna_vae.train(**train_kwargs, plan_kwargs=plan_kwargs)

    # Manually set trained flag
    rna_vae.is_trained_ = True
    protein_vae.is_trained_ = True
    print("Training flags set")

    return rna_vae, protein_vae


if __name__ == "__main__":
    save_dir = "CODEX_RNA_seq/data/processed_data"
    device = "cuda:0" if torch.cuda.is_available() else "cpu"

    # %%
    # read in the data
    cwd = os.getcwd()
    save_dir = Path("CODEX_RNA_seq/data/processed_data").absolute()

    # Use get_latest_file to find the most recent files
    adata_rna_subset = sc.read_h5ad(
        get_latest_file(save_dir, "adata_rna_subset_prepared_for_training_")
    )
    adata_prot_subset = sc.read_h5ad(
        get_latest_file(save_dir, "adata_prot_subset_prepared_for_training_")
    )

    # Subsample the data for faster testing
    print(f"Original RNA dataset shape: {adata_rna_subset.shape}")
    print(f"Original protein dataset shape: {adata_prot_subset.shape}")
    # Load config if exists
    rna_sample_size = min(len(adata_rna_subset), num_rna_cells)
    prot_sample_size = min(len(adata_prot_subset), num_protein_cells)
    adata_rna_subset = sc.pp.subsample(adata_rna_subset, n_obs=rna_sample_size, copy=True)
    adata_prot_subset = sc.pp.subsample(adata_prot_subset, n_obs=prot_sample_size, copy=True)

    print(f"Subsampled RNA dataset shape: {adata_rna_subset.shape}")
    print(f"Subsampled protein dataset shape: {adata_prot_subset.shape}")

    # Import utility functions
    from CODEX_RNA_seq.training_utils import (
        calculate_post_training_metrics,
        clear_memory,
        generate_visualizations,
        log_memory_usage,
        log_parameters,
        match_cells_and_calculate_distances,
        process_latent_spaces,
        save_results,
        setup_and_train_model,
    )

    # Create log directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)
    log_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = open(f"logs/vae_training_{log_timestamp}.log", "w")

    # Redirect stdout to both console and log file
    original_stdout = sys.stdout
    sys.stdout = Tee(sys.stdout, log_file)

    print(f"Starting VAE training at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Log file: logs/vae_training_{log_timestamp}.log")

    # Setup MLflow
    mlflow.set_tracking_uri("file:./mlruns")
    experiment_name = f"vae_training_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    experiment_id = mlflow.create_experiment(experiment_name)
    mlflow.set_experiment(experiment_name)

    log_memory_usage("Before loading data: ")

    # Load data - already done in the imports/setup section above
    print(f"RNA dataset shape: {adata_rna_subset.shape}")
    print(f"Protein dataset shape: {adata_prot_subset.shape}")

    # Define training parameters
    training_params = {
        "plot_x_times": 3,
        "max_epochs": 1,
        "batch_size": 128,
        "lr": 1e-4,
        "contrastive_weight": 0,
        "similarity_weight": 10.0,
        "diversity_weight": 0.1,
        "matching_weight": 100000.0,
        "cell_type_clustering_weight": 1.0,
        "n_hidden_rna": 64,
        "n_hidden_prot": 32,
        "n_layers": 3,
        "latent_dim": 10,
        "kl_weight_rna": 0.1,
        "kl_weight_prot": 10.0,
        "adv_weight": 0.0,
        "train_size": 0.9,
        "validation_size": 0.1,
        "check_val_every_n_epoch": 2,
        "gradient_clip_val": 1.0,
    }

    # Create loss weights JSON
    loss_weights = {
        "kl_weight_rna": training_params["kl_weight_rna"],
        "kl_weight_prot": training_params["kl_weight_prot"],
        "contrastive_weight": training_params["contrastive_weight"],
        "similarity_weight": training_params["similarity_weight"],
        # "diversity_weight": training_params["diversity_weight"],
        "matching_weight": training_params["matching_weight"],
        "cell_type_clustering_weight": training_params["cell_type_clustering_weight"],
        # "adv_weight": training_params["adv_weight"]
    }

    # Save loss weights to a temporary JSON file
    loss_weights_path = "loss_weights.json"
    with open(loss_weights_path, "w") as f:
        json.dump(loss_weights, f, indent=4)

    # Log parameters
    log_parameters(training_params, 0, 1)

    # Start MLflow run
    run_name = f"vae_training_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_name = f"vae_training_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with mlflow.start_run(run_name=run_name):
        # Log loss weights JSON as artifact at the start
        mlflow.log_artifact(loss_weights_path)
        # Clean up temporary file
        os.remove(loss_weights_path)

        rna_vae, protein_vae, rna_latent_before, latent_prot_before = setup_and_train_model(
            adata_rna_subset, adata_prot_subset, training_params
        )
        clear_memory()

        # Get training history
        history = rna_vae._training_plan.get_history()

        # Log training history metrics
        mlflow.log_metrics(
            {
                key: history[hist_key][-1] if history[hist_key] else float("nan")
                for key, hist_key in {
                    "final_train_similarity_loss": "train_similarity_loss",
                    "final_train_similarity_loss_raw": "train_similarity_loss_raw",
                    "final_train_total_loss": "train_total_loss",
                    "final_val_total_loss": "val_total_loss",
                    "final_train_cell_type_clustering_loss": "train_cell_type_clustering_loss",
                    # Add all validation loss metrics
                    "final_val_rna_loss": "val_rna_loss",
                    "final_val_protein_loss": "val_protein_loss",
                    "final_val_contrastive_loss": "val_contrastive_loss",
                    "final_val_matching_loss": "val_matching_loss",
                    "final_val_similarity_loss": "val_similarity_loss",
                    "final_val_similarity_loss_raw": "val_similarity_loss_raw",
                    "final_val_cell_type_clustering_loss": "val_cell_type_clustering_loss",
                    "final_val_cross_modal_cell_type_loss": "val_cross_modal_cell_type_loss",
                }.items()
            }
        )

        # Process latent spaces
        rna_latent, prot_latent, combined_latent = process_latent_spaces(
            rna_vae.adata, protein_vae.adata
        )

        # Match cells and calculate distances
        matching_results = match_cells_and_calculate_distances(rna_latent, prot_latent)

        # Calculate metrics
        metrics = calculate_post_training_metrics(
            rna_vae, protein_vae, matching_results["prot_matches_in_rna"]
        )

        # Log metrics
        mlflow.log_metrics(metrics)

        # Generate visualizations

        generate_visualizations(
            rna_vae,
            protein_vae,
            rna_latent,
            prot_latent,
            combined_latent,
            history,
            matching_results,
        )

        # Save results
        save_dir = Path("CODEX_RNA_seq/data/trained_data").absolute()
        save_results(rna_vae, protein_vae, save_dir)

    sys.stdout = original_stdout
    log_file.close()

    # Log the log file to MLflow
    mlflow.log_artifact(f"logs/vae_training_{log_timestamp}.log")


def calculate_losses(
    self,
    rna_batch,
    protein_batch,
    rna_vae,
    protein_vae,
    device,
    similarity_weight,
    similarity_active,
    contrastive_weight,
    matching_weight,
    cell_type_clustering_weight,
    cross_modal_cell_type_weight,
    kl_weight_rna,
    kl_weight_prot,
    global_step=None,
    total_steps=None,
    to_plot=False,
    check_ilisi=False,
    return_inference_outputs=False,
):
    """Calculate all losses for a batch of data.

    Args:
        rna_batch: Dictionary containing RNA batch data
        protein_batch: Dictionary containing protein batch data
        rna_vae: RNA VAE model
        protein_vae: Protein VAE model
        device: Device to use for calculations
        similarity_weight: Weight for similarity loss
        similarity_active: Whether similarity loss is active
        contrastive_weight: Weight for contrastive loss
        matching_weight: Weight for matching loss
        cell_type_clustering_weight: Weight for within-modality cell type clustering
        cross_modal_cell_type_weight: Weight for cross-modal cell type alignment
        kl_weight_rna: Weight for RNA KL loss
        kl_weight_prot: Weight for protein KL loss
        global_step: Current global step
        total_steps: Total number of steps
        to_plot: Whether to plot metrics
        check_ilisi: Whether to check iLISI score

    Returns:
        Dictionary containing all calculated losses and metrics
    """
    # Get model outputs

    rna_inference_outputs, _, rna_loss_output_raw = rna_vae.module(rna_batch)
    protein_inference_outputs, _, protein_loss_output_raw = protein_vae.module(protein_batch)

    # Calculate base losses
    rna_loss_output = rna_loss_output_raw.loss * kl_weight_rna
    protein_loss_output = protein_loss_output_raw.loss * kl_weight_prot

    # Get latent representations
    rna_latent_mean = rna_inference_outputs["qz"].mean
    rna_latent_std = rna_inference_outputs["qz"].scale
    protein_latent_mean = protein_inference_outputs["qz"].mean
    protein_latent_std = protein_inference_outputs["qz"].scale

    # Calculate latent distances
    latent_distances = compute_pairwise_kl_two_items(
        rna_latent_mean,
        protein_latent_mean,
        rna_latent_std,
        protein_latent_std,
    )
    latent_distances = torch.clamp(latent_distances, max=torch.quantile(latent_distances, 0.90))

    # Calculate archetype distances
    archetype_dis = torch.cdist(  # normalize for cosine distance
        normalize(rna_batch["archetype_vec"], dim=1),
        normalize(protein_batch["archetype_vec"], dim=1),
    )
    archetype_dis = torch.clamp(archetype_dis, max=torch.quantile(archetype_dis, 0.90))

    # Calculate matching loss
    archetype_dis_tensor = torch.tensor(archetype_dis, dtype=torch.float, device=device)
    threshold = 0.0005

    # Normalize distances to [0,1] range
    # archetype_dis_tensor = (archetype_dis_tensor - archetype_dis_tensor.min()) / (
    #     archetype_dis_tensor.max() - archetype_dis_tensor.min() + 1e-8
    # )
    # latent_distances = (latent_distances - latent_distances.min()) / (
    #     latent_distances.max() - latent_distances.min() + 1e-8
    # )

    # Create a mask for the closest 10% of archetype distances
    percentile_10 = torch.quantile(archetype_dis_tensor.flatten(), 0.10)
    closest_pairs_mask = archetype_dis_tensor <= percentile_10

    # Apply the mask to latent distances for similarity calculation
    squared_diff = (latent_distances - archetype_dis_tensor) ** 2

    # Only consider closest 10% for stress loss calculation
    squared_diff_masked = squared_diff * closest_pairs_mask
    stress_loss = squared_diff_masked.sum() / (
        closest_pairs_mask.sum() + 1e-8
    )  # Avoid division by zero

    # For acceptable range, consider threshold-based approach
    acceptable_range_mask = (archetype_dis_tensor < threshold) & (latent_distances < threshold)
    num_cells = squared_diff.numel()
    num_acceptable = acceptable_range_mask.sum()
    exact_pairs = 10 * torch.diag(latent_distances).mean()

    reward_strength = 0
    reward = reward_strength * (num_acceptable.float() / num_cells)
    matching_loss = (stress_loss - reward + exact_pairs) * matching_weight

    # Calculate contrastive loss
    print(f"rna_latent_mean.shape: {rna_latent_mean.shape}")
    print(f"rna_latent_std.shape: {rna_latent_std.shape}")
    print(f"protein_latent_mean.shape: {protein_latent_mean.shape}")
    print(f"protein_latent_std.shape: {protein_latent_std.shape}")
    if self.mode == "train":
        rna_distances = compute_pairwise_kl(rna_latent_mean, rna_latent_std)
        prot_distances = compute_pairwise_kl(protein_latent_mean, protein_latent_std)
        distances = prot_distances + rna_distances
    else:
        rna_distances = compute_pairwise_kl(rna_latent_mean, rna_latent_std)
        prot_distances = compute_pairwise_kl(protein_latent_mean, protein_latent_std)
        distances = prot_distances + rna_distances
    # Get cell type and neighborhood info
    cell_neighborhood_info_protein = torch.tensor(
        protein_vae.adata[protein_batch["labels"]].obs["CN"].cat.codes.values
    ).to(device)
    cell_neighborhood_info_rna = torch.tensor(
        rna_vae.adata[rna_batch["labels"]].obs["CN"].cat.codes.values
    ).to(device)

    rna_major_cell_type = (
        torch.tensor(rna_vae.adata[rna_batch["labels"]].obs["major_cell_types"].values.codes)
        .to(device)
        .squeeze()
    )
    protein_major_cell_type = (
        torch.tensor(
            protein_vae.adata[protein_batch["labels"]].obs["major_cell_types"].values.codes
        )
        .to(device)
        .squeeze()
    )

    # Create masks for different cell type and neighborhood combinations
    num_cells = rna_batch["X"].shape[0]
    same_cn_mask = cell_neighborhood_info_rna.unsqueeze(
        0
    ) == cell_neighborhood_info_protein.unsqueeze(1)
    same_major_cell_type = rna_major_cell_type.unsqueeze(0) == protein_major_cell_type.unsqueeze(1)
    diagonal_mask = torch.eye(num_cells, dtype=torch.bool, device=device)

    distances = distances.masked_fill(diagonal_mask, 0)

    same_major_type_same_cn_mask = (same_major_cell_type * same_cn_mask).type(torch.bool)
    same_major_type_different_cn_mask = (same_major_cell_type * ~same_cn_mask).type(torch.bool)
    different_major_type_same_cn_mask = (~same_major_cell_type * same_cn_mask).type(torch.bool)
    different_major_type_different_cn_mask = (~same_major_cell_type * ~same_cn_mask).type(
        torch.bool
    )

    same_major_type_same_cn_mask.masked_fill_(diagonal_mask, 0)
    same_major_type_different_cn_mask.masked_fill_(diagonal_mask, 0)
    different_major_type_same_cn_mask.masked_fill_(diagonal_mask, 0)
    different_major_type_different_cn_mask.masked_fill_(diagonal_mask, 0)

    # Calculate contrastive loss
    contrastive_loss = 0.0
    if same_major_type_same_cn_mask.sum() > 0:
        contrastive_loss += -distances.masked_select(same_major_type_same_cn_mask).mean()
    if different_major_type_same_cn_mask.sum() > 0:
        contrastive_loss += distances.masked_select(different_major_type_same_cn_mask).mean()
    if different_major_type_different_cn_mask.sum() > 0:
        contrastive_loss += (
            distances.masked_select(different_major_type_different_cn_mask).mean() * 0.5
        )
    if same_major_type_different_cn_mask.sum() > 0:
        contrastive_loss += -distances.masked_select(same_major_type_different_cn_mask).mean() * 0.5
    contrastive_loss = contrastive_loss * contrastive_weight

    # Calculate cell type clustering loss
    rna_raw_cell_type_clustering_loss = run_cell_type_clustering_loss(
        rna_vae.adata, rna_latent_mean, rna_batch["labels"]
    )
    prot_raw_cell_type_clustering_loss = run_cell_type_clustering_loss(
        protein_vae.adata, protein_latent_mean, protein_batch["labels"]
    )
    # Add a balance term with margin to ensure both RNA and protein losses are similar
    # Only penalize differences larger than the margin
    margin = 0.1  # Acceptable difference margin
    loss_diff = torch.abs(rna_raw_cell_type_clustering_loss - prot_raw_cell_type_clustering_loss)
    balance_term = torch.nn.functional.relu(loss_diff - margin)  # Zero if within margin

    # Calculate cross-modal cell type clustering loss as a completely separate loss
    cross_modal_cell_type_loss_raw = calculate_cross_modal_cell_type_loss(
        rna_vae.adata,
        protein_vae.adata,
        rna_latent_mean,
        protein_latent_mean,
        rna_batch["labels"],
        protein_batch["labels"],
        device,
    )
    cross_modal_cell_type_loss = cross_modal_cell_type_loss_raw * cross_modal_cell_type_weight

    # Combined cell type clustering loss (without cross-modal component)
    cell_type_clustering_loss = (
        rna_raw_cell_type_clustering_loss + prot_raw_cell_type_clustering_loss + balance_term
    ) * cell_type_clustering_weight

    print(f"rna_raw_cell_type_clustering_loss: {rna_raw_cell_type_clustering_loss}")
    print(f"prot_raw_cell_type_clustering_loss: {prot_raw_cell_type_clustering_loss}")
    print(f"cross_modal_cell_type_loss_raw: {cross_modal_cell_type_loss_raw}")
    print(f"cross_modal_cell_type_loss: {cross_modal_cell_type_loss}")
    print(f"loss_diff: {loss_diff}, margin: {margin}")
    print(f"balance_term: {balance_term}")
    print(f"cell_type_clustering_loss: {cell_type_clustering_loss}")
    # Calculate similarity loss
    rna_dis = torch.cdist(rna_latent_mean, rna_latent_mean)
    prot_dis = torch.cdist(protein_latent_mean, protein_latent_mean)
    rna_prot_dis = torch.cdist(rna_latent_mean, protein_latent_mean)
    similarity_loss_raw = torch.abs(
        ((rna_dis.abs().mean() + prot_dis.abs().mean()) / 2) - rna_prot_dis.abs().mean()
    )
    similarity_loss = (
        similarity_loss_raw * similarity_weight
        if similarity_active
        else torch.tensor(0.0).to(device)
    )

    # Calculate total loss
    total_loss = (
        rna_loss_output
        + protein_loss_output
        + contrastive_loss
        + matching_loss
        + similarity_loss
        + cell_type_clustering_loss
        + cross_modal_cell_type_loss
    )

    # Prepare metrics for plotting if needed

    # Create losses dictionary
    losses = {
        "total_loss": total_loss,
        "rna_loss": rna_loss_output,
        "protein_loss": protein_loss_output,
        "contrastive_loss": contrastive_loss,
        "matching_loss": matching_loss,
        "similarity_loss": similarity_loss,
        "similarity_loss_raw": similarity_loss_raw,
        "cell_type_clustering_loss": cell_type_clustering_loss,
        "cross_modal_cell_type_loss": cross_modal_cell_type_loss,
        "cross_modal_cell_type_loss_raw": cross_modal_cell_type_loss_raw,
        "latent_distances": latent_distances.mean(),
        "num_acceptable": num_acceptable,
        "num_cells": num_cells,
        "exact_pairs": exact_pairs,
    }
    if to_plot or check_ilisi:
        rna_latent_mean_numpy = rna_latent_mean.detach().cpu().numpy()
        protein_latent_mean_numpy = protein_latent_mean.detach().cpu().numpy()
        # Create combined latent AnnData
        combined_latent = anndata.concat(
            [
                AnnData(rna_latent_mean_numpy, obs=rna_vae.adata[rna_batch["labels"]].obs),
                AnnData(
                    protein_latent_mean_numpy, obs=protein_vae.adata[protein_batch["labels"]].obs
                ),
            ],
            join="outer",
            label="modality",
            keys=["RNA", "Protein"],
        )
        # Clear any existing neighbors data to ensure clean calculation
        combined_latent.obsp.pop(
            "connectivities", None
        ) if "connectivities" in combined_latent.obsp else None
        combined_latent.obsp.pop("distances", None) if "distances" in combined_latent.obsp else None
        combined_latent.uns.pop("neighbors", None) if "neighbors" in combined_latent.uns else None

        # Calculate neighbors with cosine metric for iLISI
        sc.pp.neighbors(combined_latent, use_rep="X", n_neighbors=15)

    if to_plot:
        rna_latent_mean_numpy = rna_latent_mean.detach().cpu().numpy()
        rna_latent_std_numpy = rna_latent_std.detach().cpu().numpy()
        protein_latent_mean_numpy = protein_latent_mean.detach().cpu().numpy()
        protein_latent_std_numpy = protein_latent_std.detach().cpu().numpy()

        plot_latent_pca_both_modalities_cn(
            rna_latent_mean_numpy,
            protein_latent_mean_numpy,
            rna_vae.adata,
            protein_vae.adata,
            index_rna=rna_batch["labels"],
            index_prot=protein_batch["labels"],
            global_step=global_step,
        )
        plot_latent_pca_both_modalities_by_celltype(
            rna_vae.adata,
            protein_vae.adata,
            rna_latent_mean_numpy,
            protein_latent_mean_numpy,
            index_rna=rna_batch["labels"],
            index_prot=protein_batch["labels"],
            global_step=global_step,
        )
        plot_rna_protein_matching_means_and_scale(
            rna_latent_mean_numpy,
            protein_latent_mean_numpy,
            rna_latent_std_numpy,
            protein_latent_std_numpy,
            archetype_dis,
            global_step=global_step,
        )
        pf.plot_pca_umap_latent_space_during_train(
            self.mode,
            combined_latent,
            epoch=int(global_step / total_steps),
            global_step=global_step,
        )

    if check_ilisi:
        # Calculate iLISI score using the latent representations we already have
        # No need to recreate the combined latent AnnData, just use the existing representations

        # Calculate iLISI score
        ilisi_score = bar_nick_utils.calculate_iLISI(combined_latent, "modality", plot_flag=False)
        losses["ilisi_score"] = ilisi_score
        print(f"iLISI score: {ilisi_score}")
        # if to_plot:
    return losses


def calculate_cross_modal_cell_type_loss(
    rna_adata,
    prot_adata,
    rna_latent_mean,
    protein_latent_mean,
    rna_indices,
    prot_indices,
    device="cuda:0" if torch.cuda.is_available() else "cpu",
):
    """Calculate cross-modal cell type clustering loss to push cell types from different modalities
    to be close to each other in the latent space.

    Args:
        rna_adata: RNA AnnData object with cell type information
        prot_adata: Protein AnnData object with cell type information
        rna_latent_mean: RNA latent mean representation from the VAE
        protein_latent_mean: Protein latent mean representation from the VAE
        rna_indices: Indices of RNA cells to use
        prot_indices: Indices of protein cells to use
        device: Device to use for calculations

    Returns:
        Cross-modal cell type clustering loss tensor
    """
    # Get cell types for each modality
    rna_cell_types = torch.tensor(
        rna_adata[rna_indices].obs["major_cell_types"].cat.codes.values
    ).to(device)
    prot_cell_types = torch.tensor(
        prot_adata[prot_indices].obs["major_cell_types"].cat.codes.values
    ).to(device)

    # Get unique cell types from both modalities
    unique_rna_types = torch.unique(rna_cell_types)
    unique_prot_types = torch.unique(prot_cell_types)

    # Skip if either modality has only one cell type
    if len(unique_rna_types) <= 1 or len(unique_prot_types) <= 1:
        return torch.tensor(0.0).to(device)

    # Calculate centroids for each cell type in each modality
    rna_centroids = []
    prot_centroids = []
    rna_type_to_idx = {}
    prot_type_to_idx = {}

    # Create mapping from cell type to index
    for i, ct in enumerate(unique_rna_types):
        rna_type_to_idx[ct.item()] = i

    for i, ct in enumerate(unique_prot_types):
        prot_type_to_idx[ct.item()] = i

    # Calculate centroids for RNA modality
    for ct in unique_rna_types:
        mask = rna_cell_types == ct
        if mask.sum() > 0:
            cells = rna_latent_mean[mask]
            rna_centroids.append(cells.mean(dim=0))

    # Calculate centroids for protein modality
    for ct in unique_prot_types:
        mask = prot_cell_types == ct
        if mask.sum() > 0:
            cells = protein_latent_mean[mask]
            prot_centroids.append(cells.mean(dim=0))

    if len(rna_centroids) <= 1 or len(prot_centroids) <= 1:
        return torch.tensor(0.0).to(device)

    rna_centroids = torch.stack(rna_centroids)
    prot_centroids = torch.stack(prot_centroids)

    # Cross-modal loss components

    # 1. KL divergence between corresponding cell type distributions
    cross_modal_loss = 0.0
    common_cell_types = set(rna_type_to_idx.keys()).intersection(set(prot_type_to_idx.keys()))

    if len(common_cell_types) <= 1:
        return torch.tensor(0.0).to(device)

    # Calculate centroid distances between matching cell types
    centroid_distance = 0.0
    for ct in common_cell_types:
        rna_idx = rna_type_to_idx[ct]
        prot_idx = prot_type_to_idx[ct]

        # Euclidean distance between centroids
        dist = torch.norm(rna_centroids[rna_idx] - prot_centroids[prot_idx])
        centroid_distance += dist

    centroid_distance = centroid_distance / len(common_cell_types)

    # 2. Structure preservation between modalities
    # Calculate pairwise distances between all centroids within each modality
    rna_centroid_dists = torch.cdist(rna_centroids, rna_centroids)
    prot_centroid_dists = torch.cdist(prot_centroids, prot_centroids)

    # For common cell types, calculate structure preservation loss
    structure_loss = 0.0
    count = 0

    for ct1 in common_cell_types:
        for ct2 in common_cell_types:
            if ct1 != ct2:
                rna_idx1 = rna_type_to_idx[ct1]
                rna_idx2 = rna_type_to_idx[ct2]
                prot_idx1 = prot_type_to_idx[ct1]
                prot_idx2 = prot_type_to_idx[ct2]

                # Compare the distance between these cell types in RNA vs protein space
                rna_dist = rna_centroid_dists[rna_idx1, rna_idx2]
                prot_dist = prot_centroid_dists[prot_idx1, prot_idx2]

                # Square difference in normalized distances
                diff = (rna_dist - prot_dist) ** 2
                structure_loss += diff
                count += 1

    if count > 0:
        structure_loss = structure_loss / count

    # Combine losses: centroid alignment and structure preservation
    cross_modal_loss = centroid_distance + 0.5 * structure_loss

    return cross_modal_loss


# %%
def continue_training_from_checkpoint(
    checkpoint_path,
    additional_epochs=10,
    batch_size=None,
    lr=None,
    device="cuda:0" if torch.cuda.is_available() else "cpu",
    **kwargs,
):
    """Continue training from a checkpoint.

    Args:
        checkpoint_path: Path to the checkpoint directory
        additional_epochs: Number of additional epochs to train
        batch_size: New batch size (if None, use the one from checkpoint)
        lr: New learning rate (if None, use the one from checkpoint)
        device: Device to use for training
        **kwargs: Additional training parameters to override

    Returns:
        rna_vae: Trained RNA VAE model
        protein_vae: Trained protein VAE model
    """
    print(f"Continuing training from checkpoint: {checkpoint_path}")

    # Load models and state
    rna_vae, protein_vae, training_state = DualVAETrainingPlan.load_from_checkpoint(
        checkpoint_path, device
    )

    # Load config
    with open(f"{checkpoint_path}/model_config.json", "r") as f:
        config = json.load(f)

    # Use values from config if not provided
    if batch_size is None:
        batch_size = config["batch_size"]
    if lr is None:
        lr = config["lr"]

    # Override config with kwargs
    for key, value in kwargs.items():
        config[key] = value

    # Setup training parameters
    plan_kwargs = {
        "protein_vae": protein_vae,
        "rna_vae": rna_vae,
        "contrastive_weight": config.get("contrastive_weight", 1.0),
        "similarity_weight": config.get("similarity_weight", 1000.0),
        "diversity_weight": config.get("diversity_weight", 0.1),
        "cell_type_clustering_weight": config.get("cell_type_clustering_weight", 1.0),
        "cross_modal_cell_type_weight": config.get("cross_modal_cell_type_weight", 1.0),
        "matching_weight": config.get("matching_weight", 100.0),
        "adv_weight": config.get("adv_weight", 0.1),
        "plot_x_times": config.get("plot_x_times", 5),
        "batch_size": batch_size,
        "max_epochs": additional_epochs,
        "lr": lr,
        "kl_weight_rna": config.get("kl_weight_rna", 1.0),
        "kl_weight_prot": config.get("kl_weight_prot", 1.0),
        "gradient_clip_val": config.get("gradient_clip_val", 1.0),
        "check_val_every_n_epoch": config.get("check_val_every_n_epoch", 1),
    }

    train_kwargs = {
        "max_epochs": additional_epochs,
        "batch_size": batch_size,
        "train_size": config.get("train_size", 0.9),
        "validation_size": config.get("validation_size", 0.1),
        "accumulate_grad_batches": config.get("accumulate_grad_batches", 1),
        "check_val_every_n_epoch": config.get("check_val_every_n_epoch", 1),
    }

    # Initialize training plan - required for scVI
    rna_vae._training_plan_cls = DualVAETrainingPlan

    # Set training flag to False to trigger training
    rna_vae.is_trained_ = False
    protein_vae.is_trained_ = False

    # Train for additional epochs
    print(f"Starting training for {additional_epochs} additional epochs...")
    rna_vae.train(**train_kwargs, plan_kwargs=plan_kwargs)

    # Set trained flag again
    rna_vae.is_trained_ = True
    protein_vae.is_trained_ = True

    return rna_vae, protein_vae


# %%
def get_latent_embedding(
    model, adata, batch_size=128, device="cuda:0" if torch.cuda.is_available() else "cpu"
):
    """Get latent embedding for data using a trained model.

    Args:
        model: Trained SCVI model (RNA or protein)
        adata: AnnData object with data to embed
        batch_size: Batch size for processing
        device: Device to use

    Returns:
        latent: NumPy array with latent embeddings
    """
    print(f"Getting latent embeddings for {adata.shape[0]} cells...")

    # Make sure model is in eval mode
    model.module.eval()

    # Process in batches to avoid memory issues
    latent_parts = []
    total_cells = adata.shape[0]
    for i in range(0, total_cells, batch_size):
        end_idx = min(i + batch_size, total_cells)
        indices = np.arange(i, end_idx)

        # Get batch data
        X = adata[indices].X
        if issparse(X):
            X = X.toarray()
        X = torch.tensor(X, dtype=torch.float32).to(device)

        # Get batch indices if they exist, otherwise use zeros
        if "_scvi_batch" in adata.obs:
            batch_indices = torch.tensor(
                adata[indices].obs["_scvi_batch"].values, dtype=torch.long
            ).to(device)
        else:
            batch_indices = torch.zeros(len(indices), dtype=torch.long).to(device)

        # Get archetype vectors if they exist, otherwise use zeros
        if "archetype_vec" in adata.obsm:
            archetype_vec = torch.tensor(
                adata[indices].obsm["archetype_vec"], dtype=torch.float32
            ).to(device)
        else:
            latent_dim = model.module.n_latent  # Typically the dimensions match
            archetype_vec = torch.zeros((len(indices), latent_dim), dtype=torch.float32).to(device)

        # Create batch
        batch = {
            "X": X,
            "batch": batch_indices,
            "labels": indices,
            "archetype_vec": archetype_vec,
        }

        # Get latent representation
        with torch.no_grad():
            inference_outputs, _, _ = model.module(batch)
            latent_mean = inference_outputs["qz"].mean.detach().cpu().numpy()
            latent_parts.append(latent_mean)

        if (i + batch_size) % (10 * batch_size) == 0:
            print(f"Processed {i + batch_size} / {total_cells} cells")

    # Combine all batches
    latent = np.vstack(latent_parts)
    print(f"✓ Generated latent embeddings with shape {latent.shape}")

    return latent


# %%
