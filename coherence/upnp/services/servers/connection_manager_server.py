# -*- coding: utf-8 -*-

# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2006, Frank Scholz <coherence@beebits.net>

'''
Connection Manager service
==========================
'''
import time

from twisted.internet import task
from twisted.python import failure
from twisted.web import resource

from coherence.upnp.core import service
from coherence.upnp.core.DIDLLite import build_dlna_additional_info
from coherence.upnp.core.soap_service import UPnPPublisher
from coherence.upnp.core.soap_service import errorCode
from coherence.upnp.core.utils import to_string


class ConnectionManagerControl(service.ServiceControl, UPnPPublisher):
    def __init__(self, server):
        service.ServiceControl.__init__(self)
        # self.debug(f'ConnectionManagerControl.__init__: {server}')
        UPnPPublisher.__init__(self)
        self.service = server
        self.variables = server.get_variables()
        self.actions = server.get_actions()
        # self.debug(f'\t- variables: {self.variables}')
        # self.debug(f'\t- actions: {self.actions}')


class ConnectionManagerServer(service.ServiceServer, resource.Resource):
    logCategory = 'connection_manager_server'

    def __init__(self, device, backend=None):
        self.device = device
        if backend is None:
            backend = self.device.backend
        resource.Resource.__init__(self)
        service.ServiceServer.__init__(
            self, 'ConnectionManager', self.device.version, backend
        )

        self.control = ConnectionManagerControl(self)
        self.putChild(self.scpd_url, service.scpdXML(self, self.control))
        self.putChild(self.control_url, self.control)
        self.next_connection_id = 1
        self.next_avt_id = 1
        self.next_rcs_id = 1

        self.connections = {}

        self.set_variable(0, 'SourceProtocolInfo', '')
        self.set_variable(0, 'SinkProtocolInfo', '')
        self.set_variable(0, 'CurrentConnectionIDs', '')

        self.does_playcontainer = False
        try:
            if 'playcontainer-0-1' in backend.dlna_caps:
                self.does_playcontainer = True
        except AttributeError:
            pass

        self.remove_lingering_connections_loop = task.LoopingCall(
            self.remove_lingering_connections
        )
        self.remove_lingering_connections_loop.start(180.0, now=False)

    def release(self):
        self.remove_lingering_connections_loop.stop()

    def add_connection(
            self,
            RemoteProtocolInfo,
            Direction,
            PeerConnectionID,
            PeerConnectionManager,
    ):

        id = self.next_connection_id
        self.next_connection_id += 1

        avt_id = 0
        rcs_id = 0

        if self.device.device_type == 'MediaServer':
            self.connections[id] = {
                'ProtocolInfo': RemoteProtocolInfo,
                'Direction': Direction,
                'PeerConnectionID': PeerConnectionID,
                'PeerConnectionManager': PeerConnectionManager,
                'AVTransportID': avt_id,
                'RcsID': rcs_id,
                'Status': 'OK',
            }

        if self.device.device_type == 'MediaRenderer':
            '''
            this is the place to instantiate AVTransport and RenderingControl
            for this connection
            '''
            avt_id = self.next_avt_id
            self.next_avt_id += 1
            self.device.av_transport_server.create_new_instance(avt_id)
            rcs_id = self.next_rcs_id
            self.next_rcs_id += 1
            self.device.rendering_control_server.create_new_instance(rcs_id)
            self.connections[id] = {
                'ProtocolInfo': RemoteProtocolInfo,
                'Direction': Direction,
                'PeerConnectionID': PeerConnectionID,
                'PeerConnectionManager': PeerConnectionManager,
                'AVTransportID': avt_id,
                'RcsID': rcs_id,
                'Status': 'OK',
            }
            self.backend.current_connection_id = id

        csv_ids = ','.join([str(x) for x in self.connections])
        self.set_variable(0, 'CurrentConnectionIDs', csv_ids)
        return id, avt_id, rcs_id

    def remove_connection(self, id):
        if self.device.device_type == 'MediaRenderer':
            try:
                self.device.av_transport_server.remove_instance(
                    self.lookup_avt_id(id)
                )
                self.device.rendering_control_server.remove_instance(
                    self.lookup_rcs_id(id)
                )
                del self.connections[id]
            except Exception as e:
                self.warning(f'ConnectionManagerServer.remove_connection: {e}')
            self.backend.current_connection_id = None

        if self.device.device_type == 'MediaServer':
            del self.connections[id]

        csv_ids = ','.join([str(x) for x in self.connections])
        self.set_variable(0, 'CurrentConnectionIDs', csv_ids)

    def remove_lingering_connections(self):
        '''Check if we have a connection that hasn't a StateVariable change
        within the last 300 seconds, if so remove it.'''
        if self.device.device_type != 'MediaRenderer':
            return

        now = time.time()

        for id, connection in list(self.connections.items()):
            avt_id = connection['AVTransportID']
            rcs_id = connection['RcsID']
            avt_active = True
            rcs_active = True

            # print('remove_lingering_connections', id, avt_id, rcs_id)
            if avt_id > 0:
                avt_vars = self.device.av_transport_server.get_variables().get(
                    avt_id
                )
                if avt_vars:
                    avt_active = False
                    for variable in list(avt_vars.values()):
                        if variable.last_time_touched + 300 >= now:
                            avt_active = True
                            break
            if rcs_id > 0:
                rcs_vars = (
                    self.device.rendering_control_server.get_variables().get(
                        rcs_id
                    ),
                )
                if rcs_vars:
                    rcs_active = False
                    for variable in list(rcs_vars.values()):
                        if variable.last_time_touched + 300 >= now:
                            rcs_active = True
                            break
            if not avt_active and not rcs_active:
                self.remove_connection(id)

    def lookup_connection(self, id):
        return self.connections.get(id)

    def lookup_avt_id(self, id):
        try:
            return self.connections[id]['AVTransportID']
        except (ValueError, KeyError):
            return 0

    def lookup_rcs_id(self, id):
        try:
            return self.connections[id]['RcsID']
        except (ValueError, KeyError):
            return 0

    def listchilds(self, uri):
        uri = to_string(uri)
        cl = ''
        for c in self.children:
            c = to_string(c)
            cl += f'<li><a href={uri}/{c}>{c}</a></li>'
        return cl

    def render(self, request):
        html = f'''\
        <html>
        <head>
            <title>Cohen3 (ConnectionManagerServer)</title>
            <link rel="stylesheet" type="text/css" href="/styles/main.css" />
        </head>
        <h5>
            <img class="logo-icon" src="/server-images/coherence-icon.svg">
            </img>Root of the ConnectionManager</h5>
        <div class="list"><ul>{self.listchilds(request.uri)}</ul></div>
        </html>'''
        return html.encode('ascii')

    def set_variable(self, instance, variable_name, value, default=False, ipv6=False):
        if (
                variable_name == 'SourceProtocolInfo'
                or variable_name == 'SinkProtocolInfo'
        ):
            if isinstance(value, str) and len(value) > 0:
                value = [v.strip() for v in value.split(',')]
            without_dlna_tags = []

            def convert_value(v):
                if ipv6:
                    sp = v.split(":")
                    return f"{sp[0]}!{':'.join(sp[1:-2])}!{sp[-2]}!{sp[-1]}"
                else:
                    return v.replace(":", "!")

            val_rep = []
            for v in value:
                val_rep.append(convert_value(v))

            value = val_rep

            for v in value:
                protocol, network, content_format, additional_info = v.split(
                    '!'
                )
                if additional_info == '*':
                    without_dlna_tags.append(v)

            def with_some_tag_already_there(protocolinfo):
                (
                    protocol, network, content_format, additional_info,
                ) = protocolinfo.split('!')
                for v in value:
                    if "!" not in v:
                        v = convert_value(v)

                    (
                        v_protocol,
                        v_network,
                        v_content_format,
                        v_additional_info,
                    ) = v.split('!')
                    if (protocol, network, content_format) == (
                            v_protocol,
                            v_network,
                            v_content_format,
                    ) and v_additional_info != '*':
                        return True
                return False

            for w in without_dlna_tags:
                if not with_some_tag_already_there(w):
                    (
                        protocol, network, content_format, additional_info,
                    ) = w.split('!')
                    if variable_name == 'SinkProtocolInfo':
                        extra_info = build_dlna_additional_info(
                            content_format,
                            does_playcontainer=self.does_playcontainer,
                        )
                    else:
                        extra_info = build_dlna_additional_info(content_format)
                    value.append(
                        ':'.join(
                            (protocol, network, content_format, extra_info)
                        )
                    )

        service.ServiceServer.set_variable(
            self, instance, variable_name, value, default=default
        )

    def upnp_PrepareForConnection(self, *args, **kwargs):
        self.info('upnp_PrepareForConnection')
        ''' check if we really support that mimetype '''
        RemoteProtocolInfo = kwargs['RemoteProtocolInfo']
        ''' if we are a MR and this in not 'Input'
            then there is something strange going on
        '''
        Direction = kwargs['Direction']
        if (
                self.device.device_type == 'MediaRenderer'
                and Direction == 'Output'
        ):
            return failure.Failure(errorCode(702))
        if self.device.device_type == 'MediaServer' and Direction != 'Input':
            return failure.Failure(errorCode(702))
        # the InstanceID of the MS ?
        PeerConnectionID = kwargs['PeerConnectionID']
        # ???
        PeerConnectionManager = kwargs['PeerConnectionManager']
        local_protocol_infos = None
        if self.device.device_type == 'MediaRenderer':
            local_protocol_infos = self.get_variable('SinkProtocolInfo').value
        if self.device.device_type == 'MediaServer':
            local_protocol_infos = self.get_variable(
                'SourceProtocolInfo'
            ).value
        self.debug(
            f'ProtocalInfos: {RemoteProtocolInfo} -- {local_protocol_infos}'
        )

        try:
            (
                remote_protocol, remote_network, remote_content_format, _,
            ) = RemoteProtocolInfo.split(':')
        except Exception as e:
            self.warning(
                f'unable to process RemoteProtocolInfo '
                f'{RemoteProtocolInfo} [error: {e}]'
            )
            return failure.Failure(errorCode(701))

        for protocol_info in local_protocol_infos.split(','):
            # print(remote_protocol,remote_network,remote_content_format)
            # print(protocol_info)
            (
                local_protocol, local_network, local_content_format, _,
            ) = protocol_info.split(
                ':'
            )
            # print(local_protocol,local_network,local_content_format)
            if (
                    (
                            remote_protocol == local_protocol
                            or remote_protocol == '*'
                            or local_protocol == '*'
                    )
                    and (
                    remote_network == local_network
                    or remote_network == '*'
                    or local_network == '*'
            )
                    and (
                    remote_content_format == local_content_format
                    or remote_content_format == '*'
                    or local_content_format == '*'
            )
            ):
                connection_id, avt_id, rcs_id = self.add_connection(
                    RemoteProtocolInfo,
                    Direction,
                    PeerConnectionID,
                    PeerConnectionManager,
                )
                return {
                    'ConnectionID': connection_id,
                    'AVTransportID': avt_id,
                    'RcsID': rcs_id,
                }

        return failure.Failure(errorCode(701))

    def upnp_ConnectionComplete(self, *args, **kwargs):
        ConnectionID = int(kwargs['ConnectionID'])
        ''' remove this ConnectionID
            and the associated instances @ AVTransportID and RcsID
        '''
        self.remove_connection(ConnectionID)
        return {}

    def upnp_GetCurrentConnectionInfo(self, *args, **kwargs):
        ConnectionID = int(kwargs['ConnectionID'])
        ''' return for this ConnectionID
            the associated InstanceIDs @ AVTransportID and RcsID
            ProtocolInfo
            PeerConnectionManager
            PeerConnectionID
            Direction
            Status

            or send a 706 if there isn't such a ConnectionID
        '''
        connection = self.lookup_connection(ConnectionID)
        if connection is None:
            return failure.Failure(errorCode(706))
        else:
            return {
                'AVTransportID': connection['AVTransportID'],
                'RcsID': connection['RcsID'],
                'ProtocolInfo': connection['ProtocolInfo'],
                'PeerConnectionManager': connection['PeerConnectionManager'],
                'PeerConnectionID': connection['PeerConnectionID'],
                'Direction': connection['Direction'],
                'Status': connection['Status'],
            }
