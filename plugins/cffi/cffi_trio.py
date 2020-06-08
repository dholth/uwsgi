"""
Direct port of plugins/asyncio.c
"""

import os
import sys
import trio
import inspect
import greenlet

from _uwsgi import ffi, lib, _applications

uwsgi = lib.uwsgi

wsgi_apps = _applications


def get_loop():
    return asyncio.get_event_loop()


# testing
def request_id():
    return (
        lib.uwsgi.workers[lib.uwsgi.mywid].cores[lib.uwsgi.wsgi_req.async_id].requests
    )


# keep alive
_TRIO = ffi.new("char[]", "trio".encode("utf-8"))

timeout_handles = {}  # or array with # of cores?

ob_timeout_counter = 0


def store_ob_timeout(wsgi_req, ob_timeout):
    global ob_timeout_counter
    # TODO check if wsgi_req.async_id is consistent enough for us
    ob_timeout_counter += 1
    wsgi_req.async_timeout = ffi.cast("struct uwsgi_rb_timer *", ob_timeout_counter)
    timeout_handles[ob_timeout_counter] = ob_timeout


def get_ob_timeout(wsgi_req):
    ob_timeout_ = ffi.cast("ssize_t", wsgi_req.async_timeout)
    wsgi_req.async_timeout = ffi.NULL
    return timeout_handles.pop(ob_timeout_)


def print_exc():
    import traceback

    traceback.print_exc()


def setup_trio(threads):
    setattr(uwsgi, "async", threads)  # keyword
    if uwsgi.socket_timeout < 30:
        uwsgi.socket_timeout = 30

    uwsgi.loop = _TRIO


def free_req_queue(wsgi_req):
    """
    A #define that uses wsgi_req from context.
    #define free_req_queue uwsgi.async_queue_unused_ptr++; uwsgi.async_queue_unused[uwsgi.async_queue_unused_ptr] = wsgi_req
    """
    lib.uwsgi.async_queue_unused_ptr = lib.uwsgi.async_queue_unused_ptr + 1
    lib.uwsgi.async_queue_unused[lib.uwsgi.async_queue_unused_ptr] = wsgi_req
    print("free_req_queue", lib.uwsgi.async_queue_unused_ptr, wsgi_req)


@ffi.def_extern()
def uwsgi_asyncio_wait_read_hook(fd, timeout):
    print("enter read hook")
    wsgi_req = uwsgi.current_wsgi_req()
    loop = get_loop()
    loop.add_reader(fd, uwsgi_asyncio_hook_fd, wsgi_req)
    try:
        ob_timeout = loop.call_later(timeout, hook_timeout, wsgi_req)
        try:

            # to loop
            if uwsgi.schedule_to_main:
                print("wait_read_hook", fd)
                uwsgi.schedule_to_main(wsgi_req)
            else:
                print("no read hook", uwsgi.schedule_to_main)
            # from loop
        finally:
            ob_timeout.cancel()

        if wsgi_req.async_timed_out:
            return 0

        return 1

    except:
        print_exc()
        return -1

    finally:
        # returns False if not added
        loop.remove_reader(fd)


@ffi.def_extern()
def uwsgi_asyncio_wait_write_hook(fd, timeout):
    print("enter write hook")
    wsgi_req = uwsgi.current_wsgi_req()
    loop = get_loop()
    loop.add_writer(fd, uwsgi_asyncio_hook_fd, wsgi_req)
    try:
        ob_timeout = loop.call_later(timeout, hook_timeout, wsgi_req)
        try:
            # to loop
            if uwsgi.schedule_to_main:
                print("wait_write_hook", fd)
                uwsgi.schedule_to_main(wsgi_req)
            # from loop

        finally:
            ob_timeout.cancel()

        if wsgi_req.async_timed_out:
            return 0

        return 1

    except:
        print_exc()
        return -1

    finally:
        loop.remove_writer(fd)


def find_first_available_wsgi_req():
    uwsgi = lib.uwsgi
    wsgi_req = lib.find_first_available_wsgi_req()
    return wsgi_req


def uwsgi_trio_request(wsgi_req, timed_out) -> int:
    """
    Handle a request at the event-loop level.
    The application-level request handler is on another level.
    """
    status = 0
    try:
        uwsgi.wsgi_req = wsgi_req

        # ob_timeout = get_ob_timeout(wsgi_req)
        # ob_timeout.cancel()

        if timed_out > 0:
            raise IOError("timed out")  # goto end

        status = wsgi_req.socket.proto(wsgi_req)

        if status > 0:
            # refresh the timeout
            print("again a", request_id())
            return status

        else:

            if status == 0:
                # we call this two time... overengineering :(
                uwsgi.async_proto_fd_table[wsgi_req.fd] = ffi.NULL
                uwsgi.schedule_to_req()
                print("again b", request_id())
                return status

    except IOError:
        loop.remove_reader(wsgi_req.fd)
        print_exc()

    except:
        print("other exception")
        print_exc()

    # end
    r_id = request_id()
    print("normal request end {", r_id)
    uwsgi.async_proto_fd_table[wsgi_req.fd] = ffi.NULL
    lib.uwsgi_close_request(uwsgi.wsgi_req)
    free_req_queue(wsgi_req)
    print("} normal request end", r_id)

    return status


_nurseries = {}


def uwsgi_trio_accept(uwsgi_sock, nursery):

    uwsgi = lib.uwsgi
    wsgi_req = find_first_available_wsgi_req()
    if wsgi_req == ffi.NULL:
        lib.uwsgi_async_queue_is_full(lib.uwsgi_now())
        print("queue is full")
        return None

    # we should possibly be in the per-request greenthread here
    uwsgi.wsgi_req = wsgi_req
    lib.wsgi_req_setup(wsgi_req, wsgi_req.async_id, uwsgi_sock)
    uwsgi.workers[uwsgi.mywid].cores[wsgi_req.async_id].in_request = 1
    _nurseries[wsgi_req.async_id] = nursery

    if lib.wsgi_req_simple_accept(wsgi_req, uwsgi_sock.fd):
        uwsgi.workers[uwsgi.mywid].cores[wsgi_req.async_id].in_request = 0
        free_req_queue(wsgi_req)
        print("no accept")
        return None

    wsgi_req.start_of_request = lib.uwsgi_micros()
    wsgi_req.start_of_request_in_sec = wsgi_req.start_of_request // 1000000

    # enter harakiri mode
    if uwsgi.harakiri_options.workers > 0:
        lib.set_harakiri(wsgi_req, uwsgi.harakiri_options.workers)

    uwsgi.async_proto_fd_table[wsgi_req.fd] = wsgi_req

    nursery.start_soon(request_task, wsgi_req.fd, uwsgi_trio_request, wsgi_req, False)


def uwsgi_asyncio_hook_fd(wsgi_req):
    uwsgi = lib.uwsgi
    print("hook fd", wsgi_req.fd)
    uwsgi.wsgi_req = wsgi_req
    uwsgi.schedule_to_req()


def uwsgi_asyncio_hook_timeout(wsgi_req):
    uwsgi = lib.uwsgi
    print("timeout hook", wsgi_req.fd)
    uwssgi.wsgi_req = wsgi_req
    uwsgi.wsgi_req.async_timed_out = 1
    uwsgi.schedule_to_req()


def uwsgi_asyncio_hook_fix(wsgi_req):
    print("fix hook", wsgi_req.fd)
    uwsgi.wsgi_req = wsgi_req
    uwsgi.schedule_to_req()


@ffi.def_extern()
def uwsgi_asyncio_schedule_fix(wsgi_req):
    if wsgi_req:
        print("fix schedule", wsgi_req.fd)
    print("schedule_fix")
    loop = get_loop()
    loop.call_soon(uwsgi_asyncio_hook_fix, wsgi_req)


async def request_task(fd, callback, *args):
    while True:
        await trio.lowlevel.wait_readable(fd)
        status = callback(*args)
        if status <= 0:
            trio.lowlevel.notify_closing(fd)
            break


async def reader_task(fd, callback, *args):
    while True:
        await trio.lowlevel.wait_readable(fd)
        callback(*args)
        print("back from callback")


async def writer_task(fd, callable, *args):
    while True:
        await trio.lowlevel.wait_writable(fd)
        callback(*args)


# all of our trio stuff should be running in the main thread.
# When a socket becomes readable, we will kick off synchronous
# work in the per-request greenthread. Then if it's an ASGI
# application the per-request greenthread queues up another
# task to run on the main greenthread. Maybe we set wsgi_req
# as a ContextVar too.
# Several switches between C and Python (and any other uWSGI
# plugin we might happen to be running) depending.
async def server(sockets):
    global _trio_token
    _trio_token = trio.lowlevel.current_trio_token()
    async with trio.open_nursery() as nursery:
        for uwsgi_sock in sockets:
            nursery.start_soon(
                reader_task, uwsgi_sock.fd, uwsgi_trio_accept, uwsgi_sock, nursery
            )


@ffi.def_extern()
def asyncio_loop():  # name defined in cffi C header
    """
    Set up trio.
    """

    if not uwsgi.has_threads and uwsgi.mywid == 1:
        print(
            "!!! Running trio without threads IS NOT recommended, enable "
            "them with --enable-threads !!!\n"
        )

    if uwsgi.socket_timeout < 30:
        print(
            "!!! Running trio with a socket-timeout lower than 30 seconds "
            "is not recommended, tune it with --socket-timeout !!!\n"
        )

    if not uwsgi.async_waiting_fd_table:
        uwsgi.async_waiting_fd_table = lib.uwsgi_calloc(
            ffi.sizeof("struct wsgi_request *") * uwsgi.max_fd
        )
    if not uwsgi.async_proto_fd_table:
        uwsgi.async_proto_fd_table = lib.uwsgi_calloc(
            ffi.sizeof("struct wsgi_request *") * uwsgi.max_fd
        )

    uwsgi.wait_write_hook = lib.uwsgi_asyncio_wait_write_hook
    uwsgi.wait_read_hook = lib.uwsgi_asyncio_wait_read_hook

    assert lib.uwsgi.wait_write_hook == lib.uwsgi_asyncio_wait_write_hook

    if getattr(uwsgi, "async") < 1:  # keyword
        print("the async loop engine requires async mode (--async <n>)\n")
        raise SystemExit(1)

    if not uwsgi.schedule_to_main:
        print(
            "*** DANGER *** async mode without coroutine/greenthread engine loaded !!!\n"
        )

    # call uwsgi_cffi_setup_greenlets() first:
    if not uwsgi.schedule_to_req:
        print("set schedule_to_req")
        uwsgi.schedule_to_req = lib.async_schedule_to_req
    else:
        print("set schedule_fix")
        uwsgi.schedule_fix = lib.uwsgi_asyncio_schedule_fix

    sockets = []
    uwsgi_sock = uwsgi.sockets
    while uwsgi_sock != ffi.NULL:
        sockname = ffi.string(uwsgi_sock.name, uwsgi_sock.name_len).decode("utf-8")
        print(f"sock {sockname} fd {uwsgi_sock.fd}")
        sockets.append(uwsgi_sock)
        uwsgi_sock = uwsgi_sock.next

    trio.run(server, sockets)


class WSGIfilewrapper(object):
    """
    class implementing wsgi.file_wrapper
    """

    def __init__(self, wsgi_req, f, chunksize=0):
        self.wsgi_req = wsgi_req
        self.f = f
        self.chunksize = chunksize
        if hasattr(f, "close"):
            self.close = f.close

    def __iter__(self):
        return self

    def __next__(self):
        if self.chunksize:
            data = self.f.read(self.chunksize)
        else:
            data = self.f.read()
        if data:
            return data
        raise StopIteration()

    next = __next__

    def sendfile(self):
        if hasattr(self.f, "fileno"):
            lib.uwsgi_response_sendfile_do_can_close(
                self.wsgi_req, self.f.fileno(), 0, 0, 0
            )
        elif hasattr(self.f, "read"):
            if self.chunksize == 0:
                chunk = self.f.read()
                if len(chunk) > 0:
                    lib.uwsgi_response_write_body_do(
                        self.wsgi_req, ffi.new("char[]", chunk), len(chunk)
                    )
                return
            while True:
                chunk = self.f.read(self.chunksize)
                if chunk is None or len(chunk) == 0:
                    break
                lib.uwsgi_response_write_body_do(
                    self.wsgi_req, ffi.new("char[]", chunk), len(chunk)
                )


class WSGIinput(object):
    """
    class implementing wsgi.input
    """

    def __init__(self, wsgi_req):
        self.wsgi_req = wsgi_req

    def read(self, size=0):
        rlen = ffi.new("ssize_t*")
        chunk = lib.uwsgi_request_body_read(self.wsgi_req, size, rlen)
        if chunk != ffi.NULL:
            return ffi.buffer(chunk, rlen[0])[:]
        if rlen[0] < 0:
            raise IOError("error reading wsgi.input")
        raise IOError("error waiting for wsgi.input")

    def getline(self, hint=0):
        rlen = ffi.new("ssize_t*")
        chunk = lib.uwsgi_request_body_readline(self.wsgi_req, hint, rlen)
        if chunk != ffi.NULL:
            return ffi.buffer(chunk, rlen[0])[:]
        if rlen[0] < 0:
            raise IOError("error reading line from wsgi.input")
        raise IOError("error waiting for line on wsgi.input")

    def readline(self, hint=0):
        return self.getline(hint)

    def readlines(self, hint=0):
        lines = []
        while True:
            chunk = self.getline(hint)
            if len(chunk) == 0:
                break
            lines.append(chunk)
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        chunk = self.getline()
        if len(chunk) == 0:
            raise StopIteration
        return chunk


def asgi_start_response(wsgi_req, status, headers):
    print(wsgi_req, status, headers)

    status = b"%d" % status
    lib.uwsgi_response_prepare_headers(wsgi_req, ffi.new("char[]", status), len(status))
    for (header, value) in headers:
        lib.uwsgi_response_add_header(
            wsgi_req,
            ffi.new("char[]", header),
            len(header),
            ffi.new("char[]", value),
            len(value),
        )


def asgi_scope_http(wsgi_req):
    """
    Create the ASGI scope for a http or websockets connection.
    """
    environ = {}
    headers = []
    iov = wsgi_req.hvec
    for i in range(0, wsgi_req.var_cnt, 2):
        key, value = (
            ffi.string(ffi.cast("char*", iov[i].iov_base), iov[i].iov_len),
            ffi.string(ffi.cast("char*", iov[i + 1].iov_base), iov[i + 1].iov_len),
        )
        if key.startswith(b"HTTP_"):
            headers.append((key[5:].lower(), value))
        else:
            environ[key.decode("ascii")] = value

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.1"},
        "http_version": environ["SERVER_PROTOCOL"][len("HTTP/") :].decode("utf-8"),
        "method": environ["REQUEST_METHOD"].decode("utf-8"),
        "scheme": "http",
        "path": environ["PATH_INFO"].decode("utf-8"),
        "raw_path": environ["REQUEST_URI"],
        "query_string": environ["QUERY_STRING"],
        "root_path": environ["SCRIPT_NAME"].decode("utf-8"),
        "headers": headers,
        # some references to REMOTE_PORT but not always in environ
        "client": (environ["REMOTE_ADDR"].decode("utf-8"), 0),
        "server": (environ["SERVER_NAME"].decode("utf-8"), environ["SERVER_PORT"]),
        "environ": environ,
    }

    if environ.get("HTTPS") in (b"on",):
        scope["scheme"] = "https"

    if wsgi_req.http_sec_websocket_key != ffi.NULL:
        scope["type"] = "websocket"

    return scope


def websocket_recv_nb(wsgi_req):
    """
    uwsgi.websocket_recv_nb()
    """
    ub = lib.uwsgi_websocket_recv_nb(wsgi_req)
    if ub == ffi.NULL:
        raise IOError("unable to receive websocket message")
    ret = ffi.buffer(ub.buf, ub.pos)[:]
    lib.uwsgi_buffer_destroy(ub)
    return ret


def handle_asgi_request(wsgi_req, app):
    """
    Handle asgi request, with synchronous code, in the per-request greenlet.
    """
    scope = asgi_scope_http(wsgi_req)
    gc = greenlet.getcurrent()

    async def send(event):
        gc.switch(event)

    if scope["type"] == "websocket":

        closed = False

        async def receiver():
            nonlocal closed

            yield {"type": "websocket.connect"}
            while True:
                try:
                    print("rx, closed=", closed)
                    msg = websocket_recv_nb(wsgi_req)
                except IOError:
                    closed = True
                    gc.switch({"type": "websocket.close"})
                    yield {
                        "type": "websocket.disconnect",
                        "code": 1000,
                    }  # todo lookup code
                    # don't raise, keep receivin' ?
                if msg:
                    # check wsgi_req->websocket_opcode for text / binary
                    value = {"type": "websocket.receive"}
                    # *  %x0 denotes a continuation frame
                    # *  %x1 denotes a text frame
                    # *  %x2 denotes a binary frame
                    opcode = wsgi_req.websocket_opcode
                    if opcode == 1:
                        value["text"] = msg.decode("utf-8")
                    elif opcode == 2:
                        value["bytes"] = msg
                    else:
                        print("surprise opcode", opcode)
                    yield value
                else:
                    print("no msg", wsgi_req.websocket_opcode)
                    await trio.lowlevel.wait_readable(wsgi_req.fd)
            # send this if connection is closed:
            # { "type": "websocket.disconnect", "code": int }

        receive = receiver().__anext__

        async def send(event):
            nonlocal closed
            print("ws send", event)
            if closed:
                print("ignore send on closed ws")

            elif event["type"] == "websocket.accept":
                if (
                    lib.uwsgi_websocket_handshake(
                        wsgi_req, ffi.NULL, 0, ffi.NULL, 0, ffi.NULL, 0
                    )
                    < 0
                ):
                    closed = True
                    gc.switch({"type": "websocket.close"})
                    raise IOError("unable to send websocket handshake")

            elif event["type"] == "websocket.send":
                # ok to call during any part of app?
                try:
                    msg = event["bytes"]
                    if (
                        lib.uwsgi_websocket_send_binary(
                            wsgi_req, ffi.new("char[]", msg), len(msg)
                        )
                        < 0
                    ):
                        closed = True
                        gc.switch({"type": "websocket.close"})
                        raise IOError("unable to send websocket message")
                except KeyError:
                    msg = event["text"].encode("utf-8")
                    if (
                        lib.uwsgi_websocket_send(
                            wsgi_req, ffi.new("char[]", msg), len(msg)
                        )
                        < 0
                    ):
                        closed = True
                        gc.switch({"type": "websocket.close"})
                        raise IOError("unable to send websocket message")

            elif event["type"] == "websocket.close":
                print("asked to close in send")
                closed = True
                gc.switch(event)

    elif scope["type"] == "http":

        async def receive():
            return {"type": "http.request"}

    def create_app_task():
        nursery = _nurseries[wsgi_req.async_id]
        nursery.start_soon(app, scope, receive, send)

    _trio_token.run_sync_soon(create_app_task)

    while True:
        event = gc.parent.switch()
        if event["type"] == "http.response.start":
            # raw uwsgi function accepts bytes
            asgi_start_response(wsgi_req, event["status"], event["headers"])
        elif event["type"] == "http.response.body":
            data = event["body"]
            print(
                "write_body_do",
                lib.uwsgi_response_write_body_do(
                    wsgi_req, ffi.new("char[]", data), len(data)
                ),
            )
            if not event.get("more_body"):
                break
        elif event["type"] == "websocket.close":
            break
        else:
            print("loop event", event)

    return lib.UWSGI_OK


def to_network(native):
    return native.encode("latin1")


ASGI_CALLABLE = ffi.cast("void *", 2)


@ffi.def_extern()
def uwsgi_cffi_request(wsgi_req):
    try:
        return _uwsgi_cffi_request(wsgi_req)
    except:
        print_exc()
    return lib.UWSGI_OK


def _uwsgi_cffi_request(wsgi_req):
    """
    the WSGI request handler
    """

    if wsgi_req.async_force_again:
        print("force again")
        wsgi_req.async_force_again = 0
        # just close it
        try:
            ob_timeout = get_ob_timeout(wsgi_req)
            ob_timeout.cancel()
        except KeyError:
            pass
        asyncio.get_event_loop().remove_reader(wsgi_req.fd)
        return lib.UWSGI_OK

    def writer(data):
        lib.uwsgi_response_write_body_do(wsgi_req, ffi.new("char[]", data), len(data))

    def start_response(status, headers, exc_info=None):
        if exc_info:
            traceback.print_exception(*exc_info)
        status = to_network(status)
        lib.uwsgi_response_prepare_headers(
            wsgi_req, ffi.new("char[]", status), len(status)
        )
        for hh in headers:
            header, value = to_network(hh[0]), to_network(hh[1])
            lib.uwsgi_response_add_header(
                wsgi_req,
                ffi.new("char[]", header),
                len(hh[0]),
                ffi.new("char[]", value),
                len(hh[1]),
            )
        return writer

    if lib.uwsgi_parse_vars(wsgi_req):
        return -1

    # check dynamic
    # check app_id
    app_id = lib.uwsgi_get_app_id(
        wsgi_req, wsgi_req.appid, wsgi_req.appid_len, lib.cffi_plugin.modifier1
    )
    if app_id == -1 and not lib.uwsgi.no_default_app and lib.uwsgi.default_app > -1:
        # and default app modifier1 == our modifier1
        app_id = lib.uwsgi.default_app
    wsgi_req.app_id = app_id

    app_mount = ""
    # app_mount can be something while app_id is -1
    if wsgi_req.appid != ffi.NULL:
        app_mount = ffi.string(wsgi_req.appid).decode("utf-8")

    # uwsgi app struct
    wi = lib.uwsgi.workers[lib.uwsgi.mywid].apps[app_id]
    wi.requests += 1  # we might wind up here more often than expected
    app = wsgi_apps.get(app_id)

    # (see python wsgi_handlers.c)

    if wi.callable == ASGI_CALLABLE:
        handle_asgi_request(wsgi_req, app)
        return lib.UWSGI_OK

    environ = {}
    iov = wsgi_req.hvec
    for i in range(0, wsgi_req.var_cnt, 2):
        environ[
            ffi.string(ffi.cast("char*", iov[i].iov_base), iov[i].iov_len).decode(
                "latin1"
            )
        ] = ffi.string(
            ffi.cast("char*", iov[i + 1].iov_base), iov[i + 1].iov_len
        ).decode(
            "latin1"
        )

    # check bytes on environ...
    environ["wsgi.version"] = (1, 0)
    scheme = "http"
    if "HTTPS" in environ:
        if environ["HTTPS"] in ("on", "ON", "On", "1", "true", "TRUE", "True"):
            scheme = "https"
    environ["wsgi.url_scheme"] = environ.get("UWSGI_SCHEME", scheme)
    environ["wsgi.input"] = WSGIinput(wsgi_req)
    environ["wsgi.errors"] = sys.stderr
    environ["wsgi.run_once"] = False
    environ["wsgi.file_wrapper"] = lambda f, chunksize=0: WSGIfilewrapper(
        wsgi_req, f, chunksize
    )
    environ["wsgi.multithread"] = True
    environ["wsgi.multiprocess"] = True

    environ["uwsgi.core"] = wsgi_req.async_id
    environ["uwsgi.node"] = ffi.string(lib.uwsgi.hostname).decode("latin1")

    try:
        response = app(environ, start_response)
    except:
        print_exc()
        # can I get a 500?
        # will also get here when a websocket closes
        wsgi_req.async_force_again = 1
        return lib.UWSGI_AGAIN

    if type(response) is bytes:
        writer(response)
    else:
        try:
            if isinstance(response, WSGIfilewrapper):
                response.sendfile()
            else:
                for chunk in response:
                    if isinstance(chunk, WSGIfilewrapper):
                        try:
                            chunk.sendfile()
                        finally:
                            chunk.close()
                    else:
                        writer(chunk)
        finally:
            if hasattr(response, "close"):
                response.close()

    return lib.UWSGI_OK


def trio_init():
    lib.uwsgi_register_loop(_TRIO, lib.asyncio_loop)
