"""Test harness: deny external resolution/connections, permit owned loopback checks."""

import ipaddress
import socket


def install(monkeypatch):
    original_resolve, original_connect = socket.getaddrinfo, socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_sendmsg = getattr(socket.socket, "sendmsg", None)

    def local(host):
        if host is None or host in ("localhost", "", b"localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(address):
        if isinstance(address, tuple) and not local(address[0]):
            raise RuntimeError("external network disabled in offline tests")

    def resolve(host, *args, **kwargs):
        if not local(host):
            raise RuntimeError("external network disabled in offline tests")
        return original_resolve(host, *args, **kwargs)

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        if args:
            check(args[-1])
        return original_sendto(sock, data, *args)

    def sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
        if address is not None:
            check(address)
        return original_sendmsg(sock, buffers, ancdata, flags, address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    if original_sendmsg is not None:
        monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
