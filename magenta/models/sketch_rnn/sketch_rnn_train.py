# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""SketchRNN training."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from cStringIO import StringIO
import json
import os
import time
import urllib
import zipfile

# libraries required for visualisation:
from IPython.display import SVG, display
import svgwrite # conda install -c omnia svgwrite=1.1.6
import PIL
from PIL import Image
import matplotlib.pyplot as plt

# internal imports

import numpy as np
import requests
import tensorflow as tf

from magenta.models.sketch_rnn import model as sketch_rnn_model
from magenta.models.sketch_rnn import utils


tf.logging.set_verbosity(tf.logging.INFO)

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string(
    'data_dir',
    'https://github.com/hardmaru/magenta/raw/master',
    'The directory in which to find the dataset specified in model hparams. '
    'If data_dir starts with "http://" or "https://", the file will be fetched '
    'remotely.')
tf.app.flags.DEFINE_string(
    'log_root', '/tmp/sketch_rnn/models/default',
    'Directory to store model checkpoints, tensorboard.')
tf.app.flags.DEFINE_boolean(
    'resume_training', False,
    'Set to true to load previous checkpoint')
tf.app.flags.DEFINE_string(
    'hparams', '',
    'Pass in comma-separated key=value pairs such as '
    '\'save_every=40,decay_rate=0.99\' '
    '(no whitespace) to be read into the HParams object defined in model.py')

PRETRAINED_MODELS_URL = ('http://download.magenta.tensorflow.org/models/'
                         'sketch_rnn.zip')


def reset_graph():
  """Closes the current default session and resets the graph."""
  sess = tf.get_default_session()
  if sess:
    sess.close()
  tf.reset_default_graph()


def load_env(data_dir, model_dir):
  """Loads environment for inference mode, used in jupyter notebook."""
  model_params = sketch_rnn_model.get_default_hparams()
  with tf.gfile.Open(os.path.join(model_dir, 'model_config.json'), 'r') as f:
    model_params.parse_json(f.read())
  return load_dataset(data_dir, model_params, inference_mode=True)


def load_model(model_dir):
  """Loads model for inference mode, used in jupyter notebook."""
  model_params = sketch_rnn_model.get_default_hparams()
  with tf.gfile.Open(os.path.join(model_dir, 'model_config.json'), 'r') as f:
    model_params.parse_json(f.read())

  model_params.batch_size = 1  # only sample one at a time
  eval_model_params = sketch_rnn_model.copy_hparams(model_params)
  eval_model_params.use_input_dropout = 0
  eval_model_params.use_recurrent_dropout = 0
  eval_model_params.use_output_dropout = 0
  eval_model_params.is_training = 0
  sample_model_params = sketch_rnn_model.copy_hparams(eval_model_params)
  sample_model_params.max_seq_len = 1  # sample one point at a time
  return [model_params, eval_model_params, sample_model_params]


def download_pretrained_models(
    models_root_dir='/tmp/sketch_rnn/models',
    pretrained_models_url=PRETRAINED_MODELS_URL):
  """Download pretrained models to a temporary directory."""
  tf.gfile.MakeDirs(models_root_dir)
  zip_path = os.path.join(
      models_root_dir, os.path.basename(pretrained_models_url))
  if os.path.isfile(zip_path):
    tf.logging.info('%s already exists, using cached copy', zip_path)
  else:
    tf.logging.info('Downloading pretrained models from %s...',
                    pretrained_models_url)
    urllib.urlretrieve(pretrained_models_url, zip_path)
    tf.logging.info('Download complete.')
  tf.logging.info('Unzipping %s...', zip_path)
  with zipfile.ZipFile(zip_path) as models_zip:
    models_zip.extractall(models_root_dir)
  tf.logging.info('Unzipping complete.')


def load_dataset(data_dir, model_params, inference_mode=False):
  """Loads the .npz file, and splits the set into train/valid/test."""

  # normalizes the x and y columns usint the training set.
  # applies same scaling factor to valid and test set.

  datasets = []
  if isinstance(model_params.data_set, list):
    datasets = model_params.data_set
  else:
    datasets = [model_params.data_set]

  train_strokes = None
  valid_strokes = None
  test_strokes = None
  train_labels = []
  valid_labels = []
  test_labels = []

  # FRA: labels seuqeuntial number
  label = 0
  for dataset in datasets:
    data_filepath = os.path.join(data_dir, dataset)
    if data_dir.startswith('http://') or data_dir.startswith('https://'):
      tf.logging.info('Downloading %s', data_filepath)
      response = requests.get(data_filepath)
      data = np.load(StringIO(response.content))
    else:
      data = np.load(data_filepath)  # load this into dictionary
    tf.logging.info('Loaded {}/{}/{} from {}'.format(
        len(data['train']), len(data['valid']), len(data['test']),
        dataset))
    if train_strokes is None:
      train_strokes = data['train']
      valid_strokes = data['valid']
      test_strokes = data['test']
      train_labels = np.zeros(len(train_strokes))
      valid_labels = np.zeros(len(valid_strokes))
      test_labels = np.zeros(len(test_strokes))
    else:
      train_strokes = np.concatenate((train_strokes, data['train']))
      valid_strokes = np.concatenate((valid_strokes, data['valid']))
      test_strokes = np.concatenate((test_strokes, data['test']))
      train_labels = np.concatenate((train_labels, np.ones(len(train_strokes)) * label))
      valid_labels = np.concatenate((train_labels, np.ones(len(valid_strokes)) * label))
      test_labels = np.concatenate((train_labels, np.ones(len(test_strokes)) * label))
    label += 1

  all_strokes = np.concatenate((train_strokes, valid_strokes, test_strokes))
  num_points = 0
  for stroke in all_strokes:
    num_points += len(stroke)
  avg_len = num_points / len(all_strokes)
  tf.logging.info('Dataset combined: {} ({}/{}/{}), avg len {}'.format(
      len(all_strokes), len(train_strokes), len(valid_strokes),
      len(test_strokes), int(avg_len)))

  # calculate the max strokes we need.
  max_seq_len = utils.get_max_len(all_strokes)
  # overwrite the hps with this calculation.
  model_params.max_seq_len = max_seq_len

  tf.logging.info('model_params.max_seq_len %i.', model_params.max_seq_len)

  eval_model_params = sketch_rnn_model.copy_hparams(model_params)

  eval_model_params.use_input_dropout = 0
  eval_model_params.use_recurrent_dropout = 0
  eval_model_params.use_output_dropout = 0
  eval_model_params.is_training = 1

  if inference_mode:
    eval_model_params.batch_size = 1
    eval_model_params.is_training = 0

  sample_model_params = sketch_rnn_model.copy_hparams(eval_model_params)
  sample_model_params.batch_size = 1  # only sample one at a time
  sample_model_params.max_seq_len = 1  # sample one point at a time
  sample_model_params.is_training = 0

  train_set = utils.DataLoader(
      train_strokes,
      model_params.batch_size,
      labels=train_labels,
      max_seq_length=model_params.max_seq_len,
      random_scale_factor=model_params.random_scale_factor,
      augment_stroke_prob=model_params.augment_stroke_prob)

  normalizing_scale_factor = train_set.calculate_normalizing_scale_factor()
  train_set.normalize(normalizing_scale_factor)

  valid_set = utils.DataLoader(
      valid_strokes,
      eval_model_params.batch_size,
      labels=valid_labels,
      max_seq_length=eval_model_params.max_seq_len,
      random_scale_factor=0.0,
      augment_stroke_prob=0.0)
  valid_set.normalize(normalizing_scale_factor)

  test_set = utils.DataLoader(
      test_strokes,
      eval_model_params.batch_size,
      labels=test_labels,
      max_seq_length=eval_model_params.max_seq_len,
      random_scale_factor=0.0,
      augment_stroke_prob=0.0)
  test_set.normalize(normalizing_scale_factor)

  tf.logging.info('normalizing_scale_factor %4.4f.', normalizing_scale_factor)

  result = [
      train_set, valid_set, test_set, model_params, eval_model_params,
      sample_model_params, train_labels, valid_labels, test_labels
  ]
  return result


def evaluate_model(sess, model, data_set):
  """Returns the average weighted cost, reconstruction cost and KL cost."""
  total_cost = 0.0
  total_r_cost = 0.0
  total_kl_cost = 0.0
  for batch in range(data_set.num_batches):
    unused_orig_x, x, s, y = data_set.get_batch(batch)
    feed = {model.input_data: x, model.input_labels: y, model.sequence_lengths: s}
    (cost, r_cost,
     kl_cost) = sess.run([model.cost, model.r_cost, model.kl_cost], feed)
    total_cost += cost
    total_r_cost += r_cost
    total_kl_cost += kl_cost

  total_cost /= (data_set.num_batches)
  total_r_cost /= (data_set.num_batches)
  total_kl_cost /= (data_set.num_batches)
  return (total_cost, total_r_cost, total_kl_cost)


def load_checkpoint(sess, checkpoint_path):
  saver = tf.train.Saver(tf.global_variables())
  ckpt = tf.train.get_checkpoint_state(checkpoint_path)
  tf.logging.info('Loading model %s.', ckpt.model_checkpoint_path)
  saver.restore(sess, ckpt.model_checkpoint_path)


def save_model(sess, model_save_path, global_step):
  saver = tf.train.Saver(tf.global_variables())
  checkpoint_path = os.path.join(model_save_path, 'vector')
  tf.logging.info('saving model %s.', checkpoint_path)
  tf.logging.info('global_step %i.', global_step)
  saver.save(sess, checkpoint_path, global_step=global_step)




def train(sess, model, eval_model, sample_model, train_set, valid_set, test_set):
  """Train a sketch-rnn model."""
  ##### save image
  # little function that displays vector images and saves them to .svg
  def draw_strokes(data, factor=0.2, svg_filename = 'svg/sample.svg'):
	  tf.gfile.MakeDirs(os.path.dirname(svg_filename))
	  min_x, max_x, min_y, max_y = utils.get_bounds(data, factor)
	  dims = (50 + max_x - min_x, 50 + max_y - min_y)
	  dwg = svgwrite.Drawing(svg_filename, size=dims)
	  dwg.add(dwg.rect(insert=(0, 0), size=dims,fill='white'))
	  lift_pen = 1
	  abs_x = 25 - min_x 
	  abs_y = 25 - min_y
	  p = "M%s,%s " % (abs_x, abs_y)
	  command = "m"
	  for i in xrange(len(data)):
		  if (lift_pen == 1):
		    command = "m"
		  elif (command != "l"):
		    command = "l"
		  else:
		    command = ""
		  x = float(data[i,0])/factor
		  y = float(data[i,1])/factor
		  lift_pen = data[i, 2]
		  p += command+str(x)+","+str(y)+" "
	  the_color = "black"
	  stroke_width = 1
	  dwg.add(dwg.path(p).stroke(the_color,stroke_width).fill("none"))
	  dwg.save()
	  display(SVG(dwg.tostring()))

  # generate a 2D grid of many vector drawings
  def make_grid_svg(s_list, grid_space=10.0, grid_space_x=16.0):
	  def get_start_and_end(x):
		  x = np.array(x)
		  x = x[:, 0:2]
		  x_start = x[0]
		  x_end = x.sum(axis=0)
		  x = x.cumsum(axis=0)
		  x_max = x.max(axis=0)
		  x_min = x.min(axis=0)
		  center_loc = (x_max+x_min)*0.5
		  return x_start-center_loc, x_end
	  x_pos = 0.0
	  y_pos = 0.0
	  result = [[x_pos, y_pos, 1]]
	  for sample in s_list:
		  s = sample[0]
		  grid_loc = sample[1]
		  grid_y = grid_loc[0]*grid_space+grid_space*0.5
		  grid_x = grid_loc[1]*grid_space_x+grid_space_x*0.5
		  start_loc, delta_pos = get_start_and_end(s)

		  loc_x = start_loc[0]
		  loc_y = start_loc[1]
		  new_x_pos = grid_x+loc_x
		  new_y_pos = grid_y+loc_y
		  result.append([new_x_pos-x_pos, new_y_pos-y_pos, 0])

		  result += s.tolist()
		  result[-1][2] = 1
		  x_pos = new_x_pos+delta_pos[0]
		  y_pos = new_y_pos+delta_pos[1]
	  return np.array(result)

  def decode(z_input=None, draw_mode=True, temperature=0.1, factor=0.2, svg_filename='svg/decode_sample.svg'):
	  z = None
	  if z_input is not None:
			  z = [z_input]
	  sample_strokes, m = sketch_rnn_model.sample(sess, sample_model, seq_len=eval_model.hps.max_seq_len, temperature=temperature, z=z)
	  strokes = utils.to_normal_strokes(sample_strokes)
	  if draw_mode:
			  draw_strokes(strokes, factor, svg_filename)
	  return strokes
  ######

  # Setup summary writer.
  summary_writer = tf.summary.FileWriter(FLAGS.log_root, sess.graph)

  # Calculate trainable params.
  t_vars = tf.trainable_variables()
  count_t_vars = 0
  for var in t_vars:
    num_param = np.prod(var.get_shape().as_list())
    count_t_vars += num_param
    tf.logging.info('%s %s %i', var.name, str(var.get_shape()), num_param)
  tf.logging.info('Total trainable variables %i.', count_t_vars)
  model_summ = tf.summary.Summary()
  model_summ.value.add(
      tag='Num_Trainable_Params', simple_value=float(count_t_vars))
  summary_writer.add_summary(model_summ, 0)
  summary_writer.flush()

  # setup eval stats
  best_valid_cost = 100000000.0  # set a large init value
  valid_cost = 0.0

  # main train loop

  hps = model.hps
  start = time.time()

  for _ in range(hps.num_steps):

    step = sess.run(model.global_step)

    curr_learning_rate = ((hps.learning_rate - hps.min_learning_rate) *
                          (hps.decay_rate)**step + hps.min_learning_rate)
    curr_kl_weight = (hps.kl_weight - (hps.kl_weight - hps.kl_weight_start) *
                      (hps.kl_decay_rate)**step)

    _, x, s, y = train_set.random_batch()
    feed = {
        model.input_data: x,
        model.input_labels: y, #FRA
        model.sequence_lengths: s,
        model.lr: curr_learning_rate,
        model.kl_weight: curr_kl_weight
    }

    (train_cost, r_cost, kl_cost, _, train_step, _) = sess.run([
        model.cost, model.r_cost, model.kl_cost, model.final_state,
        model.global_step, model.train_op
    ], feed)

    if step % 20 == 0 and step > 0:

      end = time.time()
      time_taken = end - start

      cost_summ = tf.summary.Summary()
      cost_summ.value.add(tag='Train_Cost', simple_value=float(train_cost))
      reconstr_summ = tf.summary.Summary()
      reconstr_summ.value.add(
          tag='Train_Reconstr_Cost', simple_value=float(r_cost))
      kl_summ = tf.summary.Summary()
      kl_summ.value.add(tag='Train_KL_Cost', simple_value=float(kl_cost))
      lr_summ = tf.summary.Summary()
      lr_summ.value.add(
          tag='Learning_Rate', simple_value=float(curr_learning_rate))
      kl_weight_summ = tf.summary.Summary()
      kl_weight_summ.value.add(
          tag='KL_Weight', simple_value=float(curr_kl_weight))
      time_summ = tf.summary.Summary()
      time_summ.value.add(
          tag='Time_Taken_Train', simple_value=float(time_taken))

      output_format = ('step: %d, lr: %.6f, klw: %0.4f, cost: %.4f, '
                       'recon: %.4f, kl: %.4f, train_time_taken: %.4f')
      output_values = (step, curr_learning_rate, curr_kl_weight, train_cost,
                       r_cost, kl_cost, time_taken)
      output_log = output_format % output_values

      tf.logging.info(output_log)

      summary_writer.add_summary(cost_summ, train_step)
      summary_writer.add_summary(reconstr_summ, train_step)
      summary_writer.add_summary(kl_summ, train_step)
      summary_writer.add_summary(lr_summ, train_step)
      summary_writer.add_summary(kl_weight_summ, train_step)
      summary_writer.add_summary(time_summ, train_step)
      summary_writer.flush()
      start = time.time()

    if step % hps.save_every == 0 and step > 0:

      (valid_cost, valid_r_cost, valid_kl_cost) = evaluate_model(
          sess, eval_model, valid_set)

      end = time.time()
      time_taken_valid = end - start
      start = time.time()

      valid_cost_summ = tf.summary.Summary()
      valid_cost_summ.value.add(
          tag='Valid_Cost', simple_value=float(valid_cost))
      valid_reconstr_summ = tf.summary.Summary()
      valid_reconstr_summ.value.add(
          tag='Valid_Reconstr_Cost', simple_value=float(valid_r_cost))
      valid_kl_summ = tf.summary.Summary()
      valid_kl_summ.value.add(
          tag='Valid_KL_Cost', simple_value=float(valid_kl_cost))
      valid_time_summ = tf.summary.Summary()
      valid_time_summ.value.add(
          tag='Time_Taken_Valid', simple_value=float(time_taken_valid))

      output_format = ('best_valid_cost: %0.4f, valid_cost: %.4f, valid_recon: '
                       '%.4f, valid_kl: %.4f, valid_time_taken: %.4f')
      output_values = (min(best_valid_cost, valid_cost), valid_cost,
                       valid_r_cost, valid_kl_cost, time_taken_valid)
      output_log = output_format % output_values

      tf.logging.info(output_log)

      summary_writer.add_summary(valid_cost_summ, train_step)
      summary_writer.add_summary(valid_reconstr_summ, train_step)
      summary_writer.add_summary(valid_kl_summ, train_step)
      summary_writer.add_summary(valid_time_summ, train_step)
      summary_writer.flush()

			
      ######
      # randomly unconditionally generate 10 examples
      N = 10
      reconstructions = []
      for i in range(N):
	      temperature=float(i)/N+0.001
	      reconstructions.append([decode(temperature=temperature, draw_mode=False), [0, i]])
	      #print temperature

      stroke_grid = make_grid_svg(reconstructions)
      filename = 'svg/sample_' + str(step) + '.svg'
      draw_strokes(stroke_grid, svg_filename=filename)
      print(filename)

      #####

      if valid_cost < best_valid_cost:
        best_valid_cost = valid_cost

        save_model(sess, FLAGS.log_root, step)

        end = time.time()
        time_taken_save = end - start
        start = time.time()

        tf.logging.info('time_taken_save %4.4f.', time_taken_save)

        best_valid_cost_summ = tf.summary.Summary()
        best_valid_cost_summ.value.add(
            tag='Best_Valid_Cost', simple_value=float(best_valid_cost))

        summary_writer.add_summary(best_valid_cost_summ, train_step)
        summary_writer.flush()

        (eval_cost, eval_r_cost, eval_kl_cost) = evaluate_model(
            sess, eval_model, test_set)

        end = time.time()
        time_taken_eval = end - start
        start = time.time()

        eval_cost_summ = tf.summary.Summary()
        eval_cost_summ.value.add(tag='Eval_Cost', simple_value=float(eval_cost))
        eval_reconstr_summ = tf.summary.Summary()
        eval_reconstr_summ.value.add(
            tag='Eval_Reconstr_Cost', simple_value=float(eval_r_cost))
        eval_kl_summ = tf.summary.Summary()
        eval_kl_summ.value.add(
            tag='Eval_KL_Cost', simple_value=float(eval_kl_cost))
        eval_time_summ = tf.summary.Summary()
        eval_time_summ.value.add(
            tag='Time_Taken_Eval', simple_value=float(time_taken_eval))

        output_format = ('eval_cost: %.4f, eval_recon: %.4f, '
                         'eval_kl: %.4f, eval_time_taken: %.4f')
        output_values = (eval_cost, eval_r_cost, eval_kl_cost, time_taken_eval)
        output_log = output_format % output_values

        tf.logging.info(output_log)

        summary_writer.add_summary(eval_cost_summ, train_step)
        summary_writer.add_summary(eval_reconstr_summ, train_step)
        summary_writer.add_summary(eval_kl_summ, train_step)
        summary_writer.add_summary(eval_time_summ, train_step)
        summary_writer.flush()
  
def trainer(model_params):
  """Train a sketch-rnn model."""
  np.set_printoptions(precision=8, edgeitems=6, linewidth=200, suppress=True)

  tf.logging.info('sketch-rnn')
  tf.logging.info('Hyperparams:')
  for key, val in model_params.values().iteritems():
    tf.logging.info('%s = %s', key, str(val))
  tf.logging.info('Loading data files.')
  datasets = load_dataset(FLAGS.data_dir, model_params)

  train_set = datasets[0]
  valid_set = datasets[1]
  test_set = datasets[2]
  model_params = datasets[3]
  eval_model_params = datasets[4]
  sample_model_params = datasets[5]

  reset_graph()
  model = sketch_rnn_model.Model(model_params)
  eval_model = sketch_rnn_model.Model(eval_model_params, reuse=True)
  print(eval_model_params)
  sample_model = sketch_rnn_model.Model(sample_model_params, reuse=True)
  print(sample_model_params)

  sess = tf.InteractiveSession()
  sess.run(tf.global_variables_initializer())

  if FLAGS.resume_training:
    load_checkpoint(sess, FLAGS.log_root)

  # Write config file to json file.
  tf.gfile.MakeDirs(FLAGS.log_root)
  with tf.gfile.Open(
      os.path.join(FLAGS.log_root, 'model_config.json'), 'w') as f:
    json.dump(model_params.values(), f, indent=True)

  train(sess, model, eval_model, sample_model, train_set, valid_set, test_set)


def main(unused_argv):
  """Load model params, save config file and start trainer."""
  model_params = sketch_rnn_model.get_default_hparams()
  if FLAGS.hparams:
    model_params.parse(FLAGS.hparams)
  trainer(model_params)


def console_entry_point():
  tf.app.run(main)


if __name__ == '__main__':
  console_entry_point()
