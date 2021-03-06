import gzip
import ipaddress
import json
import logging
import socket
from typing import Optional, Mapping, Any, NamedTuple
from urllib.parse import urlparse

import base64
import time

import requests
from requests_http_signature import HTTPSignatureAuth
import urllib3

from dss import DeploymentStage, Config
from dss.util import require
from dss.util.types import JSON

logger = logging.getLogger(__name__)

attempt_header_name = 'X-dss-notify-attempt'


class Notification(NamedTuple):
    notification_id: str
    subscription_id: str
    url: str
    method: str
    encoding: str
    body: str
    attempts: Optional[int] = None
    hmac_key: Optional[str] = None
    hmac_key_id: Optional[str] = None
    queued_at: Optional[float] = None
    correlation_id: Optional[str] = None

    @classmethod
    def create(cls,
               notification_id: str,
               subscription_id: str,
               url: str,
               method: str,
               encoding: str,
               body: JSON,
               attempts: Optional[int] = None,
               hmac_key: Optional[bytes] = None,
               hmac_key_id: Optional[str] = None,
               correlation_id: Optional[str] = None) -> 'Notification':

        allowed_schemes = {'https'} if DeploymentStage.IS_PROD() else {'https', 'http'}
        scheme = urlparse(url).scheme
        require(scheme in allowed_schemes,
                f"The scheme '{scheme}' of URL '{url}' is prohibited. Allowed schemes are {allowed_schemes}.")

        if DeploymentStage.IS_PROD():
            hostname = urlparse(url).hostname
            for family, socktype, proto, canonname, sockaddr in socket.getaddrinfo(hostname, port=None):
                require(ipaddress.ip_address(sockaddr[0]).is_global,
                        f"The hostname in URL '{url}' resolves to a private IP")

        if attempts is None:
            attempts = Config.notification_attempts()

        return cls(notification_id=notification_id,
                   subscription_id=subscription_id,
                   url=url,
                   method=method,
                   encoding=encoding,
                   body=cls._bin2sqs(json.dumps(body).encode()),
                   attempts=attempts,
                   hmac_key=None if hmac_key is None else cls._bin2sqs(hmac_key),
                   hmac_key_id=hmac_key_id,
                   queued_at=None,  # this field will be set when the message comes out of the queue
                   correlation_id=correlation_id)

    @classmethod
    def from_sqs_message(cls, message) -> 'Notification':
        def v(d):
            return None if d is None else d['StringValue']

        attributes = message.attributes
        message_attributes = message.message_attributes
        return cls(notification_id=attributes['MessageDeduplicationId'],
                   subscription_id=attributes['MessageGroupId'],
                   url=v(message_attributes.get('url')),
                   method=v(message_attributes.get('method')),
                   encoding=v(message_attributes.get('encoding')),
                   body=message.body,
                   attempts=int(v(message_attributes.get('attempts'))),
                   hmac_key=v(message_attributes.get('hmac_key')),
                   hmac_key_id=v(message_attributes.get('hmac_key_id')),
                   queued_at=float(v(message_attributes.get('queued_at'))),
                   correlation_id=v(message_attributes.get('correlation_id')))

    def to_sqs_message(self):
        assert self.attempts is not None

        # Boto3's receive_messages returns Message instances while send_message() expects keyword arguments.
        def v(s):
            return None if s is None else dict(StringValue=s, DataType='String')

        # Removing the entries with a None value is more concise than conditionally adding them
        def f(d):
            return {k: v for k, v in d.items() if v is not None}

        return dict(MessageBody=self.body,
                    MessageDeduplicationId=self.notification_id,
                    MessageGroupId=self.subscription_id,
                    MessageAttributes=f(dict(url=v(self.url),
                                             method=v(self.method),
                                             encoding=v(self.encoding),
                                             attempts=v(str(self.attempts)),
                                             hmac_key=v(self.hmac_key),
                                             hmac_key_id=v(self.hmac_key_id),
                                             queued_at=v(str(time.time())),
                                             correlation_id=v(self.correlation_id))))

    def deliver_or_raise(self, timeout: Optional[float] = None, attempt: Optional[int] = None):
        request = self._prepare_request(timeout, attempt)
        response = requests.request(**request)
        response.raise_for_status()

    def deliver(self, timeout: Optional[float] = None, attempt: Optional[int] = None) -> bool:
        request = self._prepare_request(timeout, attempt)
        try:
            response = requests.request(**request)
        except BaseException as e:
            logger.warning("Exception raised while delivering %s:", self, exc_info=e)
            return False
        else:
            if 200 <= response.status_code < 300:
                logger.info("Successfully delivered %s: HTTP status %i, response time %.3f",
                            self, response.status_code, response.elapsed.total_seconds())
                return True
            else:
                logger.warning("Failed delivering %s: HTTP status %i, response time %.3f",
                               self, response.status_code, response.elapsed.total_seconds())
                return False

    def _prepare_request(self, timeout, attempt) -> Mapping[str, Any]:
        if self.hmac_key:
            auth = HTTPSignatureAuth(key=self._sqs2bin(self.hmac_key), key_id=self.hmac_key_id)
        else:
            auth = None
        headers = {}
        if attempt is not None:
            headers[attempt_header_name] = str(attempt)
        request = dict(method=self.method,
                       url=self.url,
                       auth=auth,
                       allow_redirects=False,
                       headers=headers,
                       timeout=timeout)
        body = json.loads(self._sqs2bin(self.body))
        if self.encoding == 'application/json':
            request['json'] = body
        elif self.encoding == 'multipart/form-data':
            # The requests.request() method can encode this content type for us (using the files= keyword argument)
            # but it is awkward to use if the field values are strings or bytes and not streams.
            data, content_type = urllib3.encode_multipart_formdata(body)
            request['data'] = data
            request['headers']['Content-Type'] = content_type
        else:
            raise ValueError(f'Encoding {self.encoding} is not supported')
        return request

    def spend_attempt(self):
        assert self.attempts > 0
        return self._replace(attempts=self.attempts - 1)

    @classmethod
    def _bin2sqs(cls, s: bytes):
        # SQS supports #x9 | #xA | #xD | #x20 to #xD7FF | #xE000 to #xFFFD | #x10000 to #x10FFFF in message bodies.
        # The base85 alphabet is a subset of that and of ASCII. It is more space efficient than base64.
        return base64.b85encode(gzip.compress(s)).decode('ascii')

    @classmethod
    def _sqs2bin(cls, s: str):
        return gzip.decompress(base64.b85decode(s.encode('ascii')))

    def __str__(self) -> str:
        # Don't log body because it may be too big or the HMAC key because it is secret
        return (f"{self.__class__.__name__}("
                f"notification_id='{self.notification_id}', "
                f"subscription_id='{self.subscription_id}', "
                f"url='{self.url}', "
                f"method='{self.method}', "
                f"encoding='{self.encoding}', "
                f"attempts={self.attempts}, "
                f"hmac_key_id='{self.hmac_key_id}', "
                f"correlation_id='{self.correlation_id}')")


class Endpoint(NamedTuple):
    """
    A convenience holder for the subscription attributes that describe the HTTP endpoint to send notifications to.

    Be sure to keep the defaults in sync with the Swagger API definition.
    """
    callback_url: Optional[str]
    method: str = 'POST'
    encoding: str = 'application/json'
    form_fields: Mapping[str, str] = {}  # noqa, False positive: E701 multiple statements on one line (colon)
    payload_form_field: str = 'payload'

    @classmethod
    def from_subscription(cls, subscription):
        return Endpoint(**{k: subscription[k] for k in cls._fields if k in subscription})

    def to_dict(self):
        return self._asdict()

    def extend(self, **kwargs):
        return self._replace(**kwargs)
