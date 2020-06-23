# modify from https://github.com/tensorflow/privacy/blob/master/tutorials/mnist_dpsgd_tutorial.py
"""Training a CNN on MNIST with differentially private SGD optimizer."""

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
flags.DEFINE_float('learning_rate', .25, 'Learning rate for training')
flags.DEFINE_float('noise_multiplier', 0.6,
                      'Ratio of the standard deviation to the clipping norm')
flags.DEFINE_float('l2_norm_clip', 1.5, 'Clipping norm')
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

def cnn_model_fn(features, labels, mode):
  """Model function for a CNN."""

  # Define CNN architecture using tf.keras.layers.
  input_layer = tf.reshape(features['x'], [-1, 28, 28, 1])
  y = tf.keras.layers.Conv2D(16, 8,
                             strides=2,
                             padding='same',
                             activation='relu').apply(input_layer)
  y = tf.keras.layers.MaxPool2D(2, 1).apply(y)
  y = tf.keras.layers.Conv2D(32, 4,
                             strides=2,
                             padding='valid',
                             activation='relu').apply(y)
  y = tf.keras.layers.MaxPool2D(2, 1).apply(y)
  y = tf.keras.layers.Flatten().apply(y)
  y = tf.keras.layers.Dense(32, activation='relu').apply(y)
  logits = tf.keras.layers.Dense(10).apply(y)

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


def load_mnist():
  """Loads MNIST and preprocesses to combine training and validation data."""
  train, test = tf.keras.datasets.mnist.load_data()
  train_data, train_labels = train
  test_data, test_labels = test

  train_data = np.array(train_data, dtype=np.float32) / 255
  test_data = np.array(test_data, dtype=np.float32) / 255

  train_labels = np.array(train_labels, dtype=np.int32)
  test_labels = np.array(test_labels, dtype=np.int32)

  assert train_data.min() == 0.
  assert train_data.max() == 1.
  assert test_data.min() == 0.
  assert test_data.max() == 1.
  assert train_labels.ndim == 1
  assert test_labels.ndim == 1

  return train_data, train_labels, test_data, test_labels


def main(unused_argv):
    tf.compat.v1.logging.set_verbosity(3)
    
      # Load training and test data.
    train_data, train_labels, test_data, test_labels = load_mnist()
    
      # Instantiate the tf.Estimator.
    mnist_classifier = tf.estimator.Estimator(model_fn=cnn_model_fn,
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
    steps_per_epoch = 60000 // 256
    test_accuracy_list = []
    for epoch in range(1, FLAGS.epochs + 1):
        np.random.seed(epoch)
        # Train the model for one step.
        mnist_classifier.train(input_fn=train_input_fn, steps=steps_per_epoch)
        
        # Evaluate the model and print results
        eval_results = mnist_classifier.evaluate(input_fn=eval_input_fn)
        test_accuracy = eval_results['accuracy']
        test_accuracy_list.append(test_accuracy)
        print('Test accuracy after %d epochs is: %.3f' % (epoch, test_accuracy))
        
        # Compute the privacy budget expended so far.
        if FLAGS.dpsgd:
            if FLAGS.subsampling=='Poisson':
                eps = compute_epsP(epoch,FLAGS.noise_multiplier,60000,256,1e-5)
                mu = compute_muP(epoch,FLAGS.noise_multiplier,60000,256)
            if FLAGS.subsampling=='Uniform':
                eps = compute_epsU(epoch,FLAGS.noise_multiplier,60000,256,1e-5)
                mu = compute_muU(epoch,FLAGS.noise_multiplier,60000,256)
          
            print('For delta=1e-5, the current MA epsilon is: %.2f' % 
                  compute_epsilon(epoch,FLAGS.noise_multiplier,60000,256,1e-5))
            print('For delta=1e-5, the current CLT epsilon is: %.2f' % eps)
            print('For delta=1e-5, the current mu is: %.2f' % mu)
          
            if mu>FLAGS.max_mu:
                break
        else:
          print('Trained with vanilla non-private SGD optimizer')
    

if __name__ == '__main__':
  app.run(main)
