"""Mininet integration for the bmv2 ``simple_switch``.

The FITI project uses one ``simple_switch`` per topology node.  This module
only provides the two Mininet node classes needed for that:

* :class:`P4Host`   -- host with NIC offloads disabled (bmv2 forwards raw
                       frames, hardware segmentation would corrupt them)
* :class:`P4Switch` -- starts ``simple_switch`` on the Mininet veths and waits
                       until the Thrift runtime is reachable

It is intentionally self-contained: the project only uses the Thrift runtime
(``simple_switch_CLI``), not P4Runtime / gRPC.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time

from mininet.log import info, warning
from mininet.node import Host, Switch

from fiti.paths import DEFAULT_LOG_DIR

#: seconds to wait for the Thrift runtime to come up
SWITCH_START_TIMEOUT = 30


class P4Host(Host):
    """Host with NIC features that confuse bmv2 turned off.

    IPv6 is left enabled because the optional SRv6 overlay needs it.
    """

    def config(self, **params):
        result = super(P4Host, self).config(**params)
        intf = self.defaultIntf()
        if intf is None:
            return result
        # bmv2 forwards raw frames; hardware offloads would make them look
        # corrupt on the wire.
        self.cmd('ethtool -K %s rx off tx off sg off tso off gso off gro off '
                 'lro off 2>/dev/null' % intf)
        return result


class P4Switch(Switch):
    """bmv2 ``simple_switch`` as a Mininet switch."""

    def __init__(self, name, sw_path='simple_switch', json_path=None,
                 thrift_port=None, device_id=0, log_file=None,
                 log_level='info', pcap_dump=None, log_dir=None, **kwargs):
        Switch.__init__(self, name, **kwargs)
        if not json_path:
            raise ValueError('%s: json_path is required' % name)
        if thrift_port is None:
            raise ValueError('%s: thrift_port is required' % name)

        self.sw_path = sw_path
        self.json_path = os.path.abspath(json_path)
        self.thrift_port = int(thrift_port)
        self.device_id = int(device_id)
        self.log_dir = os.path.abspath(log_dir or DEFAULT_LOG_DIR)
        self.log_file = (log_file or
                         os.path.join(self.log_dir,
                                      'bmv2-%s-%d-%d.log'
                                      % (name, self.thrift_port,
                                         os.geteuid())))
        self.log_level = log_level
        self.pcap_dump = pcap_dump
        self.notifications_addr = ('ipc:///tmp/bmv2-%s-%d-%d-notifications.ipc'
                                   % (name, self.thrift_port, os.geteuid()))
        self.process = None

    def _open_log(self):
        """Open the bmv2 log, coping with files owned by another user."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            return open(self.log_file, 'w')
        except OSError:
            try:
                os.unlink(self.log_file)
                return open(self.log_file, 'w')
            except OSError:
                fd, path = tempfile.mkstemp(prefix='bmv2-%s-' % self.name,
                                            suffix='.log')
                os.close(fd)
                self.log_file = path
                return open(path, 'w')

    def _thrift_port_busy(self):
        """True if something already answers on the Thrift port."""
        try:
            sock = socket.create_connection(('127.0.0.1', self.thrift_port),
                                            timeout=0.5)
        except OSError:
            return False
        sock.close()
        return True

    def start(self, controllers):
        if self._thrift_port_busy():
            raise RuntimeError(
                'thrift port %d on 127.0.0.1 is already in use (a leftover '
                'simple_switch?). Stop it with "sudo pkill -f simple_switch" '
                'or choose another --thrift-base.'
                % self.thrift_port)

        # A killed bmv2 can leave its nanomsg socket behind, which would make
        # the next start fail with "Address already in use".
        sock_path = self.notifications_addr[len('ipc://'):]
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass

        log = self._open_log()

        args = [self.sw_path]
        for port, intf in sorted(self.intfs.items()):
            # Mininet gives every node a loopback interface on port 0;
            # bmv2 should only get the veths that form the topology.
            if port == 0 or intf is None or intf.name == 'lo':
                continue
            args += ['-i', '%d@%s' % (port, intf.name)]
        args += ['--thrift-port', str(self.thrift_port)]
        args += ['--device-id', str(self.device_id)]
        args += ['--notifications-addr', self.notifications_addr]
        args += ['--log-file', self.log_file, '--log-level', self.log_level]
        if self.pcap_dump:
            if isinstance(self.pcap_dump, str):
                args += ['--pcap=%s' % self.pcap_dump]
            else:
                args += ['--pcap']
        args += [self.json_path]

        info('starting %s: %s\n' % (self.name, ' '.join(args)))
        try:
            self.process = subprocess.Popen(args, stdout=log, stderr=log,
                                            close_fds=True, cwd=os.getcwd())
        except FileNotFoundError:
            log.close()
            raise RuntimeError('找不到 %s，请先安装 bmv2 并确认它在 PATH 中'
                               % self.sw_path)
        # Popen duplicated the descriptor; close the parent's copy so a
        # long-running 41-switch topology does not leak file descriptors.
        log.close()

        deadline = time.time() + SWITCH_START_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('%s exited with code %s, see %s'
                                   % (self.name, self.process.returncode,
                                      self.log_file))
            try:
                sock = socket.create_connection(('127.0.0.1', self.thrift_port),
                                                timeout=1)
                sock.close()
                if self.process.poll() is not None:
                    raise RuntimeError('%s exited with code %s, see %s'
                                       % (self.name, self.process.returncode,
                                          self.log_file))
                info('%s ready (thrift port %d)\n'
                     % (self.name, self.thrift_port))
                return self.process
            except OSError:
                time.sleep(0.1)

        self.stop()
        raise RuntimeError('%s did not open thrift port %d within %ss'
                           % (self.name, self.thrift_port,
                              SWITCH_START_TIMEOUT))

    def stop(self, deleteIntfs=True):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None
        try:
            super(P4Switch, self).stop(deleteIntfs)
        except Exception as exc:  # cleanup should never mask the real error
            warning('error stopping %s: %s\n' % (self.name, exc))

    def describe(self):
        return '%s (bmv2, thrift %d)' % (self.name, self.thrift_port)


class P4RuntimeSwitch(P4Switch):
    """bmv2 ``simple_switch_grpc`` as a Mininet switch.

    The P4 pipeline is pushed later by the P4Runtime controller.  Thrift is
    kept for runtime port add/remove only.
    """

    def __init__(self, name, grpc_port=None, **kwargs):
        if grpc_port is None:
            raise ValueError('%s: grpc_port is required' % name)
        # Switch.__init__ creates the loopback Intf and calls addIntf()
        # before P4Switch.__init__ gets a chance to set self.process.
        self.process = None
        self.grpc_port = int(grpc_port)
        super(P4RuntimeSwitch, self).__init__(name, **kwargs)

    def _port_busy(self, port):
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=0.5)
        except OSError:
            return False
        sock.close()
        return True

    def _run_cli(self, command):
        cli = shutil.which('simple_switch_CLI') or 'simple_switch_CLI'
        proc = subprocess.run([cli, '--thrift-port', str(self.thrift_port)],
                              input=command + '\n',
                              capture_output=True, text=True)
        output = proc.stdout + proc.stderr
        if proc.returncode != 0 or 'Error' in output:
            raise RuntimeError('%s: %s\n%s'
                               % (self.name, command, output.strip()))
        return output

    def attach_port(self, iface, port):
        subprocess.run(['ip', 'link', 'set', 'dev', iface, 'up'],
                       check=False, capture_output=True)
        self._run_cli('port_add %s %d' % (iface, port))

    def detach_port(self, port):
        self._run_cli('port_remove %d' % port)

    def addIntf(self, intf, port=None, moveIntfFn=None):
        if moveIntfFn is None:
            super(P4RuntimeSwitch, self).addIntf(intf, port)
        else:
            super(P4RuntimeSwitch, self).addIntf(intf, port, moveIntfFn)
        process = getattr(self, 'process', None)
        if (intf.name != 'lo' and process is not None
                and process.poll() is None):
            self.attach_port(intf.name, self.ports[intf])

    def start(self, controllers):
        for port, label in ((self.thrift_port, 'thrift'),
                            (self.grpc_port, 'grpc')):
            if self._port_busy(port):
                raise RuntimeError(
                    '%s port %d is already in use; stop leftover '
                    'simple_switch_grpc processes first' % (label, port))

        sock_path = self.notifications_addr[len('ipc://'):]
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass

        log = self._open_log()
        args = [self.sw_path]
        for port, intf in sorted(self.intfs.items()):
            if port == 0 or intf is None or intf.name == 'lo':
                continue
            args += ['-i', '%d@%s' % (port, intf.name)]
        args += ['--thrift-port', str(self.thrift_port)]
        args += ['--device-id', str(self.device_id)]
        args += ['--notifications-addr', self.notifications_addr]
        args += ['--log-file', self.log_file, '--log-level', self.log_level]
        if self.pcap_dump:
            args += (['--pcap=%s' % self.pcap_dump]
                     if isinstance(self.pcap_dump, str) else ['--pcap'])
        args += ['--no-p4', '--',
                 '--grpc-server-addr', '0.0.0.0:%d' % self.grpc_port]

        info('starting %s: %s\n' % (self.name, ' '.join(args)))
        try:
            self.process = subprocess.Popen(args, stdout=log, stderr=log,
                                            close_fds=True, cwd=os.getcwd())
        except FileNotFoundError:
            log.close()
            raise RuntimeError('找不到 %s，请先安装 bmv2' % self.sw_path)
        log.close()

        deadline = time.time() + SWITCH_START_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('%s exited with code %s, see %s'
                                   % (self.name, self.process.returncode,
                                      self.log_file))
            if self._port_busy(self.thrift_port) and self._port_busy(self.grpc_port):
                info('%s ready (thrift %d, grpc %d)\n'
                     % (self.name, self.thrift_port, self.grpc_port))
                return self.process
            time.sleep(0.1)

        self.stop()
        raise RuntimeError('%s did not open thrift+grpc ports within %ss'
                           % (self.name, SWITCH_START_TIMEOUT))

    def describe(self):
        return '%s (bmv2-grpc, thrift %d, grpc %d)' % (
            self.name, self.thrift_port, self.grpc_port)
