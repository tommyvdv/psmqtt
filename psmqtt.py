#!/usr/bin/env python

import os
import sys
import time
import socket
import logging
import logging.config
import sched
import json
from threading import Thread
from datetime import datetime

import paho.mqtt.client as paho  # pip install paho-mqtt
from recurrent import RecurringEvent  # pip install recurrent
from dateutil.rrule import *  # pip install python-dateutil

from handlers import handlers
from format import Formatter

try:
    from enum import IntEnum
except ImportError:
    class IntEnum:
        pass

# read initial config files
dirname = os.path.dirname(os.path.abspath(__file__)) + '/'
logging.config.fileConfig(dirname + 'logging.conf')
CONFIG = os.getenv('PSMQTTCONFIG', dirname + 'psmqtt.conf')


class Config(object):
    def __init__(self, filename=CONFIG):
        self.config = {}
        exec(compile(open(filename, "rb").read(), filename, 'exec'), self.config)

    def get(self, key, default=None):
        return self.config.get(key, default)


try:
    cf = Config()
except Exception as e:
    print("Cannot load configuration from file %s: %s" % (CONFIG, str(e)))
    sys.exit(2)

qos = cf.get('mqtt_qos', 0)
retain = cf.get('mqtt_retain', False)

topic_prefix = cf.get('mqtt_topic_prefix', 'psmqtt/' + socket.gethostname() + '/')
request_topic = cf.get('mqtt_request_topic', 'request')
if request_topic != '':
    request_topic = topic_prefix + request_topic + '/'

# fix for error 'No handlers could be found for logger "recurrent"'
reccurrent_logger = logging.getLogger('recurrent')
if len(reccurrent_logger.handlers) == 0:
    reccurrent_logger.addHandler(logging.NullHandler())


def run_task(task, topic):
    if task.startswith(topic_prefix):
        task = task[len(topic_prefix):]

    topic = Topic(topic if topic.startswith(topic_prefix) else topic_prefix + topic)
    try:
        payload = get_value(task)
        is_seq = isinstance(payload, list) or isinstance(payload, dict)
        if is_seq and not topic.is_multitopic():
            raise Exception("Result of task '" + task + "' has several values but topic doesn't contain '*' char")
        if isinstance(payload, list):
            for i, v in enumerate(payload):
                subtopic = topic.get_subtopic(str(i))
                mqttc.publish(subtopic, payload_as_string(v), qos=qos, retain=retain)
        elif isinstance(payload, dict):
            for key in payload:
                subtopic = topic.get_subtopic(str(key))
                v = payload[key]
                mqttc.publish(subtopic, payload_as_string(v), qos=qos, retain=retain)
        else:
            mqttc.publish(topic.get_topic(), payload_as_string(payload), qos=qos, retain=retain)
    except Exception as ex:
        mqttc.publish(topic.get_error_topic(), str(ex), qos=qos, retain=retain)
        logging.exception(task + ": " + str(ex))


def payload_as_string(v):
    if isinstance(v, dict):
        return json.dumps(v)
    elif isinstance(v, IntEnum):
        return v.value
    elif not isinstance(v, list):
        return str(v)
    elif len(v) == 1:  # single-element array should be presented as single value
        return payload_as_string(v[0])
    else:
        return json.dumps(v)


def get_value(path):
    path, _format = Formatter.get_format(path)
    head, tail = split(path)

    if head in handlers:
        value = handlers[head].handle(tail)
        if _format is not None:
            value = Formatter.format(_format, value)
        return value
    else:
        raise Exception("Element '" + head + "' in '" + path + "' is not supported")


class Topic:
    def __init__(self, topic):
        self.topic = topic
        self.wildcard_index, self.wildcard_len = self._find_wildcard(topic)

    @staticmethod
    def _find_wildcard(topic):
        start = 0
        # search for * or ** (but not *; or **;) outside of []
        while start < len(topic):
            wildcard_index = topic.find('*', start)
            if wildcard_index < 0:
                break
            bracket_index = topic.find('[', start)
            if 0 <= bracket_index < wildcard_index:
                start = topic.find(']', bracket_index)
                continue
            wildcard_len = 1
            if wildcard_index + 1 < len(topic) and topic[wildcard_index + 1] == '*':  # ** sequence
                wildcard_len += 1
            if wildcard_index + wildcard_len < len(topic) and topic[wildcard_index + wildcard_len] == ';':
                start = wildcard_index + wildcard_len
                continue
            return wildcard_index, wildcard_len
        return -1, -1

    def is_multitopic(self):
        return self.wildcard_index > 0

    def get_subtopic(self, param):
        if self.wildcard_index < 0:
            raise Exception("Topic " + self.topic + " have no wildcard")
        return self.topic[:self.wildcard_index] + param + self.topic[self.wildcard_index + self.wildcard_len:]

    def get_topic(self):
        return self.topic

    def get_error_topic(self):
        return self.topic + "/error"


# noinspection PyUnusedLocal
def on_message(mosq, userdata, msg):
    logging.debug(msg.topic + " " + str(msg.qos) + " " + str(msg.payload))

    if msg.topic.startswith(request_topic):
        task = msg.topic[len(request_topic):]
        run_task(task, task)
    else:
        logging.warn('Unknown topic: ' + msg.topic)


def on_timer(s, dt, tasks):
    if isinstance(tasks, dict):
        for k in tasks:
            run_task(k, tasks[k])
    elif isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                for k in task:
                    run_task(k, task[k])
            else:
                run_task(task, task)
    else:
        run_task(tasks, tasks)

    # add next timer task
    now = datetime.now()
    # need reparse rule (see #10)
    delay = (rrulestr(dt).after(now) - now).total_seconds()
    s.enter(delay, 1, on_timer, [s, dt, tasks])

# noinspection PyUnusedLocal
def on_connect(client, userdata, flags, result_code):
    publish_birth(client)
    if request_topic != '':
        topic = request_topic + '#'
        logging.debug("Connected to MQTT broker, subscribing to topic " + topic)
        mqttc.subscribe(topic, qos)


# noinspection PyUnusedLocal
def on_disconnect(mosq, userdata, rc):
    logging.debug("OOOOPS! psmqtt disconnects")
    time.sleep(10)


def publish_birth(client):
    topic = cf.get('mqtt_birth_topic', 'clients/psmqtt')
    payload = cf.get('mqtt_birth_payload', 'Ola!')
    qos = cf.get('mqtt_birth_qos', 0)
    retain = cf.get('mqtt_birth_retain', False)
    client.publish(topic, payload, qos, retain)


def set_last_will(client):
    topic = cf.get('mqtt_will_topic', 'clients/psmqtt')
    payload = cf.get('mqtt_will_payload', 'Adios!')
    qos = cf.get('mqtt_will_qos', 0)
    retain = cf.get('mqtt_will_retain', False)
    client.will_set(topic, payload, qos, retain)


def split(s):
    parts = s.split("/", 1)
    return parts if len(parts) == 2 else [parts[0], '']


class TimerThread(Thread):
    def __init__(self, s):
        Thread.__init__(self)
        self.s = s

    def run(self):
        self.s.run()


if __name__ == '__main__':
    clientid = cf.get('mqtt_clientid', 'psmqtt-%s' % os.getpid())
    # initialise MQTT broker connection
    mqttc = paho.Client(clientid, clean_session=cf.get('mqtt_clean_session', False))

    mqttc.on_message = on_message
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    set_last_will(mqttc)

    # Delays will be: 3, 6, 12, 24, 30, 30, ...
    # mqttc.reconnect_delay_set(delay=3, delay_max=30, exponential_backoff=True)

    mqttc.username_pw_set(cf.get('mqtt_username'), cf.get('mqtt_password'))

    mqtt_port = int(cf.get('mqtt_port', '1883'))
    ssl = (mqtt_port == 8883)
    if ssl:
        mqttc.tls_set(ca_certs=None, certfile=None, keyfile=None, cert_reqs=paho.ssl.CERT_REQUIRED, tls_version=paho.ssl.PROTOCOL_TLS, ciphers=None)
    
    mqttc.connect(cf.get('mqtt_broker', 'localhost'), mqtt_port, 60)

    # parse schedule
    schedule = cf.get('schedule', {})
    s = sched.scheduler(time.time, time.sleep)
    now = datetime.now()
    for t in schedule:
        r = RecurringEvent()
        dt = r.parse(t)
        if not r.is_recurring:
            logging.error(t + " is not recurring time. Skipping")
            continue
        delay = (rrulestr(dt).after(now) - now).total_seconds()
        s.enter(delay, 1, on_timer, [s, dt, schedule[t]])

    tt = TimerThread(s)
    tt.daemon = True
    tt.start()

    while True:
        try:
            mqttc.loop_forever()
        except socket.error:
            time.sleep(5)
        except KeyboardInterrupt:
            sys.exit(0)
