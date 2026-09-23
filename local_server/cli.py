"""Own the local DB child and HTTP server, preserving data on every shutdown."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parent.parent
_PRIVATE = tuple(ipaddress.ip_network(s) for s in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class StartupError(Exception):
    """Only developer-defined messages may be shown, never nested exceptions."""


def isolated_environment():
    """Remove ambient service/SDK settings before importing any application code."""
    for name in tuple(os.environ):
        if (name.upper().startswith(("AWS_", "BOTO_", "SENTRY_", "ARC_"))
                or name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                   "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE",
                                   "SSL_CERT_DIR", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS",
                                   "_JAVA_OPTIONS"}):
            del os.environ[name]
    os.environ.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                      AWS_EC2_METADATA_DISABLED="true", AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true")


def restrict_outbound(db_port, api_host, api_port):
    """Local executable only: fail before any Python outbound socket outside DB."""
    def guard(event, args):
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address != ("127.0.0.1", db_port):
                raise OSError("Local server outbound connection denied")
        elif event == "socket.getaddrinfo":
            if (args[0], args[1]) not in {
                ("127.0.0.1", db_port), ("127.0.0.1", str(db_port)),
                (api_host, api_port), (api_host, str(api_port)),
            }:
                raise OSError("Local server address lookup denied")
        elif event == "socket.sendto":
            raise OSError("Local server datagram denied")
    sys.addaudithook(guard)


def _private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return str(address) == value and any(address in network for network in _PRIVATE)


def validate_network(host, port, db_port, clients, insecure_lan):
    if not (1024 <= port <= 65535 and 1024 <= db_port <= 65535) or port == db_port:
        raise StartupError("API and DB ports must be different numbers between 1024 and 65535.")
    if host == "127.0.0.1":
        if clients or insecure_lan:
            raise StartupError("LAN options require the Mac's explicit private IPv4 address.")
        return frozenset({"127.0.0.1"})
    if not _private_ipv4(host) or not insecure_lan or not clients:
        raise StartupError("LAN requires a private Mac IPv4, --allow-client and --allow-insecure-lan.")
    if any(not _private_ipv4(client) or client == host for client in clients):
        raise StartupError("Each LAN client must be a separate private IPv4 address.")
    return frozenset({host, *clients})


@contextmanager
def installation_lock(path):
    # Resolve neither the supplied directory nor its ancestors through symlinks.
    path = path.absolute()
    if ".." in path.parts:
        raise StartupError("Use a data directory without parent traversal.")
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise StartupError("The data directory must not contain symlinks.")
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StartupError("The data directory must be owned by you with permissions 0700.")
    fd = os.open(path / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise StartupError("Invalid local server lock file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StartupError("Another server already owns this data directory.") from None
        yield path
    finally:
        os.close(fd)


def ensure_port_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            # Match the HTTP/DB listener's normal restart behavior after TIME_WAIT.
            # SO_REUSEPORT is deliberately not enabled: an existing listener wins.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(1)
        except OSError:
            raise StartupError("A requested address is unavailable or its port is already in use.") from None


def verify_distribution(home):
    home = home.absolute()
    manifest = json.loads((ROOT / "docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json").read_text())
    for relative, digest in manifest["files"].items():
        path = home / relative
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise StartupError("DynamoDB Local distribution must not contain symlinks.")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise StartupError("DynamoDB Local 3.3.1 files are missing or do not match the verified distribution.")
    # Classpath wildcards must never pick up additional, unchecked JARs.
    expected = set(manifest["files"])
    actual = {p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()}
    if actual != expected:
        raise StartupError("DynamoDB Local distribution contains unexpected files.")
    return home


def start_database(home, db_dir, db_port, data_dir):
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        raise StartupError("Java and javac are required; install a compatible JDK first.")
    build = tempfile.TemporaryDirectory(prefix="java-build-", dir=data_dir)
    classes = Path(build.name)
    classpath = os.pathsep.join((str(home / "DynamoDBLocal.jar"), str(home / "DynamoDBLocal_lib" / "*")))
    child_env = {"PATH": os.defpath, "LANG": "en_US.UTF-8"}
    try:
        result = subprocess.run([javac, "-cp", classpath, "-d", str(classes),
                                 str(ROOT / "scripts/local_dynamodb/ArcLocalDynamo.java")],
                                env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        build.cleanup()
        raise StartupError("Could not compile the local DB launcher with this JDK.") from None
    if result.returncode:
        build.cleanup()
        raise StartupError("The JDK and DynamoDB Local 3.3.1 launcher are incompatible.")
    ensure_port_free("127.0.0.1", db_port)
    nonce = secrets.token_hex(16)
    child = subprocess.Popen([java, "-Djava.library.path=" + str(home / "DynamoDBLocal_lib"),
                              "-cp", os.pathsep.join((str(classes), classpath)), "ArcLocalDynamo",
                              str(db_port), str(db_dir), nonce], env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    child._arc_local_build = build
    lines = queue.Queue(maxsize=64)

    def drain():
        # Do not forward vendor output (which may contain local paths or requests).
        for line in child.stdout:
            if line.strip() == "ARC_DYNAMODB_READY:" + nonce:
                lines.put(True)
    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise StartupError("The local DB child exited before becoming ready; existing servers were not used.")
            try:
                lines.get(timeout=0.1)
                if child.poll() is not None:
                    raise StartupError("The local DB child exited during startup.")
                return child
            except queue.Empty:
                pass
        raise StartupError("The local DB did not become ready within 30 seconds.")
    except BaseException:
        stop_database(child)
        raise


def stop_database(child):
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
    finally:
        if getattr(child, "_arc_local_build", None) is not None:
            child._arc_local_build.cleanup()
            child._arc_local_build = None


def close_resources(server, db, child):
    """Every owned resource is attempted even if an earlier cleanup failed."""
    try:
        if server is not None:
            try:
                server.task_dispatcher.shutdown(timeout=5)
            finally:
                from waitress import wasyncore
                wasyncore.close_all(map=server._map)
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            if child is not None:
                stop_database(child)


def argument_parser():
    parser = argparse.ArgumentParser(description="Local ARC journey with private files and an owned calculator worker.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db-port", type=int, default=8001)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var/local-server")
    parser.add_argument("--dynamodb-home", type=Path, default=ROOT / "var/dynamodb-local-3.3.1")
    parser.add_argument("--allow-client", action="append", default=[])
    parser.add_argument("--allow-insecure-lan", action="store_true")
    parser.add_argument("--control-only", action="store_true",
                        help="Run login/session/program control APIs without training or calculation.")
    parser.add_argument("--course-v2", action="store_true",
                        help="Serve the explicit /api/v2 course assembly. The default remains /mock/v1.")
    parser.add_argument("--calculation-body-bytes", type=int, default=1_000_000)
    parser.add_argument("--artifact-bytes", type=int, default=8_000_000)
    parser.add_argument("--storage-quota-bytes", type=int, default=1_073_741_824)
    parser.add_argument("--worker-lease-seconds", type=int, default=60)
    parser.add_argument("--worker-retry-seconds", type=int, default=5)
    parser.add_argument("--worker-poll-seconds", type=float, default=0.25)
    return parser


def close_journey_resources(runtime, server, db, child):
    """Never release an installation while old HTTP threads can still write."""
    try:
        runtime.close(server)
    finally:
        try:
            if runtime.requires_process_exit:
                # Undrained HTTP may own an SDK client lock as well. Stop the
                # owned DB process, then let process exit close shared FDs.
                if child is not None:
                    stop_database(child)
            else:
                close_resources(None, db, child)
        finally:
            if runtime.requires_process_exit:
                print("Local runtime shutdown could not finish safely; terminating this CLI process.",
                      file=sys.stderr, flush=True)
                # Only this CLI exits, after cleanup attempts for its children.
                # The OS stops all remaining threads before releasing the lock.
                os._exit(1)


def main(argv=None):
    args = argument_parser().parse_args(argv)
    if args.course_v2 and args.control_only:
        print("Local server could not start: The course API cannot run in control-only mode.",
              file=sys.stderr)
        return 1
    db = child = server = runtime = None
    phase = "validate local configuration"
    os.umask(0o077)
    isolated_environment()

    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        from local_server.runtime import LocalOptions
        options = LocalOptions(args.calculation_body_bytes, args.artifact_bytes, args.storage_quota_bytes,
                               args.worker_lease_seconds, args.worker_retry_seconds, args.worker_poll_seconds)
        clients = validate_network(args.host, args.port, args.db_port, args.allow_client, args.allow_insecure_lan)
        ensure_port_free(args.host, args.port)
        home = verify_distribution(args.dynamodb_home)
        with installation_lock(args.data_dir) as data_dir:
            phase = "load local dependencies"
            from local_server.database import prepare_material, connect_application
            from local_server.http import make_application, create_server
            try:
                phase = "prepare private local data"
                material = prepare_material(data_dir)
                phase = "start the owned local database"
                child = start_database(home, material.db_dir, args.db_port, data_dir)
                restrict_outbound(args.db_port, args.host, args.port)
                phase = "connect the owned local database"
                endpoint = f"http://127.0.0.1:{args.db_port}"
                if args.control_only:
                    db = connect_application(endpoint, material, operations_role="api")
                else:
                    db = connect_application(endpoint, material, journey=True, operations_role="api")
                    # The successful parent initialization may have marked the
                    # record initialized; pass only that validated new material.
                    material = prepare_material(data_dir, initialize=False)
                    from local_server.runtime import LocalRuntime
                    phase = "prepare the local calculation runtime"
                    runtime = LocalRuntime(db, material, options, args.host, args.port, child,
                                           course_v2=args.course_v2)
                    phase = "start the owned local worker"
                    runtime.start_worker(material, endpoint, args.host, args.port)
                phase = "start the local HTTP listener"
                if runtime is None:
                    application = make_application(db.application, db.ready, args.host, args.port, clients)
                else:
                    application = make_application(
                        runtime.service, runtime.ready, args.host, args.port, clients,
                        calculation_body_limit=options.calculation_body_bytes, chart_service=runtime.charts,
                        response_body_limit=options.response_body_limit, execution_ready=runtime.available,
                    )
                server = create_server(application, args.host, args.port)
                if runtime is None:
                    print(f"ARC local control API ready: http://{args.host}:{args.port}", flush=True)
                    print("Available: login, session, programs/progress, logout. Training/calculation unavailable.", flush=True)
                elif args.course_v2:
                    print(f"ARC local course API ready: http://{args.host}:{args.port}", flush=True)
                    print("Available: /api/v2 login, course progress, measured binary calculation, stored results and 300-second charts.", flush=True)
                    print("/mock/v1 is not served. Dummy login has no synthetic enrollments. ARC submission remains disabled.", flush=True)
                else:
                    print(f"ARC local journey API ready: http://{args.host}:{args.port}", flush=True)
                    print("Available: login, programs, measured binary calculation, stored results and 300-second charts.", flush=True)
                    print("CPR completion policy remains pending; ARC submission remains disabled.", flush=True)
                print("DynamoDB: loopback only. Ctrl+C stops owned services and preserves local data.", flush=True)
                if args.allow_insecure_lan:
                    print("LAN HTTP is unencrypted. Dummy data only; allowed IPs are not personal authentication.", flush=True)
                phase = "serve the local journey" if runtime is not None else "serve the local control API"
                if runtime is None:
                    server.run()
                else:
                    runtime.serve(server)
            finally:
                # Keep the installation lock until all owned resources are closed.
                try:
                    if runtime is None:
                        close_resources(server, db, child)
                    else:
                        # The second interrupt must not skip child cleanup and
                        # release this installation while old work is running.
                        signal.signal(signal.SIGINT, signal.SIG_IGN)
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                        close_journey_resources(runtime, server, db, child)
                finally:
                    db = child = server = runtime = None
        return 0
    except KeyboardInterrupt:
        return 0
    except StartupError as error:
        print("Local server could not start: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Local server failed to " + phase + ". Check local dependencies, private data files and DB configuration.", file=sys.stderr)
        return 1
    finally:
        close_resources(server, db, child)
