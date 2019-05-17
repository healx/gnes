#  Tencent is pleased to support the open source community by making GNES available.
#
#  Copyright (C) 2019 THL A29 Limited, a Tencent company. All rights reserved.
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# pylint: disable=low-comment-ratio

from concurrent import futures
import contextlib

import threading
import multiprocessing
import time
import socket
import grpc

import ctypes
import uuid
from typing import List, Optional
import datetime

import zmq

from gnes.proto import gnes_pb2, gnes_pb2_grpc
from ..messaging import send_message, recv_message
from ..helper import set_logger

_ONE_DAY = datetime.timedelta(days=1)
_PROCESS_COUNT = multiprocessing.cpu_count()
_THREAD_CONCURRENCY = _PROCESS_COUNT

LOGGER = set_logger(__name__)


class ZmqContext(object):
    """The zmq context class."""

    def __init__(self, args):
        """Database connection context.

        Args:
            servers: a list of config dicts for connecting to database
            dbapi_name: the name of database engine
        """
        self.args = args

        self.tlocal = threading.local()
        self.tlocal.client = None

    def __enter__(self):
        """Enter the context."""
        client = ZmqClient(self.args)
        self.tlocal.client = client
        return client

    def __exit__(self, exc_type, exc_value, exc_traceback):
        """Exit the context."""
        self.tlocal.client.close()
        self.tlocal.client = None

        if exc_type is not None:
            raise


class ZmqClient:

    def __init__(self, args):
        self.args = args
        self.host_in = args.host_in
        self.host_out = args.host_out
        self.port_in = args.port_in
        self.port_out = args.port_out
        # identity: str =None,
        self.context = zmq.Context()
        self.sender = self.context.socket(zmq.PUSH)
        self.sender.connect('tcp://%s:%d' % (self.host_out, self.port_out))

        self.identity = str(uuid.uuid4()).encode('ascii')
        self.logger = set_logger(self.__class__.__name__ + ':%s' % self.identity, self.args.verbose)
        self.receiver = self.context.socket(zmq.SUB)
        self.receiver.setsockopt(zmq.SUBSCRIBE, self.identity)
        self.receiver.connect('tcp://%s:%d' % (self.host_in, self.port_in))

    def close(self):
        self.sender.close()
        self.receiver.close()
        self.context.term()

    def send_message(self, message: "gnes_pb2.Message", timeout: int = -1):
        message.client_id = self.identity
        self.logger.info('send message: %s' % message.client_id)
        send_message(self.sender, message, timeout=timeout)

    def recv_message(self, timeout: int = -1) -> gnes_pb2.Message:
        msg = recv_message(self.receiver, timeout=timeout)
        return msg


class GNESServicer(gnes_pb2_grpc.GnesServicer):

    def __init__(self, args):
        self.args = args
        self.logger = set_logger(self.__class__.__name__, self.args.verbose)
        self.zmq_context = ZmqContext(args)

    def Index(self, request, context):
        #req_id = str(uuid.uuid4())
        req_id = request._request_id if request._request_id else str(
            uuid.uuid4())
        self.logger.info('index request: %s received' % req_id)
        message = gnes_pb2.Message()
        message.client_id = req_id
        message.msg_id = req_id
        message.num_part = 1
        message.part_id = 1
        if request.update_model:
            message.mode = gnes_pb2.Message.TRAIN
            if not request.send_more:
                message.command = gnes_pb2.Message.TRAIN_ENCODER
                # self.logger.info("cmd message received: %s" % str(message))
        else:
            message.mode = gnes_pb2.Message.INDEX
        message.docs.extend(request.docs)

        message.route = self.__class__.__name__
        message.is_parsed = True

        with self.zmq_context as zmq_client:
            zmq_client.send_message(message, self.args.timeout)
            # result = zmq_client.recv_message()
            # print(result)
        return gnes_pb2.IndexResponse()

        # process result message and build response proto

    def Search(self, request, context):
        # context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        # context.set_details('Method not implemented!')
        # raise NotImplementedError('Method not implemented!')

        req_id = request._request_id if request._request_id else str(
            uuid.uuid4())
        self.logger.info('index request: %s received' % req_id)
        message = gnes_pb2.Message()
        message.client_id = req_id
        message.msg_id = req_id
        message.num_part = 1
        message.part_id = 1
        message.mode = gnes_pb2.Message.QUERY
        message.docs.extend([request.doc])

        chunks = request.doc.text_chunks if len(
            request.doc.text_chunks) > 0 else request.doc.blob_chunks

        for i, chunk in enumerate(chunks):
            q = message.querys.add()
            q.id = i
            q.text = chunk
            q.top_k = request.top_k

        message.route = self.__class__.__name__
        message.is_parsed = True

        with self.zmq_context as zmq_client:
            # message.client_id = zmq_client.identity
            zmq_client.send_message(message, self.args.timeout)
            #print('send request message: ' + str(message))
            result = zmq_client.recv_message()

            response = gnes_pb2.SearchResponse()
            response.querys.extend(result.querys)

            try:
                for _ in range(len(result.querys[0].results)):
                    print(result.querys[0].results[_].chunk.text)
            except:
                print('error', line, result)

            return response


def _wait_forever(server):
    try:
        while True:
            time.sleep(_ONE_DAY.total_seconds())
    except KeyboardInterrupt:
        server.stop(None)


@contextlib.contextmanager
def _reserve_port():
    """Find and reserve a port for all subprocesses to use."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) != 1:
        raise RuntimeError("Failed to set SO_REUSEPORT.")
    sock.bind(('', 0))
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _run_server(bind_address, args):
    """Start a server in a subprocess."""
    options = (('grpc.so_reuseport', 1),)

    # WARNING: This example takes advantage of SO_REUSEPORT. Due to the
    # limitations of manylinux1, none of our precompiled Linux wheels currently
    # support this option. (https://github.com/grpc/grpc/issues/18210). To take
    # advantage of this feature, install from source with
    # `pip install grpcio --no-binary grpcio`.

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=_THREAD_CONCURRENCY,),
        options=options)
    gnes_pb2_grpc.add_GnesServicer_to_server(GRPCServicer(args), server)
    server.add_insecure_port(bind_address)
    server.start()
    _wait_forever(server)


def start_serve(args):
    bind_address = '{0}:{1}'.format(args.grpc_host, args.grpc_port)
    LOGGER.info('start grpc service at: %s' % bind_address)
    process_count = _PROCESS_COUNT if not args.num_procs else args.num_procs
    workers = []
    for _ in range(process_count):
        # NOTE: It is imperative that the worker subprocesses be forked before
        # any gRPC servers start up. See
        # https://github.com/grpc/grpc/issues/16001 for more details.
        worker = multiprocessing.Process(
            target=_run_server, args=(
                bind_address,
                args,
            ))
        worker.start()
        workers.append(worker)
        LOGGER.info('[%d] grpc service started' % _)

    for worker in workers:
        worker.join()


def serve(args):
    # Initialize GRPC Server
    LOGGER.info("start grpc server with %d workers ..." % _THREAD_CONCURRENCY)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=_THREAD_CONCURRENCY))

    # Initialize Services
    gnes_pb2_grpc.add_GnesServicer_to_server(GNESServicer(args), server)

    # Start GRPC Server
    bind_address = '{0}:{1}'.format(args.grpc_host, args.grpc_port)
    # server.add_insecure_port('[::]:' + "5555")
    server.add_insecure_port(bind_address)
    server.start()
    LOGGER.info("grpc service is listening at: %s" % bind_address)

    # Keep application alive
    _wait_forever(server)
