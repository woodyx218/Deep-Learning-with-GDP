# modify from https://github.com/tensorflow/privacy/blob/master/tutorials/mnist_dpsgd_tutorial.py
"""Training a one-layer NN on Adult data with differentially private SGD optimizer."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf

from absl import app
from absl import flags

from tensorflow_privacy.privacy.optimizers import dp_optimizer
from gdp_accountant import *

#### FLAGS
flags.DEFINE_boolean('dpsgd', True, 'If True, train with DP-SGD. If False, '
                        'train with vanilla SGD.')
flags.DEFINE_float('learning_rate', .01, 'Learning rate for training')
flags.DEFINE_float('noise_multiplier', 0.55,
                      'Ratio of the standard deviation to the clipping norm')
flags.DEFINE_float('l2_norm_clip', 5, 'Clipping norm')
flags.DEFINE_integer('epochs', 1, 'Number of epochs')
flags.DEFINE_string('model_dir', None, 'Model directory')
flags.DEFINE_float('max_mu', 2, 'Maximum mu before termination')
flags.DEFINE_string('subsampling', 'Poisson', 'Poisson or Uniform subsampling')
flags.DEFINE_integer('batch_size', 256, 'Batch size')
flags.DEFINE_integer(
    'microbatches', 256, 'Number of microbatches '
    '(must evenly divide batch_size)')

FLAGS = flags.FLAGS

np.random.seed(0)
tf.compat.v1.set_random_seed(0)

def nn_model_fn(features, labels, mode):

  # Define CNN architecture using tf.keras.layers.
  input_layer = tf.reshape(features['x'], [-1,123])
  y = tf.keras.layers.Dense(16,activation='relu').apply(input_layer)
  logits = tf.keras.layers.Dense(2).apply(y)

  # Calculate loss as a vector (to support microbatches in DP-SGD).
  vector_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
      labels=labels, logits=logits)
  # Define mean of loss across minibatch (for reporting through tf.Estimator).
  scalar_loss = tf.reduce_mean(vector_loss)

  # Configure the training op (for TRAIN mode).
  if mode == tf.estimator.ModeKeys.TRAIN:

    if FLAGS.dpsgd:
      # Use DP version of GradientDescentOptimizer. Other optimizers are
      # available in dp_optimizer. Most optimizers inheriting from
      # tf.train.Optimizer should be wrappable in differentially private
      # counterparts by calling dp_optimizer.optimizer_from_args().
      optimizer = dp_optimizer.DPGradientDescentGaussianOptimizer(
          l2_norm_clip=FLAGS.l2_norm_clip,
          noise_multiplier=FLAGS.noise_multiplier,
          num_microbatches=FLAGS.microbatches,
          learning_rate=FLAGS.learning_rate)
      opt_loss = vector_loss
    else:
      optimizer = tf.compat.v1.train.GradientDescentOptimizer(
          learning_rate=FLAGS.learning_rate)
      opt_loss = scalar_loss
    global_step = tf.compat.v1.train.get_global_step()
    train_op = optimizer.minimize(loss=opt_loss, global_step=global_step)
    # In the following, we pass the mean of the loss (scalar_loss) rather than
    # the vector_loss because tf.estimator requires a scalar loss. This is only
    # used for evaluation and debugging by tf.estimator. The actual loss being
    # minimized is opt_loss defined above and passed to optimizer.minimize().
    return tf.estimator.EstimatorSpec(mode=mode,
                                      loss=scalar_loss,
                                      train_op=train_op)

  # Add evaluation metrics (for EVAL mode).
  elif mode == tf.estimator.ModeKeys.EVAL:
    eval_metric_ops = {
        'accuracy':
            tf.compat.v1.metrics.accuracy(
                labels=labels,
                predictions=tf.argmax(input=logits, axis=1))
    }
    return tf.estimator.EstimatorSpec(mode=mode,
                                      loss=scalar_loss,
                                      eval_metric_ops=eval_metric_ops)


def load_adult():
  """Loads ADULT a2a as in LIBSVM and preprocesses to combine training and validation data."""
  """https://www.csie.ntu.edu.tw/~cjlin/libsvmtools/datasets/binary.html"""  
  import pandas as pd
  from sklearn.model_selection import KFold

  X=pd.read_csv("data/adult.csv")
  kf = KFold(n_splits=10)
  for train_index, test_index in kf.split(X):
      train, test = X.iloc[train_index,:], X.iloc[test_index,:]
  train_data = train.iloc[:,range(X.shape[1]-1)].values.astype('float32')
  test_data = test.iloc[:,range(X.shape[1]-1)].values.astype('float32')

  train_labels = (train.iloc[:,X.shape[1]-1]==1).astype('int32').values
  test_labels = (test.iloc[:,X.shape[1]-1]==1).astype('int32').values

  return train_data, train_labels, test_data, test_labels


def main(unused_argv):
  tf.compat.v1.logging.set_verbosity(3)

  # Load training and test data.
  train_data, train_labels, test_data, test_labels = load_adult()

  # Instantiate the tf.Estimator.
  adult_classifier = tf.estimator.Estimator(model_fn=nn_model_fn,
                                            model_dir=FLAGS.model_dir)

  # Create tf.Estimator input functions for the training and test data.
  eval_input_fn = tf.compat.v1.estimator.inputs.numpy_input_fn(
      x={'x': test_data},
      y=test_labels,
      num_epochs=1,
      shuffle=False)
  train_input_fn = tf.compat.v1.estimator.inputs.numpy_input_fn(
      x={'x': train_data},
      y=train_labels,
      batch_size=FLAGS.batch_size,
      num_epochs=FLAGS.epochs,
      shuffle=True)

  # Training loop.
  steps_per_epoch = 29305 // 256
  test_accuracy_list = []
  for epoch in range(1, FLAGS.epochs + 1):
    np.random.seed(epoch)

    # Train the model for one step.
    adult_classifier.train(input_fn=train_input_fn, steps=steps_per_epoch)

    # Evaluate the model and print results
    eval_results = adult_classifier.evaluate(input_fn=eval_input_fn)
    test_accuracy = eval_results['accuracy']
    test_accuracy_list.append(test_accuracy)
    print('Test accuracy after %d epochs is: %.3f' % (epoch, test_accuracy))
    
    # Compute the privacy budget expended so far.
    if FLAGS.dpsgd:
        if FLAGS.subsampling=='Poisson':
            eps = compute_epsP(epoch,FLAGS.noise_multiplier,29305,256,1e-5)
            mu = compute_muP(epoch,FLAGS.noise_multiplier,29305,256)
        if FLAGS.subsampling=='Uniform':
            eps = compute_epsU(epoch,FLAGS.noise_multiplier,29305,256,1e-5)
            mu = compute_muU(epoch,FLAGS.noise_multiplier,29305,256)
      
        print('For delta=1e-5, the current MA epsilon is: %.2f' % 
              compute_epsilon(epoch,FLAGS.noise_multiplier,29305,256,1e-5))
        print('For delta=1e-5, the current CLT epsilon is: %.2f' % eps)
        print('For delta=1e-5, the current mu is: %.2f' % mu)
      
        if mu>FLAGS.max_mu:
            break
    else:
      print('Trained with vanilla non-private SGD optimizer')
    
if __name__ == '__main__':
  app.run(main)
