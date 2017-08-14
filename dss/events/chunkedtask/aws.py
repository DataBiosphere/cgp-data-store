import json
import logging
import os
import typing
import uuid

import boto3

from . import _awsimpl, awsconstants
from .runner import Runner

# this is the authoritative mapping between client names and Task classes.
CLIENTS = {
    _awsimpl.AWS_FAST_TEST_CLIENT_NAME: _awsimpl.AWSFastTestTask,
}

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def schedule_task(client_name: str, state: typing.Any):
    task_id = str(uuid.uuid4())

    payload = {
        awsconstants.CLIENT_KEY: client_name,
        awsconstants.REQUEST_VERSION_KEY: awsconstants.CURRENT_VERSION,
        awsconstants.TASK_ID_KEY: task_id,
        awsconstants.STATE_KEY: state,
    }

    sts_client = boto3.client("sts")
    accountid = sts_client.get_caller_identity()['Account']

    sns_client = boto3.client("sns")
    region = boto3.Session().region_name
    topic = awsconstants.get_worker_sns_topic()
    arn = f"arn:aws:sns:{region}:{accountid}:{topic}"
    sns_client.publish(
        TopicArn=arn,
        Message=json.dumps(payload),
    )

    _awsimpl.AWSRuntime.log(
        task_id,
        json.dumps(dict(
            action=awsconstants.LogActions.SCHEDULED,
            payload=payload,
        )),
    )

    return task_id


def get_payload(payload):
    try:
        task_id = payload[awsconstants.TASK_ID_KEY]
    except KeyError as ex:
        _awsimpl.AWSRuntime.log(
            awsconstants.FALLBACK_LOG_STREAM_NAME,
            json.dumps(dict(
                action=awsconstants.LogActions.EXCEPTION,
                message="Could not find task_id",
                payload=payload,
                exception=str(ex),
            )),
        )
        return None

    # look up by client name
    try:
        client_name = payload[awsconstants.CLIENT_KEY]
        client_class = CLIENTS[client_name]
        version = payload[awsconstants.REQUEST_VERSION_KEY]
        state = payload[awsconstants.STATE_KEY]
    except KeyError as ex:
        _awsimpl.AWSRuntime.log(
            task_id,
            json.dumps(dict(
                action=awsconstants.LogActions.EXCEPTION,
                message="Request payload missing required data",
                payload=payload,
                exception=str(ex),
            )),
        )
        return None

    if version < awsconstants.MIN_SUPPORTED_VERSION or version > awsconstants.MAX_SUPPORTED_VERSION:
        _awsimpl.AWSRuntime.log(
            task_id,
            json.dumps(dict(
                action=awsconstants.LogActions.EXCEPTION,
                message="Message version not supported",
                payload=payload,
            )),
        )
        return None

    return task_id, client_name, client_class, state


def dispatch(context, payload):
    decoded_payload = get_payload(payload)
    if decoded_payload is None:
        return
    task_id, client_name, client_class, state = decoded_payload

    # special case: if the client name is `AWS_FAST_TEST_CLIENT_NAME`, we use a special runtime environment so we don't
    # take forever running the test.
    if client_name == _awsimpl.AWS_FAST_TEST_CLIENT_NAME:
        runtime = _awsimpl.AWSFastTestRuntime(context, task_id)
    else:
        runtime = _awsimpl.AWSRuntime(context, client_name, task_id)

    task = client_class(state)

    runner = Runner(task, runtime)
    runner.run()