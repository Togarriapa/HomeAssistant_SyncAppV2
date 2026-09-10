"""Private standalone Core helper; run only in a fresh isolated Python subprocess.

No Home Assistant or candidate import occurs before the sandbox is installed.
Linux Landlock ABI >=3 and libseccomp are mandatory, never optional fallbacks.
"""

from __future__ import annotations

import ctypes
import errno
import importlib.metadata
import json
import os
import platform
import resource
import runpy
import sys
from pathlib import Path


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _Compare(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_uint),
        ("a", ctypes.c_uint64),
        ("b", ctypes.c_uint64),
    ]


def _filesystem_sandbox(libc: ctypes.CDLL, config: Path) -> None:
    # asm-generic and x86_64 share the assigned Landlock syscall numbers.
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("unsupported platform")
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(444, 0, 0, 1) < 3:
        raise RuntimeError("Landlock ABI 3 is required")
    # All filesystem rights through ABI 3, including REFER and TRUNCATE, handled by default.
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = libc.syscall(444, ctypes.byref(handled), ctypes.sizeof(handled), 0)
    if ruleset < 0:
        raise RuntimeError("Landlock unavailable")
    read_file, read_dir = 1 << 2, 1 << 3
    # Allow read-only interpreter/libraries/timezone/certificates, never /data or /homeassistant.
    rules = [(Path(p), read_file | read_dir) for p in ("/usr", "/lib", "/lib64", "/etc/ssl")]
    rules += [
        (Path(p), read_file)
        for p in ("/etc/localtime", "/etc/passwd", "/etc/mime.types", "/dev/urandom")
    ]
    # Disposable candidate copy only: regular files/directories, not sockets/devices/symlinks.
    writable = read_file | read_dir | (1 << 1) | (1 << 4) | (1 << 5)
    writable |= (1 << 7) | (1 << 8) | (1 << 13) | (1 << 14)
    rules += [(config.parent, writable), (Path("/dev/null"), read_file | (1 << 1))]
    try:
        for path, access in rules:
            if not path.exists():
                continue
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(access, descriptor)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0) != 0:
                    raise RuntimeError("Landlock rule failed")
            finally:
                os.close(descriptor)
        if libc.syscall(446, ruleset, 0) != 0:
            raise RuntimeError("Landlock enforcement failed")
    finally:
        os.close(ruleset)


def _syscall_sandbox() -> None:
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_Compare),
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; libseccomp checks native arch.
    if not context:
        raise RuntimeError("seccomp unavailable")
    denied = 0x00050000 | errno.EPERM
    try:
        names = (
            "execve",
            "execveat",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "setsid",
            "setpgid",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
            "open_by_handle_at",
            "kill",
            "tkill",
            "tgkill",
            "pidfd_send_signal",
            "sendmsg",
            "sendmmsg",
        )
        for name in names:
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number == -1 or seccomp.seccomp_rule_add_array(context, denied, number, 0, None):
                raise RuntimeError("seccomp rule failed")
        # asyncio needs unnamed AF_UNIX socket pairs. All network socket families are denied.
        for socket_call in (b"socket", b"socketpair"):
            number = seccomp.seccomp_syscall_resolve_name(socket_call)
            compare = _Compare(0, 1, 1, 0)  # SCMP_CMP_NE, AF_UNIX == 1
            if seccomp.seccomp_rule_add_array(context, denied, number, 1, ctypes.byref(compare)):
                raise RuntimeError("seccomp socket rule failed")
        # Unconnected Unix datagrams must not reach external/abstract Unix endpoints.
        # send() uses sendto with a null destination and is needed by asyncio's wakeup pair.
        compare = _Compare(4, 1, 0, 0)
        if seccomp.seccomp_rule_add_array(
            context,
            denied,
            seccomp.seccomp_syscall_resolve_name(b"sendto"),
            1,
            ctypes.byref(compare),
        ):
            raise RuntimeError("seccomp datagram rule failed")
        if seccomp.seccomp_load(context):
            raise RuntimeError("seccomp enforcement failed")
    finally:
        seccomp.seccomp_release(context)


def _sandbox(config: Path) -> None:
    os.umask(0o077)
    limits = (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, 90),
        (resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, 16 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 512),
        (resource.RLIMIT_NPROC, 256),
    )
    for kind, limit in limits:
        _, hard = resource.getrlimit(kind)
        value = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(kind, (value, value))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    if os.geteuid() == 0 or os.getuid() == 0:
        raise RuntimeError("privilege drop failed")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise RuntimeError("privilege restriction failed")
    _filesystem_sandbox(libc, config)
    _syscall_sandbox()
    os.chdir(config)


def _check(config: Path, version: str) -> str:
    _sandbox(config)
    if importlib.metadata.version("homeassistant") != version:
        return "version_mismatch"
    sys.argv = [
        "homeassistant",
        "--script",
        "check_config",
        "--config",
        str(config),
        "--fail-on-warnings",
    ]
    try:
        runpy.run_module("homeassistant", run_name="__main__")
    except SystemExit as exc:
        if type(exc.code) is int and exc.code == 0:
            return "passed"
        if type(exc.code) is int and exc.code == 1:
            return "invalid"
    return "ambiguous"


def main() -> None:
    if len(sys.argv) != 4:
        return
    config, version, nonce = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    # Suppress at the descriptor layer, including C-extension output and raw checker errors.
    receipt = os.dup(1)
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
    try:
        result = _check(config, version)
    except BaseException:
        result = "unavailable"
    # Core/atexit shutdown output remains suppressed; only this fixed receipt is observable.
    payload = json.dumps({"nonce": nonce, "version": version, "result": result}).encode()
    os.write(receipt, payload)
    os.close(receipt)


if __name__ == "__main__":
    main()
