# ==============================================================================
#  Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import matplotlib
import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.contrib.timeseries.python.timeseries import NumpyReader
from tensorflow.contrib.timeseries.python.timeseries import estimators as ts_estimators
from tensorflow.contrib.timeseries.python.timeseries import model as ts_model

matplotlib.use("agg")
tf.logging.set_verbosity(tf.logging.INFO)


class _LSTMModel(ts_model.SequentialTimeSeriesModel):
    """
    A time series model-building example using an RNNCell.
    """

    def __init__(self, num_units, num_features, dtype=tf.float32):
        """Initialize/configure the model object.
        Note that we do not start graph building here. Rather, this object is a
        configurable factory for TensorFlow graphs which are run by an Estimator.
        Args:
          num_units: The number of units in the model's LSTMCell.
          num_features: The dimensionality of the time series (features per
            timestep).
          dtype: The floating point data type to use.
        """
        super(_LSTMModel, self).__init__(
            # Pre-register the metrics we'll be outputting (just a mean here).
            train_output_names=["mean"],
            predict_output_names=["mean"],
            num_features=num_features,
            dtype=dtype)
        self._num_units = num_units
        # Filled in by initialize_graph()
        self._lstm_cell = None
        self._lstm_cell_run = None
        self._predict_from_lstm_output = None

    def initialize_graph(self, input_statistics):
        """Save templates for components, which can then be used repeatedly.
        This method is called every time a new graph is created. It's safe to start
        adding ops to the current default graph here, but the graph should be
        constructed from scratch.
        Args:
          input_statistics: A math_utils.InputStatistics object.
        """
        super(_LSTMModel, self).initialize_graph(input_statistics=input_statistics)
        self._lstm_cell = tf.nn.rnn_cell.LSTMCell(num_units=self._num_units)
        # Create templates so we don't have to worry about variable reuse.
        self._lstm_cell_run = tf.make_template(
            name_="lstm_cell",
            func_=self._lstm_cell,
            create_scope_now_=True)
        # Transforms LSTM output into mean predictions.
        self._predict_from_lstm_output = tf.make_template(
            name_="predict_from_lstm_output",
            func_=lambda inputs: tf.layers.dense(inputs=inputs, units=self.num_features),
            create_scope_now_=True)

    def get_start_state(self):
        """Return initial state for the time series model."""
        return (
            # Keeps track of the time associated with this state for error checking.
            tf.zeros([], dtype=tf.int64),
            # The previous observation or prediction.
            tf.zeros([self.num_features], dtype=self.dtype),
            # The state of the RNNCell (batch dimension removed since this parent
            # class will broadcast).
            [tf.squeeze(state_element, axis=0)
             for state_element
             in self._lstm_cell.zero_state(batch_size=1, dtype=self.dtype)])

    def _transform(self, data):
        """Normalize data based on input statistics to encourage stable training."""
        mean, variance = self._input_statistics.overall_feature_moments
        return (data - mean) / variance

    def _de_transform(self, data):
        """Transform data back to the input scale."""
        mean, variance = self._input_statistics.overall_feature_moments
        return data * variance + mean

    def _filtering_step(self, current_times, current_values, state, predictions):
        """Update model state based on observations.
        Note that we don't do much here aside from computing a loss. In this case
        it's easier to update the RNN state in _prediction_step, since that covers
        running the RNN both on observations (from this method) and our own
        predictions. This distinction can be important for probabilistic models,
        where repeatedly predicting without filtering should lead to low-confidence
        predictions.
        Args:
          current_times: A [batch size] integer Tensor.
          current_values: A [batch size, self.num_features] floating point Tensor
            with new observations.
          state: The model's state tuple.
          predictions: The output of the previous `_prediction_step`.
        Returns:
          A tuple of new state and a predictions dictionary updated to include a
          loss (note that we could also return other measures of goodness of fit,
          although only "loss" will be optimized).
        """
        state_from_time, prediction, lstm_state = state
        with tf.control_dependencies([tf.assert_equal(current_times, state_from_time)]):
            transformed_values = self._transform(current_values)
            # Use mean squared error across features for the loss.
            predictions["loss"] = tf.reduce_mean(
                (prediction - transformed_values) ** 2, axis=-1)
            # Keep track of the new observation in model state. It won't be run
            # through the LSTM until the next _imputation_step.
            new_state_tuple = (current_times, transformed_values, lstm_state)
        return new_state_tuple, predictions

    def _prediction_step(self, current_times, state):
        """Advance the RNN state using a previous observation or prediction."""
        _, previous_observation_or_prediction, lstm_state = state
        lstm_output, new_lstm_state = self._lstm_cell_run(
            inputs=previous_observation_or_prediction, state=lstm_state)
        next_prediction = self._predict_from_lstm_output(lstm_output)
        new_state_tuple = (current_times, next_prediction, new_lstm_state)
        return new_state_tuple, {"mean": self._de_transform(next_prediction)}

    def _imputation_step(self, current_times, state):
        """Advance model state across a gap."""
        # Does not do anything special if we're jumping across a gap. More advanced
        # models, especially probabilistic ones, would want a special case that
        # depends on the gap size.
        return state

    def _exogenous_input_step(
            self, current_times, current_exogenous_regressors, state):
        """Update model state based on exogenous regressors."""
        raise NotImplementedError(
            "Exogenous inputs are not implemented for this example.")


def train(y):
    x = np.array(range(len(y)))
    data = {
        tf.contrib.timeseries.TrainEvalFeatures.TIMES: x,
        tf.contrib.timeseries.TrainEvalFeatures.VALUES: y,
    }
    reader = NumpyReader(data)

    train_input_fn = tf.contrib.timeseries.RandomWindowInputFn(
        reader, batch_size=4, window_size=10)

    estimator = ts_estimators.TimeSeriesRegressor(
        model=_LSTMModel(num_features=1, num_units=128)
        # , optimizer=tf.train.AdamOptimizer(0.0025)
        , optimizer=tf.train.AdagradOptimizer(0.0025)
        # , optimizer=tf.train.FtrlOptimizer(0.0025)
        # , optimizer=tf.train.AdadeltaOptimizer(0.0025)
    )

    estimator.train(input_fn=train_input_fn, steps=2000)
    evaluation_input_fn = tf.contrib.timeseries.WholeDatasetInputFn(reader)
    evaluation = estimator.evaluate(input_fn=evaluation_input_fn, steps=1)

    # Predict starting after the evaluation
    (predictions,) = tuple(estimator.predict(
        input_fn=tf.contrib.timeseries.predict_continuation_input_fn(
            evaluation, steps=1)))

    return predictions


if __name__ == '__main__':
    df = pd.read_csv("../data/data/data_1.csv", index_col=False)

    df_err = df.iloc[-1:, :]
    df_err = df_err.append(pd.DataFrame(np.ones((1, 140)) * 0, columns=df_err.columns))

    # for index in range(140):
    for index in np.array([0, 2, 5, 6, 7, 9, 11, 12, 13, 15]):
        # index = 45
        df1 = df.iloc[:-1, index]
        df2 = df.iloc[-1:, index]

        y = df1.as_matrix()

        predictions = train(y)

        predicted_times = predictions['times']
        predicted = predictions["mean"]

        df_err.iloc[1, index] = predicted
        pp = pd.DataFrame(predicted)
        res = pd.concat([df2, pp], axis=0)

        res.loc['err'] = res.iloc[0, :] - res.iloc[1, :]
        res['mse'] = res.apply(lambda x: np.sqrt(sum(x * x) / len(x)), axis=1)

        df_err.loc['err'] = df_err.iloc[0, :] - df_err.iloc[1, :]
        df_err['mse'] = df_err.apply(lambda x: np.sqrt(sum(x * x) / len(x)), axis=1)

        print(res)
        print(df_err)

        df_err.to_csv("../data/data/df_err.csv")
