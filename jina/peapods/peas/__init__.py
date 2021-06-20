import argparse
from typing import Any, Tuple
import time
import multiprocessing
import threading

from .helper import _get_event, ConditionalEvent
from .run import run
from ... import __stop_msg__, __ready_msg__, __default_host__
from ...enums import PeaRoleType, RuntimeBackendType, SocketType
from ...excepts import RuntimeFailToStart
from ...helper import typename
from ...logging.logger import JinaLogger
from ..runtimes.jinad import JinadRuntime
from ..zmq import Zmqlet, send_ctrl_message

__all__ = ['BasePea']


class BasePea:
    """
    :class:`BasePea` is a thread/process- container of :class:`BaseRuntime`. It leverages :class:`threading.Thread`
    or :class:`multiprocessing.Process` to manage the lifecycle of a :class:`BaseRuntime` object in a robust way.

    A :class:`BasePea` must be equipped with a proper :class:`Runtime` class to work.
    """

    def __init__(self, args: 'argparse.Namespace'):
        super().__init__()  #: required here to call process/thread __init__
        self.args = args
        backend_runtime = getattr(
            self.args, 'runtime_backend', RuntimeBackendType.THREAD
        )

        self.daemon = args.daemon  #: required here to set process/thread daemon

        self.name = self.args.name or self.__class__.__name__
        self.is_ready = _get_event(backend_runtime)
        self.is_shutdown = _get_event(backend_runtime)
        self.ready_or_shutdown = ConditionalEvent(
            getattr(args, 'runtime_backend', RuntimeBackendType.THREAD),
            events_list=[self.is_ready, self.is_shutdown],
        )
        self.logger = JinaLogger(self.name, **vars(self.args))

        if self.args.runtime_backend == RuntimeBackendType.THREAD:
            self.logger.warning(
                f' Using Thread as runtime backend is not recommended for production purposes. It is '
                f'just supposed to be used for easier debugging. Besides the performance considerations, it is'
                f'specially dangerous to mix `Executors` running in different types of `RuntimeBackends`.'
            )

        self._envs = {'JINA_POD_NAME': self.name, 'JINA_LOG_ID': self.args.identity}
        if self.args.quiet:
            self._envs['JINA_LOG_CONFIG'] = 'QUIET'
        if self.args.env:
            self._envs.update(self.args.env)

        # arguments needed to create `runtime` and communicate with it in the `run` in the stack of the new process
        # or thread. Control address from Zmqlet has some randomness and therefore we need to make sure Pea knows
        # control address of runtime
        self.runtime_cls, self._is_remote_controlled = self._get_runtime_cls()

        # This logic must be improved specially when it comes to naming. It is about relative local/remote position
        # between the runtime and the `ZEDRuntime` it may control
        self._zed_runtime_ctrl_addres = Zmqlet.get_ctrl_address(
            self.args.host, self.args.port_ctrl, self.args.ctrl_with_ipc
        )[0]
        self._local_runtime_ctrl_address = (
            Zmqlet.get_ctrl_address(None, None, True)[0]
            if self.runtime_cls == JinadRuntime
            else self._zed_runtime_ctrl_addres
        )
        self._timeout_ctrl = self.args.timeout_ctrl

        self.worker = {
            RuntimeBackendType.THREAD: threading.Thread,
            RuntimeBackendType.PROCESS: multiprocessing.Process,
        }.get(backend_runtime)(
            target=run,
            kwargs={
                'args': self.args,
                'logger': self.logger,
                'envs': self._envs,
                'runtime_cls': self.runtime_cls,
                'local_runtime_ctrl_addr': self._local_runtime_ctrl_address,
                'zed_runtime_ctrl_addres': self._zed_runtime_ctrl_addres,
                'is_ready_event': self.is_ready,
                'is_shutdown_event': self.is_shutdown,
            },
        )

    def start(self):
        """Start the Pea.
        This method calls :meth:`start` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        .. #noqa: DAR201
        """
        self.worker.start()
        if not self.args.noblock_on_start:
            self.wait_start_success()
        return self

    def join(self, *args, **kwargs):
        """Joins the Pea.
        This method calls :meth:`join` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.

        :param args: extra positional arguments to pass to join
        :param kwargs: extra keyword arguments to pass to join
        """
        self.worker.join(*args, **kwargs)

    def terminate(self):
        """Terminate the Pea.
        This method calls :meth:`terminate` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        """
        if hasattr(self.worker, 'terminate'):
            self.worker.terminate()

    def activate_runtime(self):
        """ Send activate control message. """
        if self._dealer:
            send_ctrl_message(
                self._zed_runtime_ctrl_addres, 'ACTIVATE', timeout=self._timeout_ctrl
            )

    def _deactivate_runtime(self):
        """Send deactivate control message. """
        if self._dealer:
            send_ctrl_message(
                self._zed_runtime_ctrl_addres, 'DEACTIVATE', timeout=self._timeout_ctrl
            )

    def _cancel_rumtime(self):
        """Send terminate control message."""
        send_ctrl_message(
            self._local_runtime_ctrl_address, 'TERMINATE', timeout=self._timeout_ctrl
        )

    def wait_start_success(self):
        """Block until all peas starts successfully.

        If not success, it will raise an error hoping the outer function to catch it
        """
        _timeout = self.args.timeout_ready
        if _timeout <= 0:
            _timeout = None
        else:
            _timeout /= 1e3
        if self.ready_or_shutdown.event.wait(_timeout):
            if self.is_shutdown.is_set():
                # return too early and the shutdown is set, means something fails!!
                if self.args.quiet_error:
                    self.logger.critical(
                        f'fail to start {self!r} because {self.runtime_cls!r} throws some exception, '
                        f'remove "--quiet-error" to see the exception stack in details'
                    )
                raise RuntimeFailToStart
            else:
                self.logger.success(__ready_msg__)
        else:
            _timeout = _timeout or -1
            self.logger.warning(
                f'{self.runtime_cls!r} timeout after waiting for {self.args.timeout_ready}ms, '
                f'if your executor takes time to load, you may increase --timeout-ready'
            )
            self.close()
            raise TimeoutError(
                f'{typename(self)}:{self.name} can not be initialized after {_timeout * 1e3}ms'
            )

    @property
    def _dealer(self):
        """Return true if this `Pea` must act as a Dealer responding to a Router
        .. # noqa: DAR201
        """
        return self.args.socket_in == SocketType.DEALER_CONNECT

    def close(self) -> None:
        """Close the Pea

        This method makes sure that the `Process/thread` is properly finished and its resources properly released
        """
        # wait 0.1s for the process/thread to end naturally, in this case no "cancel" is required this is required for
        # the is case where in subprocess, runtime.setup() fails and _finally() is not yet executed, BUT close() in the
        # main process is calling runtime.cancel(), which is completely unnecessary as runtime.run_forever() is not
        # started yet.
        self.join(0.1)

        # if that 1s is not enough, it means the process/thread is still in forever loop, cancel it
        if self.is_ready.is_set() and not self.is_shutdown.is_set():
            try:
                self._deactivate_runtime()
                time.sleep(0.1)
                self._cancel_rumtime()
                self.is_shutdown.wait()
            except Exception as ex:
                self.logger.error(
                    f'{ex!r} during {self._deactivate_runtime!r}'
                    + f'\n add "--quiet-error" to suppress the exception details'
                    if not self.args.quiet_error
                    else '',
                    exc_info=not self.args.quiet_error,
                )

            # if it is not daemon, block until the process/thread finish work
            if not self.args.daemon:
                self.join()
        else:
            # if it fails to start, the process will hang at `join`
            self.terminate()

        self.logger.success(__stop_msg__)
        self.logger.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _get_runtime_cls(self) -> Tuple[Any, bool]:
        is_remote_controlled = False
        if self.args.host != __default_host__:
            self.args.runtime_cls = 'JinadRuntime'
            is_remote_controlled = True
        if self.args.runtime_cls == 'ZEDRuntime' and self.args.uses.startswith(
            'docker://'
        ):
            self.args.runtime_cls = 'ContainerRuntime'
        if hasattr(self.args, 'protocol'):
            self.args.runtime_cls = {
                GatewayProtocolType.GRPC: 'GRPCRuntime',
                GatewayProtocolType.WEBSOCKET: 'WebSocketRuntime',
                GatewayProtocolType.HTTP: 'HTTPRuntime',
            }[self.args.protocol]
        from ..runtimes import get_runtime

        v = get_runtime(self.args.runtime_cls)
        return v, is_remote_controlled

    @property
    def role(self) -> 'PeaRoleType':
        """Get the role of this pea in a pod


        .. #noqa: DAR201"""
        return self.args.pea_role

    @property
    def _is_inner_pea(self) -> bool:
        """Determine whether this is a inner pea or a head/tail


        .. #noqa: DAR201"""
        return self.role is PeaRoleType.SINGLETON or self.role is PeaRoleType.PARALLEL
