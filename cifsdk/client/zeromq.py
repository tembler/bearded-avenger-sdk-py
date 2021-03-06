import time
import json
from cifsdk.client import Client
from cifsdk.exceptions import AuthError, CIFConnectionError, TimeoutError, InvalidSearch
from cifsdk.constants import PYVERSION
import logging

from pprint import pprint

import zmq

SNDTIMEO = 90000
RCVTIMEO = 90000
LINGER = 3
ENCODING_DEFAULT = "utf-8"
SEARCH_LIMIT = 100
RETRIES = 5
RETRY_SLEEP = 5
FIREBALL_SIZE = 500

logger = logging.getLogger(__name__)


class ZMQ(Client):
    def __init__(self, remote, token, **kwargs):
        super(ZMQ, self).__init__(remote, token)

        self.context = zmq.Context.instance()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.RCVTIMEO = RCVTIMEO
        self.socket.SNDTIMEO = SNDTIMEO
        self.socket.setsockopt(zmq.LINGER, LINGER)
        self.nowait = kwargs.get('nowait', False)
        if self.nowait:
            self.socket = self.context.socket(zmq.DEALER)

        logger.debug('token: {}'.format(self.token))
        logger.debug('remote: {}'.format(self.remote))

    def _recv(self):
        mtype, data = self.socket.recv_multipart()
        data = json.loads(data.decode('utf-8'))

        if data.get('status') == 'success':
            return data.get('data')
        elif data.get('message') == 'unauthorized':
            raise AuthError('unauthorized')
        elif data.get('message') == 'invalid search':
            raise InvalidSearch('invalid search')
        else:
            logger.error(data.get('status'))
            logger.error(data.get('data'))
            raise RuntimeError(data.get('message'))

    def _send(self, mtype, data='[]', retries=RETRIES, timeout=SNDTIMEO, retry_sleep=RETRY_SLEEP, nowait=False):
        logger.debug('connecting to: %s' % self.remote)
        self.socket.connect(self.remote)

        if type(data) == str:
            data = data.encode('utf-8')

        sent = False
        while not sent and retries > 0:
            try:
                self.socket.send_multipart([self.token.encode(ENCODING_DEFAULT),
                                            mtype.encode(ENCODING_DEFAULT),
                                            data])
                sent = True
            except zmq.error.Again:
                logger.warning('timeout... retrying in 5s')
                retries -= 1
                time.sleep(retry_sleep)

        if not sent:
            m = 'unable to connect to remote: {}'.format(self.remote)
            logger.warn(m)
            raise TimeoutError(m)

        if self.nowait or nowait:
            logger.debug('not waiting for a resp')
        else:
            logger.debug("receiving")
            retries = RETRIES
            while retries > 0:
                try:
                    return self._recv()
                except zmq.error.Again:
                    logger.warn('timeout trying to receive, retrying...')
                    retries -= 1

            self.socket.close()
            raise TimeoutError('timeout waiting for: {}'.format(self.remote))

    def _handle_message_fireball(self, s, e):
        logger.debug('message recieved')
        m = s.recv_multipart()

        logger.debug(m)

        null, mtype, data = m

        data = json.loads(data.decode('utf-8'))

        self.response.append(data)

        self.num_responses -= 1
        logger.debug('num responses remaining: %i' % self.num_responses)
        if self.num_responses == 0:
            logger.debug('finishing up...')
            self.loop.stop()

        logger.debug('loop stopped')

    def _send_fireball_timeout(self):
        logger.warn('timeout')
        self.loop.stop()
        raise TimeoutError('timeout')

    def _send_fireball(self, mtype, data):
        if len(data) < 3:
            logger.error('no data to send')
            return []

        logger.debug('connecting to {0}'.format(self.remote))
        logger.debug("mtype {0}".format(mtype))
        self.socket = self.context.socket(zmq.DEALER)
        self.socket.connect(self.remote)

        from zmq.eventloop.ioloop import IOLoop
        self.loop = IOLoop.instance()
        timeout = time.time() + SNDTIMEO
        self.loop.add_timeout(timeout, self._send_fireball_timeout)
        self.response = []

        self.loop.add_handler(self.socket, self._handle_message_fireball, zmq.POLLIN)

        data = json.loads(data)

        if not isinstance(data, list):
            data = [data]

        if (len(data) / FIREBALL_SIZE) % FIREBALL_SIZE == 0:
            self.num_responses = (len(data) / FIREBALL_SIZE)
        else:
            self.num_responses = int((len(data) / FIREBALL_SIZE)) + 1

        logger.debug('responses: %i' % self.num_responses)

        batch = []
        for d in data:
            batch.append(d)
            if len(batch) == 1000:
                dd = json.dumps(batch)
                self.socket.send_multipart([b'', self.token.encode(ENCODING_DEFAULT),
                                            mtype.encode(ENCODING_DEFAULT),
                                            dd.encode(ENCODING_DEFAULT)])
                batch = []

        if len(batch):
            dd = json.dumps(batch)
            self.socket.send_multipart([b'', self.token.encode(ENCODING_DEFAULT),
                                        mtype.encode(ENCODING_DEFAULT),
                                        dd.encode(ENCODING_DEFAULT)])
        logger.debug("starting loop to receive")
        self.loop.start()
        self.socket.close()
        return self.response

    def test_connect(self):
        try:
            self.socket.RCVTIMEO = 5000
            self.ping()
            self.socket.RCVTIMEO = RCVTIMEO
        except zmq.error.Again:
            return False

        return True

    def ping(self, write=False):
        if write:
            return self._send('ping_write')
        else:
            return self._send('ping')

    def indicators_search(self, filters):
        rv = self._send('indicators_search', json.dumps(filters))
        return rv

    def indicators_create(self, data, nowait=False):
        if isinstance(data, dict):
            data = self._kv_to_indicator(data)

        if not isinstance(data, str):
            data = str(data)

        if self.fireball:
            logger.info('using fireball mode')
            data = self._send_fireball("indicators_create", data)
        else:
            data = self._send('indicators_create', data, nowait=nowait)

        return data

    def tokens_search(self, filters={}):
        return self._send('tokens_search', json.dumps(filters))

    def tokens_create(self, data):
        return self._send('tokens_create', data)

    def tokens_delete(self, data):
        return self._send('tokens_delete', data)

    def tokens_edit(self, data):
        return self._send('tokens_edit', data)

Plugin = ZMQ
