#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Han Xiao <artex.xh@gmail.com> <https://hanxiao.github.io>
import contextlib
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from multiprocessing import Process

import numpy as np
import tensorflow as tf
import zmq
from tensorflow.python.estimator.estimator import Estimator
from tensorflow.python.estimator.model_fn import EstimatorSpec
from tensorflow.python.estimator.run_config import RunConfig
from tensorflow.python.tools.optimize_for_inference_lib import optimize_for_inference
from termcolor import colored
from zmq.utils import jsonapi

from .bert import modeling, tokenization
from .bert.extract_features import convert_lst_to_features, masked_reduce_mean, PoolingStrategy, \
    masked_reduce_max, mul_mask
from .helper import set_logger

_tf_ver = tf.__version__.split('.')
assert int(_tf_ver[0]) >= 1 and int(_tf_ver[1]) >= 10, 'Tensorflow >=1.10 is required!'

__version__ = '1.5.4'

_graph_tmp_file_ = tempfile.NamedTemporaryFile('w', delete=False).name


def _auto_bind(socket):
    if os.name == 'nt':  # for Windows
        socket.bind_to_random_port('tcp://*')
    else:
        # Get the location for tmp file for sockets
        try:
            tmp_dir = os.environ['ZEROMQ_SOCK_TMP_DIR']
            if not os.path.exists(tmp_dir):
                raise ValueError('This directory for sockets ({}) does not seems to exist.'.format(tmp_dir))
            tmp_dir = os.path.join(tmp_dir, str(uuid.uuid1())[:8])
        except KeyError:
            tmp_dir = '*'

        socket.bind('ipc://{}'.format(tmp_dir))
    return socket.getsockopt(zmq.LAST_ENDPOINT).decode('ascii')


class ServerCommand:
    terminate = b'TERMINATION'
    show_config = b'SHOW_CONFIG'
    new_job = b'REGISTER'


class BertServer(threading.Thread):
    def __init__(self, args):
        super().__init__()
        self.logger = set_logger(colored('VENTILATOR', 'magenta'))

        self.model_dir = args.model_dir
        self.max_seq_len = args.max_seq_len
        self.num_worker = args.num_worker
        self.max_batch_size = args.max_batch_size
        self.port = args.port
        self.args = args
        self.args_dict = {
            'model_dir': args.model_dir,
            'max_seq_len': args.max_seq_len,
            'num_worker': args.num_worker,
            'max_batch_size': args.max_batch_size,
            'port': args.port,
            'port_out': args.port_out,
            'pooling_layer': args.pooling_layer,
            'pooling_strategy': args.pooling_strategy.value,
            'tensorflow_version': tf.__version__,
            'python_version': sys.version,
            'server_start_time': str(datetime.now()),
            'use_xla_compiler': args.xla,
            'optimized_graph': _graph_tmp_file_
        }
        self.processes = []
        self.context = zmq.Context()

        # frontend facing client
        self.frontend = self.context.socket(zmq.PULL)
        self.frontend.bind('tcp://*:%d' % self.port)

        # pair connection between frontend and sink
        self.sink = self.context.socket(zmq.PAIR)
        self.addr_front2sink = _auto_bind(self.sink)

        # backend facing workers
        self.backend = self.context.socket(zmq.PUSH)
        self.addr_backend = _auto_bind(self.backend)

        # start the sink thread
        proc_sink = BertSink(self.args, self.addr_front2sink)
        proc_sink.start()
        self.processes.append(proc_sink)
        self.addr_sink = self.sink.recv().decode('ascii')

        # optimize the graph
        self.optimize_graph(args)

    def optimize_graph(self, args):
        config_fp = os.path.join(args.model_dir, 'bert_config.json')
        init_checkpoint = os.path.join(args.model_dir, 'bert_model.ckpt')
        # load json BERT config using standard io
        with tf.gfile.GFile(config_fp, 'r') as f:
            text = f.read()

        bert_config = modeling.BertConfig.from_dict(json.loads(text))
        self.logger.info('BERT config is loaded.')

        jit_scope = tf.contrib.compiler.jit.experimental_jit_scope if args.xla else contextlib.suppress

        with jit_scope():
            input_ids = tf.placeholder(tf.int32, (None, args.max_seq_len), 'input_ids')
            input_mask = tf.placeholder(tf.int32, (None, args.max_seq_len), 'input_mask')
            input_type_ids = tf.placeholder(tf.int32, (None, args.max_seq_len), 'input_type_ids')

            input_tensors = [input_ids, input_mask, input_type_ids]

            model = modeling.BertModel(
                config=bert_config,
                is_training=False,
                input_ids=input_ids,
                input_mask=input_mask,
                token_type_ids=input_type_ids,
                use_one_hot_embeddings=False)

            tvars = tf.trainable_variables()

            (assignment_map, initialized_variable_names
             ) = modeling.get_assignment_map_from_checkpoint(tvars, init_checkpoint)

            print('train vars: %d' % len(tvars))
            print('vars from checkpoint: %d' % len(initialized_variable_names))
            print('assignment map: %d' % len(assignment_map))

            tf.train.init_from_checkpoint(init_checkpoint, assignment_map)

            with tf.variable_scope("pooling"):
                if len(args.pooling_layer) == 1:
                    encoder_layer = model.all_encoder_layers[args.pooling_layer[0]]
                else:
                    all_layers = [model.all_encoder_layers[l] for l in args.pooling_layer]
                    encoder_layer = tf.concat(all_layers, -1)

                input_mask = tf.cast(input_mask, tf.float32)
                if args.pooling_strategy == PoolingStrategy.REDUCE_MEAN:
                    pooled = masked_reduce_mean(encoder_layer, input_mask)
                elif args.pooling_strategy == PoolingStrategy.REDUCE_MAX:
                    pooled = masked_reduce_max(encoder_layer, input_mask)
                elif args.pooling_strategy == PoolingStrategy.REDUCE_MEAN_MAX:
                    pooled = tf.concat([masked_reduce_mean(encoder_layer, input_mask),
                                        masked_reduce_max(encoder_layer, input_mask)], axis=1)
                elif args.pooling_strategy == PoolingStrategy.FIRST_TOKEN or \
                        args.pooling_strategy == PoolingStrategy.CLS_TOKEN:
                    pooled = tf.squeeze(encoder_layer[:, 0:1, :], axis=1)
                elif args.pooling_strategy == PoolingStrategy.LAST_TOKEN or \
                        args.pooling_strategy == PoolingStrategy.SEP_TOKEN:
                    seq_len = tf.cast(tf.reduce_sum(input_mask, axis=1), tf.int32)
                    rng = tf.range(0, tf.shape(seq_len)[0])
                    indexes = tf.stack([rng, seq_len - 1], 1)
                    pooled = tf.gather_nd(encoder_layer, indexes)
                elif args.pooling_strategy == PoolingStrategy.NONE:
                    pooled = mul_mask(encoder_layer, input_mask)
                else:
                    raise NotImplementedError()

            pooled = tf.identity(pooled, 'final_encodes')

            output_tensors = [pooled]
            tmp_g = tf.get_default_graph().as_graph_def()
            print('original: %d' % len(tmp_g.node), flush=True)
            # print('\n'.join([n.name for n in tf.get_default_graph().as_graph_def().node]))

            os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
            config = tf.ConfigProto(device_count={'GPU': 0}, allow_soft_placement=True)
            config.gpu_options.allow_growth = True
            config.gpu_options.per_process_gpu_memory_fraction = 0.5

            sess = tf.Session(config=config)
            sess.run(tf.global_variables_initializer())
            tmp_g = tf.graph_util.convert_variables_to_constants(sess, tmp_g, [n.name[:-2] for n in output_tensors])
            print('after freeze: %d' % len(tmp_g.node))
            before_opt = set(n.name for n in tmp_g.node)
            # prune unused nodes from graph
            dtypes = [n.dtype for n in input_tensors]
            tmp_g = optimize_for_inference(
                tmp_g,
                [n.name[:-2] for n in input_tensors],
                [n.name[:-2] for n in output_tensors],
                [dtype.as_datatype_enum for dtype in dtypes],
                False)
            print('after optimize: %d' % len(tmp_g.node))
            after_opt = set(n.name for n in tmp_g.node)
            removed = [n for n in before_opt if n not in after_opt]
            print('\n'.join(removed))

            with tf.gfile.GFile(_graph_tmp_file_, 'wb') as f:
                f.write(tmp_g.SerializeToString())

            sess.close()
            self.logger.info('graph is optimized and stored at %s' % _graph_tmp_file_)

    def close(self):
        self.logger.info('shutting down...')
        for p in self.processes:
            p.close()
        self.frontend.close()
        self.backend.close()
        self.sink.close()
        self.context.term()
        self.logger.info('terminated!')

    def run(self):
        num_req = 0
        run_on_gpu = False
        device_map = [-1] * self.num_worker
        if not self.args.cpu:
            try:
                import GPUtil
                num_all_gpu = len(GPUtil.getGPUs())
                avail_gpu = GPUtil.getAvailable(order='memory', limit=min(num_all_gpu, self.num_worker))
                num_avail_gpu = len(avail_gpu)
                if num_avail_gpu < self.num_worker:
                    self.logger.warn('only %d out of %d GPU(s) is available/free, but "-num_worker=%d"' %
                                     (num_avail_gpu, num_all_gpu, self.num_worker))
                    self.logger.warn('multiple workers will be allocated to one GPU, '
                                     'may not scale well and may raise out-of-memory')
                device_map = (avail_gpu * self.num_worker)[: self.num_worker]
                run_on_gpu = True
            except FileNotFoundError:
                self.logger.warn('nvidia-smi is missing, often means no gpu on this machine. '
                                 'fall back to cpu!')

        self.logger.info('device_map: \n\t\t%s' % '\n\t\t'.join(
            'worker %2d -> %s' % (w_id, ('gpu %2d' % g_id) if g_id >= 0 else 'cpu') for w_id, g_id in
            enumerate(device_map)))
        # start the backend processes
        for idx, device_id in enumerate(device_map):
            process = BertWorker(idx, self.args, self.addr_backend, self.addr_sink, device_id)
            self.processes.append(process)
            process.start()

        while True:
            try:
                request = self.frontend.recv_multipart()
                client, msg, req_id = request
                if msg == ServerCommand.show_config:
                    self.logger.info('new config request\treq id: %d\tclient: %s' % (int(req_id), client))
                    self.sink.send_multipart([client, msg,
                                              jsonapi.dumps({**{'client': client.decode('ascii'),
                                                                'num_subprocess': len(self.processes),
                                                                'ventilator -> worker': self.addr_backend,
                                                                'worker -> sink': self.addr_sink,
                                                                'ventilator <-> sink': self.addr_front2sink,
                                                                'server_current_time': str(datetime.now()),
                                                                'num_request': num_req,
                                                                'run_on_gpu': run_on_gpu,
                                                                'server_version': __version__},
                                                             **self.args_dict}), req_id])
                    continue

                self.logger.info('new encode request\treq id: %d\tclient: %s' % (int(req_id), client))
                num_req += 1
                seqs = jsonapi.loads(msg)
                num_seqs = len(seqs)
                # register a new job at sink
                self.sink.send_multipart([client, ServerCommand.new_job, b'%d' % num_seqs, req_id])

                job_id = client + b'#' + req_id
                if num_seqs > self.max_batch_size:
                    # partition the large batch into small batches
                    s_idx = 0
                    while s_idx < num_seqs:
                        tmp = seqs[s_idx: (s_idx + self.max_batch_size)]
                        if tmp:
                            partial_job_id = job_id + b'@%d' % s_idx
                            self.backend.send_multipart([partial_job_id, jsonapi.dumps(tmp)])
                        s_idx += len(tmp)
                else:
                    self.backend.send_multipart([job_id, msg])
            except zmq.error.ContextTerminated:
                self.logger.error('context is closed!')
            except ValueError:
                self.logger.error('received a wrongly-formatted request (expected 3 frames, got %d)' % len(request))
                self.logger.error('\n'.join('field %d: %s' % (idx, k) for idx, k in enumerate(request)))


class BertSink(Process):
    def __init__(self, args, front_sink_addr):
        super().__init__()
        self.port = args.port_out
        self.exit_flag = multiprocessing.Event()
        self.logger = set_logger(colored('SINK', 'green'))
        self.front_sink_addr = front_sink_addr

    def close(self):
        self.logger.info('shutting down...')
        self.exit_flag.set()
        self.terminate()
        self.join()
        self.logger.info('terminated!')

    def run(self):
        context = zmq.Context()
        # receive from workers
        receiver = context.socket(zmq.PULL)
        receiver_addr = _auto_bind(receiver)

        frontend = context.socket(zmq.PAIR)
        frontend.connect(self.front_sink_addr)

        # publish to client
        sender = context.socket(zmq.PUB)
        sender.bind('tcp://*:%d' % self.port)

        pending_checksum = defaultdict(int)
        pending_result = defaultdict(list)
        job_checksum = {}

        poller = zmq.Poller()
        poller.register(frontend, zmq.POLLIN)
        poller.register(receiver, zmq.POLLIN)

        # send worker receiver address back to frontend
        frontend.send(receiver_addr.encode('ascii'))

        try:
            while not self.exit_flag.is_set():
                socks = dict(poller.poll())
                if socks.get(receiver) == zmq.POLLIN:
                    msg = receiver.recv_multipart()
                    job_id = msg[0]
                    # parsing the ndarray
                    arr_info, arr_val = jsonapi.loads(msg[1]), msg[2]
                    X = np.frombuffer(memoryview(arr_val), dtype=arr_info['dtype'])
                    X = X.reshape(arr_info['shape'])
                    job_info = job_id.split(b'@')
                    job_id = job_info[0]
                    partial_id = job_info[1] if len(job_info) == 2 else 0
                    pending_result[job_id].append((X, partial_id))
                    pending_checksum[job_id] += X.shape[0]
                    self.logger.info('collect job %s (%d/%d)' % (job_id,
                                                                 pending_checksum[job_id],
                                                                 job_checksum[job_id]))

                    # check if there are finished jobs, send it back to workers
                    finished = [(k, v) for k, v in pending_result.items() if pending_checksum[k] == job_checksum[k]]
                    for job_info, tmp in finished:
                        self.logger.info(
                            'send back\tsize: %d\tjob id:%s\t' % (
                                job_checksum[job_info], job_info))
                        # re-sort to the original order
                        tmp = [x[0] for x in sorted(tmp, key=lambda x: int(x[1]))]
                        client_addr, req_id = job_info.split(b'#')
                        send_ndarray(sender, client_addr, np.concatenate(tmp, axis=0), req_id)
                        pending_result.pop(job_info)
                        pending_checksum.pop(job_info)
                        job_checksum.pop(job_info)

                if socks.get(frontend) == zmq.POLLIN:
                    client_addr, msg_type, msg_info, req_id = frontend.recv_multipart()
                    if msg_type == ServerCommand.new_job:
                        job_info = client_addr + b'#' + req_id
                        job_checksum[job_info] = int(msg_info)
                        self.logger.info('job register\tsize: %d\tjob id: %s' % (int(msg_info), job_info))
                    elif msg_type == ServerCommand.show_config:
                        time.sleep(0.1)  # dirty fix of slow-joiner: sleep so that client receiver can connect.
                        self.logger.info('send config\tclient %s' % client_addr)
                        sender.send_multipart([client_addr, msg_info, req_id])
        except zmq.error.ContextTerminated:
            self.logger.error('context is closed!')


class BertWorker(Process):
    def __init__(self, id, args, worker_address, sink_address, device_id):
        super().__init__()
        self.worker_id = id
        self.device_id = device_id
        self.logger = set_logger(colored('WORKER-%d' % self.worker_id, 'yellow'))
        self.tokenizer = tokenization.FullTokenizer(vocab_file=os.path.join(args.model_dir, 'vocab.txt'))
        self.max_seq_len = args.max_seq_len
        self.daemon = True
        self.exit_flag = multiprocessing.Event()
        self.worker_address = worker_address
        self.sink_address = sink_address
        self.prefetch_factor = 10
        self.gpu_memory_fraction = args.gpu_memory_fraction

    def close(self):
        self.logger.info('shutting down...')
        self.exit_flag.set()
        self.terminate()
        self.join()
        self.logger.info('terminated!')

    def get_estimator(self):
        os.environ['CUDA_VISIBLE_DEVICES'] = str(self.device_id)
        config = tf.ConfigProto(device_count={'GPU': 0 if self.device_id < 0 else 1})
        # session-wise XLA doesn't seem to work on tf 1.10
        # if args.xla:
        #     config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
        config.gpu_options.allow_growth = True
        config.gpu_options.per_process_gpu_memory_fraction = self.gpu_memory_fraction
        config.log_device_placement = True
        return Estimator(self.model_fn, config=RunConfig(session_config=config))

    def run(self):
        estimator = self.get_estimator()
        context = zmq.Context()
        receiver = context.socket(zmq.PULL)
        receiver.connect(self.worker_address)

        sink = context.socket(zmq.PUSH)
        sink.connect(self.sink_address)
        self.logger.info('estimator is built')

        for r in estimator.predict(self.input_fn_builder(receiver), yield_single_examples=False):
            send_ndarray(sink, r['client_id'], r['encodes'])
            self.logger.info('job done\tsize: %s\tclient: %s' % (r['encodes'].shape, r['client_id']))

        receiver.close()
        sink.close()
        context.term()
        self.logger.info('terminated!')

    def input_fn_builder(self, worker):
        def gen():
            while True:
                time.sleep(1)
                yield {
                    'client_id': 'test',
                    'input_ids': [[0] * self.max_seq_len],
                    'input_mask': [[1] * self.max_seq_len],
                    'input_type_ids': [[0] * self.max_seq_len]
                }
            # self.logger.info('ready and listening!')
            # while not self.exit_flag.is_set():
            #     client_id, msg = worker.recv_multipart()
            #     msg = jsonapi.loads(msg)
            #     self.logger.info('new job\tsize: %d\tclient: %s' % (len(msg), client_id))
            #     # check if msg is a list of list, if yes consider the input is already tokenized
            #     is_tokenized = all(isinstance(el, list) for el in msg)
            #     tmp_f = list(convert_lst_to_features(msg, self.max_seq_len, self.tokenizer, is_tokenized))
            #     yield {
            #         'client_id': client_id,
            #         'input_ids': [f.input_ids for f in tmp_f],
            #         'input_mask': [f.input_mask for f in tmp_f],
            #         'input_type_ids': [f.input_type_ids for f in tmp_f]
            #     }

        def input_fn():
            return (tf.data.Dataset.from_generator(
                gen,
                output_types={'input_ids': tf.int32,
                              'input_mask': tf.int32,
                              'input_type_ids': tf.int32,
                              'client_id': tf.string},
                output_shapes={
                    'client_id': (),
                    'input_ids': (None, self.max_seq_len),
                    'input_mask': (None, self.max_seq_len),
                    'input_type_ids': (None, self.max_seq_len)}).prefetch(self.prefetch_factor))

        return input_fn

    def model_fn(self, features, labels, mode, params):
        self.logger.info('loading graph...')
        with tf.gfile.GFile(_graph_tmp_file_, 'rb') as f:
            graph_def = tf.GraphDef()
            graph_def.ParseFromString(f.read())

        input_names = ['input_ids', 'input_mask', 'input_type_ids']

        output = tf.import_graph_def(graph_def,
                                     input_map={k + ':0': features[k] for k in input_names},
                                     return_elements=['final_encodes:0'])

        self.logger.info('graph is loaded')

        return EstimatorSpec(mode=mode, predictions={
            'client_id': features['client_id'],
            'encodes': output[0]
        })


def send_ndarray(src, dest, X, req_id=b'', flags=0, copy=True, track=False):
    """send a numpy array with metadata"""
    md = dict(dtype=str(X.dtype), shape=X.shape)
    return src.send_multipart([dest, jsonapi.dumps(md), X, req_id], flags, copy=copy, track=track)
