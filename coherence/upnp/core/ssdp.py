# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2005, Tim Potter <tpot@samba.org>
# Copyright 2006 John-Mark Gurney <gurney_j@resnet.uroegon.edu>
# Copyright (C) 2006 Fluendo, S.A. (www.fluendo.com).
# Copyright 2006,2007,2008,2009 Frank Scholz <coherence@beebits.net>
# Copyright 2018, Pol Canelles <canellestudi@gmail.com>

'''
:class:`SSDPServer`
-------------------

Implementation of a SSDP server under Twisted and EventDispatcher.
'''

import random
import socket
import time

import twisted.internet.threads
from twisted.internet import reactor
from twisted.internet import task
from twisted.internet.protocol import DatagramProtocol
from twisted.web.http import datetimeToString
from twisted.test import proto_helpers

from eventdispatcher import EventDispatcher, ListProperty

from coherence.upnp.core.utils import to_bytes, to_string
from coherence import log, SERVER_ID

SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'

# use the IPv6 site-local group for SSDP
# the link-local ff02::c would also be possible, but during implementation
# link-local addresses caused some trouble, now they are only used when needed
# also site-local multicast can be reached from link-local, ULA and global addresses
SSDP_ADDR6 = 'ff05::c'


class SSDPServer(EventDispatcher, DatagramProtocol, log.LogAble):
    '''
    A class implementing a SSDP server.

    .. versionchanged:: 0.9.0

        * Migrated from louie/dispatcher to EventDispatcher
        * The emitted events changed:

            - datagram_received => datagram_received
            - Coherence.UPnP.SSDP.new_device => new_device
            - Coherence.UPnP.SSDP.removed_device => removed_device
            - Coherence.UPnP.Log => log

        * Added new class variable `root_devices` which uses EventDispatcher's
          properties

    .. note:: The methods :meth:`notifyReceived` and :meth:`searchReceived`
              are called when the appropriate type of datagram is received by
              the server.
    '''

    logCategory = 'ssdp'

    root_devices = ListProperty([])
    '''A list of the detected root devices'''

    def __init__(self, test=False, interface='', ipv6=False):
        '''Initialize the SSDP server.'''
        log.LogAble.__init__(self)
        EventDispatcher.__init__(self)
        self.register_event(
            'datagram_received', 'new_device', 'removed_device', 'log'
        )
        self.known = {}
        self._callbacks = {}
        self.test = test
        self.ipv6 = ipv6
        if not self.test:
            # listen on IPv6 started with :: (see twisted UDP docs)
            self.port = reactor.listenMulticast(
                SSDP_PORT, self, listenMultiple=True, interface=('::' if ipv6 else interface),
            )

            self.port.joinGroup(SSDP_ADDR6 if ipv6 else SSDP_ADDR, interface=interface)
            if self.ipv6:
                # although the above call to joinGroup accepts IPv6 addresses , no IPv6 groups can be joined
                # but that's currently a twisted limitation
                # to join the group nevertheless, a second "dummy" socket is created alongside the twisted stuff
                # as the second socket binds on the same port and can actually join the group, twisted sees all datat too

                import struct
                import socket
                # create a new udp socket
                s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)

                # allow reuse is required, as the "dummy" and twisted socket are all within this program
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

                # get the link-local address for the listing interface
                import netifaces
                iface = netifaces.ifaddresses(interface)
                addresses = iface[netifaces.AF_INET6]
                ll_addr = None
                for address in addresses:
                    if address['addr'].startswith("fe80"):
                        ll_addr = address['addr']
                        break

                if ll_addr is None:
                    raise ValueError(f"The interface {interface} has no IPv6 link-local address, cannot continue without it")

                # netifaces returns the link-local address with a %interface scope qualifier which isn't accepted by bind
                # the struct returned by getaddrinfo holds a tuple with the address, scope_id etc. which can directly be used with bind
                socket_addr = socket.getaddrinfo(ll_addr, SSDP_PORT, socket.AF_INET6, socket.SOCK_DGRAM, socket.SOL_UDP)[0][4]
                s.bind(socket_addr)

                # during testing the wrong interface was used sometimes
                # set the given interface as the outgoing interface for multicast packets on the socket
                # (just to be sure that the multicast group subscription is done on the given interface)
                if_index = socket.if_nametoindex(interface)
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, if_index)

                # join the multicast group on the given interface
                group_bin = socket.inet_pton(socket.AF_INET6, SSDP_ADDR6)
                mreq = group_bin + struct.pack('@I', if_index)
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)

                # start a listen thread which only listens for data (and discards it)
                # required to keep the dummy socket alive and to stay in the multicast group
                self.dummy_socket = s
                twisted.internet.threads.deferToThread(f=self.listen_dummy)

            self.resend_notify_loop = task.LoopingCall(self.resendNotify)
            self.resend_notify_loop.start(777.0, now=False)

            self.check_valid_loop = task.LoopingCall(self.check_valid)
            self.check_valid_loop.start(333.0, now=False)

        self.active_calls = []

    def listen_dummy(self):
        """
        Listen on the dummy interface and discard any received data
        :return:
        """
        while self.dummy_socket is not None:
            data, sender = self.dummy_socket.recvfrom(1024)

            # sender will be None if the shutdown method is called on the socket
            if sender is None:
                break

    def shutdown(self):
        '''Shutdowns the server :class:`SSDPServer` and sends out
        the bye bye notifications via method :meth:`doByebye`.'''
        for call in reactor.getDelayedCalls():
            if call.func == self.send_it:
                call.cancel()
        if not self.test:
            if self.resend_notify_loop.running:
                self.resend_notify_loop.stop()
            if self.check_valid_loop.running:
                self.check_valid_loop.stop()
            for st in self.known:
                if self.known[st]['MANIFESTATION'] == 'local':
                    self.doByebye(st)

            # close the dummy socket when using IPv6
            if self.ipv6:
                # sometimes a "transport endpoint not connected" error is thrown,
                # but it's the shutdown and exit phase, so it doesn't really matter
                try:
                    self.dummy_socket.shutdown(socket.SHUT_RD)
                except OSError:
                    pass
                self.dummy_socket.close()
                self.dummy_socket = None

    def datagramReceived(self, data, xxx_todo_changeme):
        '''Handle a received multicast datagram.'''
        self.debug(f'datagramReceived: {data}')
        (host, port) = xxx_todo_changeme
        data = to_string(data)
        try:
            header, payload = data.split('\r\n\r\n')[:2]
        except ValueError as err:
            print(err)
            print('Arggg,', data)
            import pdb

            pdb.set_trace()

        lines = header.split('\r\n')
        cmd = lines[0].split(' ')
        lines = [x.replace(': ', ':', 1) for x in lines[1:]]
        lines = [x for x in lines if len(x) > 0]

        # TODO: Find  and fix where some of the header's keys are quoted.
        # This hack, allows to fix the quoted keys for the headers, introduced
        # at some point of the source code. I notice that the issue appears
        # when using FSStore plugin. But where?
        def fix_string(s, to_lower=True):
            for q in ['\'', '"']:
                while s.startswith(q):
                    s = s[1:]
            for q in ['\'', '"']:
                while s.endswith(q):
                    s = s[:-1]
            if to_lower:
                s = s.lower()
            return s

        headers = [x.split(':', 1) for x in lines]
        headers = dict(
            [
                (fix_string(x[0]), fix_string(x[1], to_lower=False))
                for x in headers
            ]
        )

        self.msg(f'SSDP command {cmd[0]} {cmd[1]} - from {host}:{port}')
        self.debug(f'with headers: {headers}')
        if cmd[0] == 'M-SEARCH' and cmd[1] == '*':
            # SSDP discovery
            self.discoveryRequest(headers, (host, port))
        elif cmd[0] == 'NOTIFY' and cmd[1] == '*':
            # SSDP presence
            self.notifyReceived(headers, (host, port))
        else:
            self.warning(f'Unknown SSDP command {cmd[0]} {cmd[1]}')

        # make raw data available
        # send out the signal after we had a chance to register the device
        self.dispatch_event('datagram_received', data, host, port)

    def register(
            self,
            manifestation,
            usn,
            st,
            location,
            server=SERVER_ID,
            cache_control='max-age=1800',
            silent=False,
            host=None,
    ):
        '''Register a service or device that this SSDP server will
        respond to.'''

        # in ipv6 mode and no ipv6 address given
        if self.ipv6 and ":" not in host:
            self.info(f"IPv6 mode: Skipping registration of IPv4 device {st} ({location})")
            return

        self.info(f'Registering {st} ({location}) -> {manifestation}')
        self.debug(f'\t-searching usn: {usn}')

        try:
            self.known[usn] = {}
            self.known[usn]['USN'] = usn
            self.known[usn]['LOCATION'] = location
            self.known[usn]['ST'] = st
            self.known[usn]['EXT'] = ''
            self.known[usn]['SERVER'] = server
            self.known[usn]['CACHE-CONTROL'] = cache_control

            self.known[usn]['MANIFESTATION'] = manifestation
            self.known[usn]['SILENT'] = silent
            self.known[usn]['HOST'] = host
            self.known[usn]['last-seen'] = time.time()

            self.msg(self.known[usn])
            self.debug(f'\t-self.known: {self.known}')

            if manifestation == 'local':
                self.doNotify(usn)

            if st == 'upnp:rootdevice':
                self.dispatch_event(
                    'new_device', device_type=st, infos=self.known[usn],
                )
                self.root_devices.append(usn)
                # self.callback('new_device', st, self.known[usn])
            # print('\t - ok all')
        except Exception as err:
            self.error(
                f'\t -> Error on registering service: {manifestation} '
                f'[error: "{err}"]'
            )

    def unRegister(self, usn):
        self.msg(f'Un-registering {usn}')
        st = self.known[usn]['ST']
        if st == 'upnp:rootdevice':
            self.dispatch_event(
                'removed_device', device_type=st, infos=self.known[usn],
            )
            # self.callback('removed_device', st, self.known[usn])
            self.root_devices.remove(usn)
        del self.known[usn]

    def isKnown(self, usn):
        return usn in self.known

    def notifyReceived(self, headers, xxx_todo_changeme1):
        '''Process a presence announcement.  We just remember the
        details of the SSDP service announced.'''
        (host, port) = xxx_todo_changeme1
        self.info(f'Notification from ({host},{port}) for {headers["nt"]}')
        self.debug(f'Notification headers: {headers}')

        if headers['nts'] == 'ssdp:alive':
            try:
                self.known[headers['usn']]['last-seen'] = time.time()
                self.debug(f'updating last-seen for {headers["usn"]}')
            except KeyError:
                self.register(
                    'remote',
                    headers['usn'],
                    headers['nt'],
                    headers['location'],
                    headers['server'],
                    headers['cache-control'],
                    host=host,
                )
        elif headers['nts'] == 'ssdp:byebye':
            if self.isKnown(headers['usn']):
                self.unRegister(headers['usn'])
        else:
            self.warning(
                f'Unknown subtype {headers["nts"]} '
                f'for notification type {headers["nt"]}'
            )
        self.dispatch_event(
            'log',
            'SSDP',
            host,
            f'Notify {headers["nts"]} for {headers["usn"]}',
        )

    def send_it(self, response, destination, delay, usn):
        self.info(
            f'send discovery response delayed by '
            f'{delay} for {usn} to {destination}'
        )
        try:
            # If some clients (e.g. android) insist on only using link-local addresses for multicast
            # the twisted socket will currently not give them the correct response, as it's not bound to a link-local address
            # (twisted on link-local address caused other issues during implementation)
            # Fortunately the dummy socket is bound on a link-local address on the given interface and, as UDP is stateless,
            # the response can just be delivered from the dummy socket
            if self.ipv6 and destination[0].startswith("fe80"):
                self.dummy_socket.sendto(to_bytes(response), destination)
            self.transport.write(to_bytes(response), destination)
        except (AttributeError, socket.error) as msg:
            self.exception(f'failure sending out datagram: {msg}')

    def discoveryRequest(self, headers, xxx_todo_changeme2):
        '''Process a discovery request.  The response must be sent to
        the address specified by (host, port).'''
        (host, port) = xxx_todo_changeme2
        self.info(
            f'Discovery request from ({host},{port}) for {headers["st"]}'
        )
        self.info(f'Discovery request for {headers["st"]}')

        self.dispatch_event(
            'log', 'SSDP', host, f'M-Search for {headers["st"]}',
        )

        if self.ipv6 and SSDP_ADDR6 not in str(headers["host"]).strip().lower():
            self.info(f"Ignoring recovery for host {host} as multicast group doesn't match: {headers['host']} vs {SSDP_ADDR6}")
            return

        # Do we know about this service?
        for i in list(self.known.values()):
            if i['MANIFESTATION'] == 'remote':
                continue
            if headers['st'] == 'ssdp:all' and i['SILENT'] is True:
                continue
            if i['ST'] == headers['st'] or headers['st'] == 'ssdp:all':
                response = [b'HTTP/1.1 200 OK']

                for k, v in list(i.items()):
                    if k == 'USN':
                        usn = v
                    if k not in ('MANIFESTATION', 'SILENT', 'HOST'):
                        response.append(f'{k}: {v}'.encode('ascii'))
                response.append(f'DATE: {datetimeToString()}'.encode('ascii'))

                response.extend((b'', b''))
                delay = random.randint(0, int(headers['mx']))

                reactor.callLater(
                    delay,
                    self.send_it,
                    b'\r\n'.join(response),
                    (host, port),
                    delay,
                    usn,
                )

    def doNotify(self, usn):
        '''Do notification'''

        if self.known[usn]['SILENT'] is True:
            return
        self.info(f'Sending alive notification for {usn}')
        # self.info(f'\t - self.known[usn]: {self.known[usn]}')

        resp = [
            'NOTIFY * HTTP/1.1',
            f'HOST: [{SSDP_ADDR6}]:{SSDP_PORT}' if self.ipv6 else f'HOST: {SSDP_ADDR}:{SSDP_PORT}',
            'NTS: ssdp:alive',
        ]
        stcpy = dict(iter(self.known[usn].items()))
        stcpy['NT'] = stcpy['ST']
        del stcpy['ST']
        del stcpy['MANIFESTATION']
        del stcpy['SILENT']
        del stcpy['HOST']
        del stcpy['last-seen']

        resp.extend([f'{k}: {v}' for k, v in stcpy.items()])
        resp.extend(('', ''))
        r = '\r\n'.join(resp).encode('ascii')
        self.debug(f'doNotify content {r}  [transport is: {self.transport}]')
        if not self.transport:
            try:
                self.warning(
                    'transport not initialized...'
                    + 'trying to initialize a FakeDatagramTransport'
                )
                self.transport = proto_helpers.FakeDatagramTransport()
            except Exception as er:
                self.error(f'Cannot initialize transport: {er}')
        try:
            self.transport.write(r, (SSDP_ADDR6 if self.ipv6 else SSDP_ADDR, SSDP_PORT))
        except (AttributeError, socket.error) as msg:
            self.info(f'failure sending out alive notification: {msg}')

    def doByebye(self, usn):
        '''Do byebye'''

        self.info(f'Sending byebye notification for {usn}')

        resp = [
            'NOTIFY * HTTP/1.1',
            f'HOST: [{SSDP_ADDR6}]:{SSDP_PORT}' if self.ipv6 else f'HOST: {SSDP_ADDR}:{SSDP_PORT}',
            'NTS: ssdp:byebye',
        ]
        try:
            stcpy = dict(iter(self.known[usn].items()))
            stcpy['NT'] = stcpy['ST']
            del stcpy['ST']
            del stcpy['MANIFESTATION']
            del stcpy['SILENT']
            del stcpy['HOST']
            del stcpy['last-seen']
            resp.extend([f'{k}: {v}' for k, v in stcpy.items()])
            resp.extend(('', ''))
            r = '\r\n'.join(resp).encode('ascii')
            self.debug(f'doByebye content {resp}')
            if not self.transport:
                self.warning(
                    'transport not initialized...'
                    + 'trying to initialize a FakeDatagramTransport'
                )
                self.transport = proto_helpers.FakeDatagramTransport()
                self.makeConnection(self.transport)
            try:
                self.transport.write(r, (SSDP_ADDR6 if self.ipv6 else SSDP_ADDR, SSDP_PORT))
            except (AttributeError, socket.error) as msg:
                self.info(f'failure sending out byebye notification: {msg}')
        except KeyError as msg:
            self.debug(f'error building byebye notification: {msg}')

    def resendNotify(self):
        for usn in self.known:
            if self.known[usn]['MANIFESTATION'] == 'local':
                self.doNotify(usn)

    def check_valid(self):
        '''
        Check if the discovered devices are still ok,
        or if we haven't received a new discovery response
        '''
        self.debug('Checking devices/services are still valid')
        removable = []
        for usn in self.known:
            if self.known[usn]['MANIFESTATION'] != 'local':
                _, expiry = self.known[usn]['CACHE-CONTROL'].split('=')
                expiry = int(expiry)
                now = time.time()
                last_seen = self.known[usn]['last-seen']
                self.debug(
                    f'Checking if {self.known[usn]["USN"]} is still valid - '
                    + f'last seen {last_seen} (+{expiry}), now {now}'
                )
                if last_seen + expiry + 30 < now:
                    self.debug(f'Expiring: {self.known[usn]}')
                    if self.known[usn]['ST'] == 'upnp:rootdevice':
                        self.dispatch_event(
                            'removed_device',
                            device_type=self.known[usn]['ST'],
                            infos=self.known[usn],
                        )
                    removable.append(usn)
        while len(removable) > 0:
            usn = removable.pop(0)
            del self.known[usn]

    def subscribe(self, name, callback):
        self._callbacks.setdefault(name, []).append(callback)

    def unsubscribe(self, name, callback):
        callbacks = self._callbacks.get(name, [])
        if callback in callbacks:
            callbacks.remove(callback)
        self._callbacks[name] = callbacks

    def callback(self, name, *args):
        for callback in self._callbacks.get(name, []):
            callback(*args)
