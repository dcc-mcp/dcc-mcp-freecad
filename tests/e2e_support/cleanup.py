"""Close only the exact API/process handles created by the real test."""

from __future__ import annotations

import subprocess


def close_owned(server, handle, children):
    handle = handle or getattr(server, "_handle", None)
    cleanup, terminals = {}, []
    for name, action in (
        ("signal_shutdown", handle.signal_shutdown if handle else None),
        ("raw_shutdown", handle.shutdown if handle else None),
        ("facade_stop", server.stop if server else None),
    ):
        try:
            if action:
                action()
            cleanup[name] = action is not None
        except BaseException:
            cleanup[name] = False
    for method, process in children:
        item = {"method": method, "returncode": None, "cleanup_termination": False}
        try:
            item["cleanup_termination"] = process.poll() is None
            if item["cleanup_termination"]:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            item["returncode"] = process.wait(timeout=5)
        except BaseException as error:
            item["cleanup_error_type"] = type(error).__name__
        terminals.append(item)
    try:
        cleanup["facade_stopped"] = server is not None and not server.is_running
    except BaseException:
        cleanup["facade_stopped"] = False
    return cleanup, terminals
