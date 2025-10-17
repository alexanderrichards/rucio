import logging
import socket
from collections.abc import Callable, Sequence
from typing import Any, Optional

import stomp


class StompController:
    """
    Common controller for managing Stomp connections to brokers.
    """

    def __init__(
        self,
        brokers: Sequence[str],
        port: int,
        use_ssl: bool = True,
        vhost: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ssl_key_file: Optional[str] = None,
        ssl_cert_file: Optional[str] = None,
        timeout: Optional[int] = None,
        reconnect_attempts: int = 999,
        logger: Callable = logging.log,
    ):
        self.logger = logger
        self.brokers = brokers
        self.port = port
        self.use_ssl = use_ssl
        self.vhost = vhost
        self.username = username
        self.password = password
        self.ssl_key_file = ssl_key_file
        self.ssl_cert_file = ssl_cert_file
        self.timeout = timeout
        self.reconnect_attempts = reconnect_attempts
        self.connections = []

    def resolve_brokers(self) -> list[str]:
        resolved = []
        for broker in self.brokers:
            try:
                addrinfos = socket.getaddrinfo(broker, 0, socket.AF_INET, 0, socket.IPPROTO_TCP)
                resolved.extend(ai[4][0] for ai in addrinfos)
            except socket.gaierror as ex:
                self.logger(logging.ERROR, f"Cannot resolve domain name {broker} ({ex})")
        return resolved

    def setup_connections(self) -> None:
        resolved_brokers = self.resolve_brokers()
        self.logger(logging.INFO, f"Brokers resolved to {resolved_brokers}")
        for broker in resolved_brokers:
            con = stomp.Connection12(
                host_and_ports=[(broker, self.port)],
                vhost=self.vhost,
                keepalive=True,
                timeout=self.timeout,
                reconnect_attempts_max=self.reconnect_attempts,
            )
            if self.use_ssl and self.ssl_key_file and self.ssl_cert_file:
                con.set_ssl(key_file=self.ssl_key_file, cert_file=self.ssl_cert_file)
            self.connections.append(con)

    def connect_and_subscribe(
        self,
        destination: str,
        listener: Any,
        subscription_id: str = "rucio-subscription",
        ack: str = "auto",
        headers: Optional[dict] = None,
    ) -> None:
        for con in self.connections:
            if not con.is_connected():
                con.set_listener(subscription_id, listener)
                if self.use_ssl:
                    con.connect(wait=True)
                else:
                    con.connect(self.username, self.password, wait=True)
                con.subscribe(destination=destination, ack=ack, id=subscription_id, headers=headers or {})

    def disconnect(self) -> None:
        for con in self.connections:
            try:
                con.disconnect()
            except Exception:
                pass