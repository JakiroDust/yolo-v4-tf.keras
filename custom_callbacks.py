from tensorflow.keras import callbacks
import math

# BH 02/03/2023
import tensorflow as tf
from tensorflow.python.keras.utils import io_utils
from tensorflow.python.keras.utils import tf_utils
from tensorflow.python.platform import tf_logging as logging
import numpy as np
from tensorflow.python.keras.distribute import distributed_file_utils
import os
import re


class CosineAnnealingScheduler(callbacks.LearningRateScheduler):
    def __init__(self, epochs_per_cycle, lr_min, lr_max, verbose=0):
        super(callbacks.LearningRateScheduler, self).__init__()
        self.verbose = verbose
        self.lr_min = lr_min
        self.lr_max = lr_max
        self.epochs_per_cycle = epochs_per_cycle

    def schedule(self, epoch, lr):
        return self.lr_min + (self.lr_max - self.lr_min) *\
            (1 + math.cos(math.pi * (epoch % self.epochs_per_cycle) / self.epochs_per_cycle)) / 2


# BH 02/03/2023
class ModelCheckpointYolov4(callbacks.Callback):
    """Custom callback to save Keras model & weights to file during training.

    This callback automatically saves the Keras model & weights to
    file during training whenever some condition is met (e.g. when
    validation loss has reached a new minimum).

    `ModelCheckpointYolov4` is a modified version of the Keras callback
    `ModelCheckpoint`. The original callback CANNOT be used with this
    implementation of YOLOv4, as it saves the wrong model to file,
    namely `self.training_model` rather than `self.yolo_model`.

    The usage of this callback is identical to `ModelCheckpoint`, except
    for the addition of the `model_name` parameter. This parameter
    should be set to the name of the model to be saved (usually
    `model.yolo_model`).
    """

    def __init__(
        self,
        filepath,
        model_name=None,  # BH 02/03/2023
        monitor: str = "val_loss",
        verbose: int = 0,
        save_best_only: bool = False,
        save_weights_only: bool = False,
        mode: str = "auto",
        save_freq="epoch",
        options=None,
        initial_value_threshold=None,
        **kwargs,
    ):
        super().__init__()
        self._supports_tf_logs = True
        self.model_name = model_name
        self.monitor = monitor
        self.verbose = verbose
        self.filepath = io_utils.path_to_string(filepath)
        self.save_best_only = save_best_only
        self.save_weights_only = save_weights_only
        self.save_freq = save_freq
        self.epochs_since_last_save = 0
        self._batches_seen_since_last_saving = 0
        self._last_batch_seen = 0
        self.best = initial_value_threshold

        if save_weights_only:
            if options is None or isinstance(
                options, tf.train.CheckpointOptions
            ):
                self._options = options or tf.train.CheckpointOptions()
            else:
                raise TypeError(
                    "If save_weights_only is True, then `options` must be "
                    "either None or a tf.train.CheckpointOptions. "
                    f"Got {options}."
                )
        else:
            if options is None or isinstance(
                options, tf.saved_model.SaveOptions
            ):
                self._options = options or tf.saved_model.SaveOptions()
            else:
                raise TypeError(
                    "If save_weights_only is False, then `options` must be "
                    "either None or a tf.saved_model.SaveOptions. "
                    f"Got {options}."
                )

        # Deprecated field `load_weights_on_restart` is for loading the
        # checkpoint file from `filepath` at the start of `model.fit()`
        # TODO(rchao): Remove the arg during next breaking release.
        if "load_weights_on_restart" in kwargs:
            self.load_weights_on_restart = kwargs["load_weights_on_restart"]
            logging.warning(
                "`load_weights_on_restart` argument is deprecated. "
                "Please use `model.load_weights()` for loading weights "
                "before the start of `model.fit()`."
            )
        else:
            self.load_weights_on_restart = False

        # Deprecated field `period` is for the number of epochs between which
        # the model is saved.
        if "period" in kwargs:
            self.period = kwargs["period"]
            logging.warning(
                "`period` argument is deprecated. Please use `save_freq` "
                "to specify the frequency in number of batches seen."
            )
        else:
            self.period = 1

        if mode not in ["auto", "min", "max"]:
            logging.warning(
                "ModelCheckpoint mode %s is unknown, fallback to auto mode.",
                mode,
            )
            mode = "auto"

        if mode == "min":
            self.monitor_op = np.less
            if self.best is None:
                self.best = np.Inf
        elif mode == "max":
            self.monitor_op = np.greater
            if self.best is None:
                self.best = -np.Inf
        else:
            if "acc" in self.monitor or self.monitor.startswith("fmeasure"):
                self.monitor_op = np.greater
                if self.best is None:
                    self.best = -np.Inf
            else:
                self.monitor_op = np.less
                if self.best is None:
                    self.best = np.Inf

        if self.save_freq != "epoch" and not isinstance(self.save_freq, int):
            raise ValueError(
                f"Unrecognized save_freq: {self.save_freq}. "
                'Expected save_freq are "epoch" or integer'
            )

        # Only the chief worker writes model checkpoints, but all workers
        # restore checkpoint at on_train_begin().
        self._chief_worker_only = False

    def on_train_begin(self, logs=None):
        if self.load_weights_on_restart:
            filepath_to_load = (
                self._get_most_recently_modified_file_matching_pattern(
                    self.filepath
                )
            )
            if filepath_to_load is not None and self._checkpoint_exists(
                filepath_to_load
            ):
                try:
                    # `filepath` may contain placeholders such as `{epoch:02d}`,
                    # and thus it attempts to load the most recently modified
                    # file with file name matching the pattern.
                    self.model.load_weights(filepath_to_load)
                except (IOError, ValueError) as e:
                    raise ValueError(
                        f"Error loading file from {filepath_to_load}. "
                        f"Reason: {e}"
                    )

    def _implements_train_batch_hooks(self):
        # Only call batch hooks when saving on batch
        return self.save_freq != "epoch"

    def on_train_batch_end(self, batch, logs=None):
        if self._should_save_on_batch(batch):
            self._save_model(epoch=self._current_epoch, batch=batch, logs=logs)

    def on_epoch_begin(self, epoch, logs=None):
        self._current_epoch = epoch

    def on_epoch_end(self, epoch, logs=None):
        self.epochs_since_last_save += 1

        if self.save_freq == "epoch":
            self._save_model(epoch=epoch, batch=None, logs=logs)

    def _should_save_on_batch(self, batch):
        """Handles batch-level saving logic, supports steps_per_execution."""
        if self.save_freq == "epoch":
            return False

        if batch <= self._last_batch_seen:  # New epoch.
            add_batches = batch + 1  # batches are zero-indexed.
        else:
            add_batches = batch - self._last_batch_seen
        self._batches_seen_since_last_saving += add_batches
        self._last_batch_seen = batch

        if self._batches_seen_since_last_saving >= self.save_freq:
            self._batches_seen_since_last_saving = 0
            return True
        return False

    def _save_model(self, epoch, batch, logs):
        """Saves the model.
        Args:
            epoch: the epoch this iteration is in.
            batch: the batch this iteration is in. `None` if the `save_freq`
              is set to `epoch`.
            logs: the `logs` dict passed in to `on_batch_end` or `on_epoch_end`.
        """
        logs = logs or {}

        if (
            isinstance(self.save_freq, int) or self.epochs_since_last_save >= self.period
        ):
            # Block only when saving interval is reached.
            logs = tf_utils.sync_to_numpy_or_python_type(logs)
            self.epochs_since_last_save = 0
            filepath = self._get_file_path(epoch, batch, logs)

            try:
                if self.save_best_only:
                    current = logs.get(self.monitor)
                    if current is None:
                        logging.warning(
                            "Can save best model only with %s available, "
                            "skipping.",
                            self.monitor,
                        )
                    else:
                        if self.monitor_op(current, self.best):
                            if self.verbose > 0:
                                print(
                                    f"\nEpoch {epoch + 1}: {self.monitor} "
                                    "improved "
                                    f"from {self.best:.5f} to {current:.5f}, "
                                    f"saving model to {filepath}"
                                )
                            self.best = current
                            if self.save_weights_only:
                                self.model.save_weights(
                                    filepath,
                                    overwrite=True,
                                    options=self._options,
                                )
                            else:
                                # BH 02/03/2023
                                if self.model_name is None:
                                    self.model.save(
                                        filepath,
                                        overwrite=True,
                                        options=self._options,
                                    )
                                else:
                                    self.model_name.save(
                                        filepath,
                                        overwrite=True,
                                        options=self._options,
                                    )

                        else:
                            if self.verbose > 0:
                                print(
                                    f"\nEpoch {epoch + 1}: "
                                    f"{self.monitor} did not improve "
                                    f"from {self.best:.5f}"
                                )
                else:
                    if self.verbose > 0:
                        print(
                            f"\nEpoch {epoch + 1}: saving model to {filepath}"
                        )
                    if self.save_weights_only:
                        self.model.save_weights(
                            filepath, overwrite=True, options=self._options
                        )
                    else:
                        # BH 02/03/2023
                        if self.model_name is None:
                            self.model.save(
                                filepath,
                                overwrite=True,
                                options=self._options,
                            )
                        else:
                            self.model_name.save(
                                filepath,
                                overwrite=True,
                                options=self._options,
                            )

                self._maybe_remove_file()
            except IsADirectoryError:  # h5py 3.x
                raise IOError(
                    "Please specify a non-directory filepath for "
                    "ModelCheckpoint. Filepath used is an existing "
                    f"directory: {filepath}"
                )
            except IOError as e:  # h5py 2.x
                # `e.errno` appears to be `None` so checking the content of
                # `e.args[0]`.
                if "is a directory" in str(e.args[0]).lower():
                    raise IOError(
                        "Please specify a non-directory filepath for "
                        "ModelCheckpoint. Filepath used is an existing "
                        f"directory: f{filepath}"
                    )
                # Re-throw the error for any other causes.
                raise e

    def _get_file_path(self, epoch, batch, logs):
        """Returns the file path for checkpoint."""

        try:
            # `filepath` may contain placeholders such as
            # `{epoch:02d}`,`{batch:02d}` and `{mape:.2f}`. A mismatch between
            # logged metrics and the path's placeholders can cause formatting to
            # fail.
            if batch is None or "batch" in logs:
                file_path = self.filepath.format(epoch=epoch + 1, **logs)
            else:
                file_path = self.filepath.format(
                    epoch=epoch + 1, batch=batch + 1, **logs
                )
        except KeyError as e:
            raise KeyError(
                f'Failed to format this callback filepath: "{self.filepath}". '
                f"Reason: {e}"
            )
        self._write_filepath = distributed_file_utils.write_filepath(
            file_path, self.model.distribute_strategy
        )
        return self._write_filepath

    def _maybe_remove_file(self):
        # Remove the checkpoint directory in multi-worker training where this
        # worker should not checkpoint. It is a dummy directory previously saved
        # for sync distributed training.
        distributed_file_utils.remove_temp_dir_with_filepath(
            self._write_filepath, self.model.distribute_strategy
        )

    def _checkpoint_exists(self, filepath):
        """Returns whether the checkpoint `filepath` refers to exists."""
        if filepath.endswith(".h5"):
            return tf.io.gfile.exists(filepath)
        tf_saved_model_exists = tf.io.gfile.exists(filepath)
        tf_weights_only_checkpoint_exists = tf.io.gfile.exists(
            filepath + ".index"
        )
        return tf_saved_model_exists or tf_weights_only_checkpoint_exists

    def _get_most_recently_modified_file_matching_pattern(self, pattern):
        """Returns the most recently modified filepath matching pattern.
        Pattern may contain python formatting placeholder. If
        `tf.train.latest_checkpoint()` does not return None, use that;
        otherwise, check for most recently modified one that matches the
        pattern.
        In the rare case where there are more than one pattern-matching file
        having the same modified time that is most recent among all, return the
        filepath that is largest (by `>` operator, lexicographically using the
        numeric equivalents). This provides a tie-breaker when multiple files
        are most recent. Note that a larger `filepath` can sometimes indicate a
        later time of modification (for instance, when epoch/batch is used as
        formatting option), but not necessarily (when accuracy or loss is used).
        The tie-breaker is put in the logic as best effort to return the most
        recent, and to avoid undeterministic result.
        Modified time of a file is obtained with `os.path.getmtime()`.
        This utility function is best demonstrated via an example:
        ```python
        file_pattern = 'f.batch{batch:02d}epoch{epoch:02d}.h5'
        test_dir = self.get_temp_dir()
        path_pattern = os.path.join(test_dir, file_pattern)
        file_paths = [
            os.path.join(test_dir, file_name) for file_name in
            ['f.batch03epoch02.h5',
             'f.batch02epoch02.h5', 'f.batch01epoch01.h5']
        ]
        for file_path in file_paths:
          # Write something to each of the files
        self.assertEqual(
            _get_most_recently_modified_file_matching_pattern(path_pattern),
            file_paths[-1])
        ```
        Args:
            pattern: The file pattern that may optionally contain python
                placeholder such as `{epoch:02d}`.
        Returns:
            The most recently modified file's full filepath matching `pattern`.
            If `pattern` does not contain any placeholder, this returns the
            filepath that exactly matches `pattern`. Returns `None` if no match
            is found.
        """
        dir_name = os.path.dirname(pattern)
        base_name = os.path.basename(pattern)
        base_name_regex = "^" + re.sub(r"{.*}", r".*", base_name) + "$"

        # If tf.train.latest_checkpoint tells us there exists a latest
        # checkpoint, use that as it is more robust than `os.path.getmtime()`.
        latest_tf_checkpoint = tf.train.latest_checkpoint(dir_name)
        if latest_tf_checkpoint is not None and re.match(
            base_name_regex, os.path.basename(latest_tf_checkpoint)
        ):
            return latest_tf_checkpoint

        latest_mod_time = 0
        file_path_with_latest_mod_time = None
        n_file_with_latest_mod_time = 0
        file_path_with_largest_file_name = None

        if tf.io.gfile.exists(dir_name):
            for file_name in os.listdir(dir_name):
                # Only consider if `file_name` matches the pattern.
                if re.match(base_name_regex, file_name):
                    file_path = os.path.join(dir_name, file_name)
                    mod_time = os.path.getmtime(file_path)
                    if (
                        file_path_with_largest_file_name is None or file_path > file_path_with_largest_file_name
                    ):
                        file_path_with_largest_file_name = file_path
                    if mod_time > latest_mod_time:
                        latest_mod_time = mod_time
                        file_path_with_latest_mod_time = file_path
                        # In the case a file with later modified time is found,
                        # reset the counter for the number of files with latest
                        # modified time.
                        n_file_with_latest_mod_time = 1
                    elif mod_time == latest_mod_time:
                        # In the case a file has modified time tied with the
                        # most recent, increment the counter for the number of
                        # files with latest modified time by 1.
                        n_file_with_latest_mod_time += 1

        if n_file_with_latest_mod_time == 1:
            # Return the sole file that has most recent modified time.
            return file_path_with_latest_mod_time
        else:
            # If there are more than one file having latest modified time,
            # return the file path with the largest file name.
            return file_path_with_largest_file_name
