from oslo.config import cfg
from neutron.common import log
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants as plugin_const
from neutron.common.exceptions import InvalidConfigurationOption
from neutron.services.loadbalancer import constants as lb_const
from neutron.services.loadbalancer.drivers.f5.bigip \
                                     import agent_manager as am
from f5.bigip import bigip
from f5.common import constants as f5const
from f5.bigip import exceptions as f5ex
from f5.bigip import bigip_interfaces

from eventlet import greenthread

import random
import urllib2
import netaddr
import datetime
import time

LOG = logging.getLogger(__name__)
NS_PREFIX = 'qlbaas-'
APP_COOKIE_RULE_PREFIX = 'app_cookie_'
RPS_THROTTLE_RULE_PREFIX = 'rps_throttle_'

__VERSION__ = '0.1.1'

OPTS = [
    cfg.StrOpt(
        'icontrol_hostname',
        help=_('The hostname (name or IP address) to use for iControl access'),
    ),
    cfg.StrOpt(
        'icontrol_username',
        default='admin',
        help=_('The username to use for iControl access'),
    ),
    cfg.StrOpt(
        'icontrol_password',
        default='admin',
        secret=True,
        help=_('The password to use for iControl access'),
    ),
    cfg.IntOpt(
        'icontrol_connection_retry_interval',
        default=10,
        help=_('How many seconds to wait between retry connection attempts'),
    )
]


def serialized(method_name):
    def real_serialized(method):
        """Decorator to serialize calls to configure via iControl"""
        def wrapper(*args, **kwargs):
            instance = args[0]
            my_request_id = random.random()
            instance.service_queue.append(my_request_id)
            waitsecs = .05
            while instance.service_queue[0] != my_request_id:
                LOG.debug('%s request %s is blocking for %s secs - queue depth: %d'
                          % (str(method_name), my_request_id,
                             waitsecs, len(instance.service_queue)))
                greenthread.sleep(waitsecs)
                if waitsecs < 1:
                    waitsecs = waitsecs * 2
            else:
                LOG.debug('%s request %s is running with queue depth: %d'
                          % (str(method_name), my_request_id,
                             len(instance.service_queue)))
            try:
                start_time = time.time()
                result = method(*args, **kwargs)
                end_time = time.time()
                LOG.debug('%s request %s ran in time: %s'
                          % (str(method_name), my_request_id,
                             str(end_time - start_time)))
            except:
                LOG.error('%s request %s FAILED'
                          % (str(method_name), my_request_id))
                raise
            finally:
                instance.service_queue.pop(0)
            return result
        return wrapper
    return real_serialized


class iControlDriver(object):

    # containers
    __bigips = {}
    __traffic_groups = []

    # mappings
    __vips_to_traffic_group = {}
    __gw_to_traffic_group = {}

    # scheduling counts
    __vips_on_traffic_groups = {}
    __gw_on_traffic_groups = {}

    __service_locks = {}

    def __init__(self, conf):
        self.conf = conf
        self.conf.register_opts(OPTS)
        self.connected = False
        self.service_queue = []

        self._init_connection()

        LOG.debug(_('iControlDriver initialized to %d hosts with username:%s'
                    % (len(self.__bigips), self.username)))
        self.interface_mapping = {}
        self.tagging_mapping = {}

        mappings = str(self.conf.f5_external_physical_mappings).split(",")
        # map format is   phynet:interface:tagged
        for maps in mappings:
            intmap = maps.split(':')
            intmap[0] = str(intmap[0]).strip()
            self.interface_mapping[intmap[0]] = str(intmap[1]).strip()
            self.tagging_mapping[intmap[0]] = str(intmap[2]).strip()
            LOG.debug(_('physical_network %s = BigIP interface %s, tagged %s'
                        % (intmap[0], intmap[1], intmap[2])
                        ))

    @serialized('sync')
    @am.is_connected
    @log.log
    def sync(self, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('create_vip')
    @am.is_connected
    @log.log
    def create_vip(self, vip, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('update_vip')
    @am.is_connected
    @log.log
    def update_vip(self, old_vip, vip, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('delete_vip')
    @am.is_connected
    @log.log
    def delete_vip(self, vip, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('create_pool')
    @am.is_connected
    @log.log
    def create_pool(self, pool, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('update_pool')
    @am.is_connected
    @log.log
    def update_pool(self, old_pool, pool, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('delete_pool')
    @am.is_connected
    @log.log
    def delete_pool(self, pool, service):
        self._assure_service(service)

    @serialized('create_member')
    @am.is_connected
    @log.log
    def create_member(self, member, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('update_member')
    @am.is_connected
    @log.log
    def update_member(self, old_member, member, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('delete_member')
    @am.is_connected
    @log.log
    def delete_member(self, member, service):
        self._assure_service_networks(service)
        self._assure_service(service)

    @serialized('create_pool_health_monitor')
    @am.is_connected
    @log.log
    def create_pool_health_monitor(self, health_monitor, pool, service):
        self._assure_service(service)
        return True

    @serialized('update_health_monitor')
    @am.is_connected
    @log.log
    def update_health_monitor(self, old_health_monitor,
                              health_monitor, pool, service):
        self._assure_service(service)
        return True

    @serialized('delete_pool_health_monitor')
    @am.is_connected
    @log.log
    def delete_pool_health_monitor(self, health_monitor, pool, service):
        # Two behaviors of the plugin dictate our behavior here.
        # 1. When a plug-in deletes a monitor that is not being
        # used by a pool, it does not notify the drivers. Therefore,
        # we need to aggresively remove monitors that are not in use.
        # 2. When a plug-in deletes a monitor which is being
        # used by one or more pools, it calls delete_pool_health_monitor
        # against the driver that owns each pool, but it does not
        # set status to PENDING_DELETE in the health_monitors_status
        # list for the pool monitor. This may be a bug or perhaps this
        # is intended to be a synchronous process.
        #
        # In contrast, when a pool monitor association is deleted, the 
        # PENDING DELETE status is set properly, so this code will
        # run unnecessarily in that case. 
        for status in service['pool']['health_monitors_status']:
            if status['monitor_id'] == health_monitor['id']:
                # Signal to our own code that we should delete the
                # pool health monitor. The plugin should do this.
                status['status'] = plugin_const.PENDING_DELETE

        self._assure_service(service)
        return True

    @serialized('get_stats')
    @am.is_connected
    def get_stats(self, service):
        # use pool stats because the pool_id is the
        # the service definition... not the vip
        #
        stats = {}

        bigip = self._get_bigip()

        # It appears that stats are collected for pools in a pending delete
        # state which means that if those messages are queued (or delayed)
        # it can result in the process of a stats request after the pool
        # and tenant are long gone
        if not bigip.system.folder_exists( \
                                '/uuid_' + service['pool']['tenant_id']):
            return None

        bigip_stats = bigip.pool.get_statistics(name=service['pool']['id'],
                                          folder=service['pool']['tenant_id'])
        if 'STATISTIC_SERVER_SIDE_BYTES_IN' in bigip_stats:
            stats[lb_const.STATS_IN_BYTES] = \
              bigip_stats['STATISTIC_SERVER_SIDE_BYTES_IN']
            stats[lb_const.STATS_OUT_BYTES] = \
              bigip_stats['STATISTIC_SERVER_SIDE_BYTES_OUT']
            stats[lb_const.STATS_ACTIVE_CONNECTIONS] = \
              bigip_stats['STATISTIC_SERVER_SIDE_CURRENT_CONNECTIONS']
            stats[lb_const.STATS_TOTAL_CONNECTIONS] = \
              bigip_stats['STATISTIC_SERVER_SIDE_TOTAL_CONNECTIONS']

            # need to get members for this pool and update their status
            states = bigip.pool.get_members_monitor_status(
                                        name=service['pool']['id'],
                                        folder=service['pool']['tenant_id'])
            # format is data = {'members': { uuid:{'status':'state1'},
            #                             uuid:{'status':'state2'}} }
            members = {'members': {}}
            if hasattr(service, 'members'):
                for member in service['members']:
                    for state in states:
                        if state == 'MONITOR_STATUS_UP':
                            members['members'][member['id']] = 'ACTIVE'
                        else:
                            members['members'][member['id']] = 'DOWN'
            stats['members'] = members

            return stats
        else:
            return None

    @log.log
    def remove_orphans(self, known_pool_ids):
        raise NotImplementedError()

    @log.log
    def non_connected(self):
        now = datetime.datetime.now()
        if (now - self.__last_connect_attempt).total_seconds()  \
                         > self.conf.icontrol_connection_retry_interval:
            self.connected = False
            self._init_connection()

    # A context used for storing information used to sync
    # the service request with the current configuration
    class AssureServiceContext:
        def __init__(self):
            self.device_group = None
            self.assured_subnets = []
            self.deleted_subnets = []
            self.delete_vip_service_networks = False
            self.delete_member_service_networks = False

    @log.log
    def _assure_service(self, service):
        ctx = self.AssureServiceContext()

        self.set_monitor_delete_if_pool_delete(service)
        bigip = self._get_bigip()
        self.assure_pool_create(service, bigip, ctx)
        self.assure_pool_monitors(service, bigip, ctx)
        self.assure_members(service, bigip, ctx)
        self.assure_vip(service, bigip, ctx)
        self.assure_pool_delete(service, bigip, ctx)
        self.assure_vip_network_delete(service, bigip, ctx)
        self.assure_member_network_delete(service, bigip, ctx)
        self.assure_tenant_cleanup(service, bigip, ctx)

    @log.log
    def set_monitor_delete_if_pool_delete(self, service):
        if service['pool']['status'] == plugin_const.PENDING_DELETE:
            # Everything needs to be go with the pool, so overwrite
            # service state to appropriately remove all elements
            service['vip']['status'] = plugin_const.PENDING_DELETE
            for member in service['members']:
                member['status'] = plugin_const.PENDING_DELETE
            for monitor in service['pool']['health_monitors_status']:
                monitor['status'] = plugin_const.PENDING_DELETE

    #
    # Provision Pool - Create/Update
    #
    @log.log
    def assure_pool_create(self, service, bigip, ctx):
        if not service['pool']['status'] == plugin_const.PENDING_DELETE:
            if not bigip.pool.create(name=service['pool']['id'],
                              lb_method=service['pool']['lb_method'],
                              description=service['pool']['name'] + \
                              ':' + service['pool']['description'],
                              folder=service['pool']['tenant_id']):

                if service['pool']['status'] == \
                                               plugin_const.PENDING_UPDATE:
                    # make sure pool attributes are correct
                    bigip.pool.set_lb_method(name=service['pool']['id'],
                                    lb_method=service['pool']['lb_method'])
                    bigip.pool.set_description(name=service['pool']['id'],
                                    description=service['pool']['name'] + \
                                    ':' + service['pool']['description'])
                    self.plugin_rpc.update_pool_status(
                                    service['pool']['id'],
                                    status=plugin_const.ACTIVE,
                                    status_description='pool updated'
                                  )
            else:
                self.plugin_rpc.update_pool_status(
                                        service['pool']['id'],
                                        status=plugin_const.ACTIVE,
                                        status_description='pool created'
                                       )

    #
    # Provision Health Monitors - Create/Update
    #
    def assure_pool_monitors(self, service, bigip, ctx):
        # Current monitors on the pool according to BigIP
        existing_monitors = bigip.pool.get_monitors(
                                name=service['pool']['id'],
                                folder=service['pool']['tenant_id'])
        LOG.debug(_("Pool: %s before assurance has monitors: %s"
                    % (service['pool']['id'], existing_monitors)))

        health_monitors_status = {}
        for monitor in service['pool']['health_monitors_status']:
            health_monitors_status[monitor['monitor_id']] = \
                                                       monitor['status']

        # Current monitor associations according to Neutron
        for monitor in service['health_monitors']:
            if monitor['id'] in health_monitors_status and \
               health_monitors_status[monitor['id']] == \
                                            plugin_const.PENDING_DELETE:
                bigip.pool.remove_monitor(
                                      name=service['pool']['id'],
                                      monitor_name=monitor['id'],
                                      folder=service['pool']['tenant_id']
                                    )
                self.plugin_rpc.health_monitor_destroyed(
                                      health_monitor_id=monitor['id'],
                                      pool_id=service['pool']['id'])
                # not sure if the monitor might be in use
                try:
                    bigip.monitor.delete(
                                  name=monitor['id'],
                                  folder=service['pool']['tenant_id'])
                except:
                    pass
            else:
                timeout = int(monitor['max_retries']) \
                        * int(monitor['timeout'])
                bigip.monitor.create(name=monitor['id'],
                                     mon_type=monitor['type'],
                                     interval=monitor['delay'],
                                     timeout=timeout,
                                     send_text=None,
                                     recv_text=None,
                                     folder=monitor['tenant_id'])
                # make sure monitor attributes are correct
                bigip.monitor.set_interval(name=monitor['id'],
                                     interval=monitor['delay'],
                                     folder=monitor['tenant_id'])
                bigip.monitor.set_timeout(name=monitor['id'],
                                          timeout=timeout,
                                          folder=monitor['tenant_id'])

                if monitor['type'] == 'HTTP' or monitor['type'] == 'HTTPS':
                    if 'url_path' in monitor:
                        send_text = "GET " + monitor['url_path'] + \
                                                        " HTTP/1.0\\r\\n\\r\\n"
                    else:
                        send_text = "GET / HTTP/1.0\\r\\n\\r\\n"

                    if 'expected_codes' in monitor:
                        try:
                            if monitor['expected_codes'].find(",") > 0:
                                status_codes = \
                                    monitor['expected_codes'].split(',')
                                recv_text = "HTTP/1\.(0|1) ("
                                for status in status_codes:
                                    int(status)
                                    recv_text += status + "|"
                                recv_text = recv_text[:-1]
                                recv_text += ")"
                            elif monitor['expected_codes'].find("-") > 0:
                                status_range = \
                                    monitor['expected_codes'].split('-')
                                start_range = status_range[0]
                                int(start_range)
                                stop_range = status_range[1]
                                int(stop_range)
                                recv_text = \
                                    "HTTP/1\.(0|1) [" + \
                                    start_range + "-" + \
                                    stop_range + "]"
                            else:
                                int(monitor['expected_codes'])
                                recv_text = "HTTP/1\.(0|1) " + \
                                            monitor['expected_codes']
                        except:
                            LOG.error(_(
                            "invalid monitor expected_codes %s, setting to 200"
                            % monitor['expected_codes']))
                            recv_text = "HTTP/1\.(0|1) 200"
                    else:
                        recv_text = "HTTP/1\.(0|1) 200"

                    LOG.debug('setting monitor send: %s, receive: %s'
                              % (send_text, recv_text))

                    bigip.monitor.set_send_string(name=monitor['id'],
                                                  send_text=send_text,
                                                  folder=monitor['tenant_id'])
                    bigip.monitor.set_recv_string(name=monitor['id'],
                                                  recv_text=recv_text,
                                                  folder=monitor['tenant_id'])

                bigip.pool.add_monitor(name=service['pool']['id'],
                                       monitor_name=monitor['id'],
                                       folder=service['pool']['tenant_id'])

                self.plugin_rpc.update_health_monitor_status(
                                    pool_id=service['pool']['id'],
                                    health_monitor_id=monitor['id'],
                                    status=plugin_const.ACTIVE,
                                    status_description='monitor active'
                                 )

            if monitor['id'] in existing_monitors:
                existing_monitors.remove(monitor['id'])

        LOG.debug(_("Pool: %s removing monitors %s"
                    % (service['pool']['id'], existing_monitors)))
        # get rid of monitors no longer in service definition
        for monitor in existing_monitors:
            bigip.monitor.delete(name=monitor,
                                 folder=service['pool']['tenant_id'])

    #
    # Provision Members - Create/Update
    #
    def assure_members(self, service, bigip, ctx):
        # Current members on the BigIP
        existing_members = bigip.pool.get_members(
                                name=service['pool']['id'],
                                folder=service['pool']['tenant_id'])
        LOG.debug(_("Pool: %s before assurance has membership: %s"
                    % (service['pool']['id'], existing_members)))

        # Flag if we need to change the pool's LB method to
        # include weighting by the ratio attribute
        using_ratio = False
        # Members according to Neutron
        for member in service['members']:
            LOG.debug(_("Pool %s assuring member %s:%d - status %s"
                        % (service['pool']['id'],
                           member['address'],
                           member['protocol_port'],
                           member['status'])
                        ))

            ip_address = member['address']
            if member['network']['shared']:
                ip_address = ip_address + '%0'

            # Delete those pending delete
            if member['status'] == plugin_const.PENDING_DELETE:
                bigip.pool.remove_member(name=service['pool']['id'],
                                  ip_address=ip_address,
                                  port=int(member['protocol_port']),
                                  folder=service['pool']['tenant_id'])
                # avoids race condition:
                # deletion of pool member objects must sync before we
                # remove the selfip from the peer bigips.
                self.sync_if_clustered(bigip, ctx)
                try:
                    self.plugin_rpc.member_destroyed(member['id'])
                except Exception as e:
                    LOG.error(_("Plugin delete member %s error: %s"
                                % (member['id'], e.message)
                                ))
                    pass
                ctx.delete_member_service_networks = True
            else:
                if bigip.pool.add_member(name=service['pool']['id'],
                                      ip_address=ip_address,
                                      port=int(member['protocol_port']),
                                      folder=service['pool']['tenant_id']):
                    LOG.debug(_("Pool: %s added member: %s:%d"
                    % (service['pool']['id'],
                       member['address'],
                       member['protocol_port'])))
                    self.plugin_rpc.update_member_status(
                                        member['id'],
                                        status=plugin_const.ACTIVE,
                                        status_description='member created'
                                       )
                if member['status'] == plugin_const.PENDING_CREATE or \
                   member['status'] == plugin_const.PENDING_UPDATE:
                    # Is it enabled or disabled?
                    if member['admin_state_up']:
                        bigip.pool.enable_member(name=member['id'],
                                    ip_address=ip_address,
                                    port=int(member['protocol_port']),
                                    folder=service['pool']['tenant_id'])
                    else:
                        bigip.pool.disable_member(name=member['id'],
                                    ip_address=ip_address,
                                    port=int(member['protocol_port']),
                                    folder=service['pool']['tenant_id'])
                    # Do we have weights for ratios?
                    if member['weight'] > 0:
                        bigip.pool.set_member_ratio(
                                    name=service['pool']['id'],
                                    ip_address=ip_address,
                                    port=int(member['protocol_port']),
                                    ratio=int(member['weight']),
                                    folder=service['pool']['tenant_id']
                                   )
                        using_ratio = True

                    self.plugin_rpc.update_member_status(
                                    member['id'],
                                    status=plugin_const.ACTIVE,
                                    status_description='member updated'
                                   )
                ctx.assured_subnets.append(member['subnet']['id'])

            # Remove them from the one's BigIP needs to
            # handle.. leaving on those that are needed to
            # delete from the BigIP
            for existing_member in existing_members:
                if member['address'] == existing_member['addr'] and \
                   member['protocol_port'] == existing_member['port']:
                    existing_members.remove(existing_member)
                    LOG.debug(_("Pool: %s assured member: %s:%d"
                    % (service['pool']['id'],
                       member['address'],
                       member['protocol_port'])))

        # remove any members which are no longer in the service
        LOG.debug(_("Pool: %s removing members %s"
                    % (service['pool']['id'], existing_members)))
        for need_to_delete in existing_members:
            bigip.pool.remove_member(
                                 name=service['pool']['id'],
                                 ip_address=need_to_delete['addr'],
                                 port=int(need_to_delete['port']),
                                 folder=service['pool']['tenant_id']
                                )
        # if members are using weights, change the LB to RATIO
        if using_ratio:
            LOG.debug(_("Pool: %s changing to ratio based lb"
                    % service['pool']['id']))
            bigip.pool.set_lb_method(
                                name=service['pool']['id'],
                                lb_method='RATIO',
                                folder=service['pool']['tenant_id'])

            self.plugin_rpc.update_pool_status(
                            service['pool']['id'],
                            status=plugin_const.ACTIVE,
                            status_description='pool now using ratio lb'
                           )

    def assure_vip(self, service, bigip, ctx):
        if 'id' in service['vip']:
            #
            # Provision Virtual Service - Create/Update
            #
            vlan_name = self._get_vlan_name(service['vip']['network'])
            ip_address = service['vip']['address']
            if service['vip']['network']['shared']:
                vlan_name = '/Common/' + vlan_name
                ip_address = ip_address + "%0"
            if service['vip']['status'] == plugin_const.PENDING_DELETE:
                LOG.debug(_('Vip: deleting VIP %s' % service['vip']['id']))
                bigip.virtual_server.remove_and_delete_persist_profile(
                                        name=service['vip']['id'],
                                        folder=service['vip']['tenant_id'])
                bigip.virtual_server.delete(name=service['vip']['id'],
                                        folder=service['vip']['tenant_id'])

                bigip.rule.delete(name=RPS_THROTTLE_RULE_PREFIX + \
                                  service['vip']['id'],
                                  folder=service['vip']['tenant_id'])
                # avoids race condition:
                # deletion of vip address must sync before we
                # remove the selfip from the peer bigips.
                self.sync_if_clustered(bigip, ctx)

                if service['vip']['id'] in self.__vips_to_traffic_group:
                    tg = self.__vips_to_traffic_group[service['vip']['id']]
                    self.__vips_on_traffic_groups[tg] = \
                                  self.__vips_on_traffic_groups[tg] - 1
                    del(self.__vips_to_traffic_groups[
                                                     service['vip']['id']])
                ctx.delete_vip_service_networks = True
                try:
                    self.plugin_rpc.vip_destroyed(service['vip']['id'])
                except Exception as e:
                    LOG.error(_("Plugin delete vip %s error: %s"
                                % (service['vip']['id'], e.message)
                                ))
            else:
                tg = self._get_least_vips_traffic_group()

                snat_pool_name = None
                if self.conf.f5_snat_mode and \
                   self.conf.f5_snat_addresses_per_subnet > 0:
                        snat_pool_name = bigip_interfaces.decorate_name(
                                    service['pool']['tenant_id'],
                                    service['pool']['tenant_id'])

                # This is where you could decide to use a fastl4
                # or a standard virtual server.  The problem
                # is making sure that if someone updates the
                # vip protocol or a session persistence that
                # required you change virtual service types
                # would have to make sure a virtual of the
                # wrong type does not already exist or else
                # delete it first. That would cause a service
                # disruption. It would be better if the
                # specification did not allow you to update
                # L7 attributes if you already created a
                # L4 service.  You should have to delete the
                # vip and then create a new one.  That way
                # the end user expects the service outage.

                #virtual_type = 'fastl4'
                #if 'protocol' in service['vip']:
                #    if service['vip']['protocol'] == 'HTTP' or \
                #       service['vip']['protocol'] == 'HTTPS':
                #        virtual_type = 'standard'
                #if 'session_persistence' in service['vip']:
                #    if service['vip']['session_persistence'] == \
                #       'APP_COOKIE':
                #        virtual_type = 'standard'

                # Hard code to standard until we decide if we
                # want to handle the check/delete before create
                # and document the service outage associated
                # with deleting a virtual service. We'll leave
                # the steering logic for create in place.
                # Be aware the check/delete before create
                # is not in the logic below because it means
                # another set of interactions with the device
                # we don't need unless we decided to handle
                # shifting from L4 to L7 or from L7 to L4

                virtual_type = 'standard'

                if virtual_type == 'standard':
                    if bigip.virtual_server.create(
                                    name=service['vip']['id'],
                                    ip_address=ip_address,
                                    mask='255.255.255.255',
                                    port=int(service['vip']['protocol_port']),
                                    protocol=service['vip']['protocol'],
                                    vlan_name=vlan_name,
                                    traffic_group=tg,
                                    use_snat=self.conf.f5_snat_mode,
                                    snat_pool=snat_pool_name,
                                    folder=service['pool']['tenant_id']
                                   ):
                        # created update driver traffic group mapping
                        tg = bigip.virtual_server.get_traffic_group(
                                        name=service['vip']['ip'],
                                        folder=service['pool']['tenant_id'])
                        self.__vips_to_traffic_group[service['vip']['ip']] = tg
                        self.plugin_rpc.update_vip_status(
                                            service['vip']['id'],
                                            status=plugin_const.ACTIVE,
                                            status_description='vip created'
                                           )
                else:
                    if bigip.virtual_server.create_fastl4(
                                    name=service['vip']['id'],
                                    ip_address=ip_address,
                                    mask='255.255.255.255',
                                    port=int(service['vip']['protocol_port']),
                                    protocol=service['vip']['protocol'],
                                    vlan_name=vlan_name,
                                    traffic_group=tg,
                                    use_snat=self.conf.f5_snat_mode,
                                    snat_pool=snat_pool_name,
                                    folder=service['pool']['tenant_id']
                                   ):
                        # created update driver traffic group mapping
                        tg = bigip.virtual_server.get_traffic_group(
                                        name=service['vip']['ip'],
                                        folder=service['pool']['tenant_id'])
                        self.__vips_to_traffic_group[service['vip']['ip']] = tg
                        self.plugin_rpc.update_vip_status(
                                            service['vip']['id'],
                                            status=plugin_const.ACTIVE,
                                            status_description='vip created'
                                           )

                if service['vip']['status'] == \
                        plugin_const.PENDING_CREATE or \
                   service['vip']['status'] == \
                        plugin_const.PENDING_UPDATE:

                    bigip.virtual_server.set_description(
                                    name=service['vip']['id'],
                                    description=service['vip']['name'] + \
                                    ':' + service['vip']['description'])
                    bigip.virtual_server.set_pool(
                                    name=service['vip']['id'],
                                    pool_name=service['pool']['id'],
                                    folder=service['pool']['tenant_id'])
                    if service['vip']['admin_state_up']:
                        bigip.virtual_server.enable_virtual_server(
                                    name=service['vip']['id'],
                                    folder=service['pool']['tenant_id'])
                    else:
                        bigip.virtual_server.disable_virtual_server(
                                    name=service['vip']['id'],
                                    folder=service['pool']['tenant_id'])

                    if 'session_persistence' in service['vip']:
                        # branch on persistence type
                        persistence_type = \
                               service['vip']['session_persistence']['type']

                        if persistence_type == 'SOURCE_IP':
                            # add source_addr persistence profile
                            LOG.debug('adding source_addr primary persistence')
                            bigip.virtual_server.set_persist_profile(
                                name=service['vip']['id'],
                                profile_name='/Common/source_addr',
                                folder=service['vip']['tenant_id'])
                        elif persistence_type == 'HTTP_COOKIE':
                            # HTTP cookie persistence requires an HTTP profile
                            LOG.debug('adding http profile and primary cookie persistence')
                            bigip.virtual_server.add_profile(
                                name=service['vip']['id'],
                                profile_name='/Common/http',
                                folder=service['vip']['tenant_id'])
                            # add standard cookie persistence profile
                            bigip.virtual_server.set_persist_profile(
                                name=service['vip']['id'],
                                profile_name='/Common/cookie',
                                folder=service['vip']['tenant_id'])
                            if service['pool']['lb_method'] == 'SOURCE_IP':
                                bigip.virtual_server.set_fallback_persist_profile(
                                    name=service['vip']['id'],
                                    profile_name='/Common/source_addr',
                                    folder=service['vip']['tenant_id'])
                        elif persistence_type == 'APP_COOKIE':
                            # application cookie persistence requires
                            # an HTTP profile
                            LOG.debug('adding http profile and primary universal persistence')
                            bigip.virtual_server.virtual_server.add_profile(
                                name=service['vip']['id'],
                                profile_name='/Common/http',
                                folder=service['vip']['tenant_id'])
                            # make sure they gave us a cookie_name
                            if 'cookie_name' in \
                          service['vip']['session_persistence']['cookie_name']:
                                cookie_name = \
                          service['vip']['session_persistence']['cookie_name']
                                # create and add irule to capture cookie
                                # from the service response.
                                rule_definition = \
                          self._create_app_cookie_persist_rule(cookie_name)
                                # try to create the irule
                                if bigip.rule.create(
                                        name=APP_COOKIE_RULE_PREFIX + \
                                             service['vip']['id'],
                                        rule_definition=rule_definition,
                                        folder=service['vip']['tenant_id']):
                                    # create universal persistence profile
                                    bigip.virtual_server.create_uie_profile(
                                        name=APP_COOKIE_RULE_PREFIX + \
                                              service['vip']['id'],
                                        rule_name=APP_COOKIE_RULE_PREFIX + \
                                                  service['vip']['id'],
                                        folder=service['vip']['tenant_id'])
                                # set persistence profile
                                bigip.virtual_server.set_persist_profile(
                                        name=service['vip']['id'],
                                        profile_name=APP_COOKIE_RULE_PREFIX + \
                                                 service['vip']['id'],
                                        folder=service['vip']['tenant_id'])
                                if service['pool']['lb_method'] == 'SOURCE_IP':
                                    bigip.virtual_server.set_fallback_persist_profile(
                                        name=service['vip']['id'],
                                        profile_name='/Common/source_addr',
                                        folder=service['vip']['tenant_id'])
                            else:
                                # if they did not supply a cookie_name
                                # just default to regualar cookie peristence
                                bigip.virtual_server.set_persist_profile(
                                       name=service['vip']['id'],
                                       profile_name='/Common/cookie',
                                       folder=service['vip']['tenant_id'])
                                if service['pool']['lb_method'] == 'SOURCE_IP':
                                    bigip.virtual_server.set_fallback_persist_profile(
                                        name=service['vip']['id'],
                                        profile_name='/Common/source_addr',
                                        folder=service['vip']['tenant_id'])
                    else:
                        bigip.virtual_server.remove_all_persist_profiles(
                                        name=service['vip']['id'],
                                        folder=service['vip']['tenant_id'])

                    rule_name = 'http_throttle_' + service['vip']['id']

                    if service['vip']['connection_limit'] > 0 and \
                       'protocol' in service['vip']:
                        # spec says you need to do this for HTTP
                        # and HTTPS, but unless you can decrypt
                        # you can't measure HTTP rps for HTTPs... Duh..
                        if service['vip']['protocol'] == 'HTTP':
                            LOG.debug('adding http profile and RPS throttle rule')
                            # add an http profile
                            bigip.virtual_server.add_profile(
                                name=service['vip']['id'],
                                profile_name='/Common/http',
                                folder=service['vip']['tenant_id'])
                            # create the rps irule
                            rule_definition = \
                              self._create_http_rps_throttle_rule(
                                            service['vip']['connection_limit'])
                            # try to create the irule
                            bigip.rule.create(
                                    name=RPS_THROTTLE_RULE_PREFIX + \
                                     service['vip']['id'],
                                    rule_definition=rule_definition,
                                    folder=service['vip']['tenant_id'])
                            # add the throttle to the vip
                            bigip.virtual_server.add_rule(
                                        name=service['vip']['id'],
                                        rule_name=RPS_THROTTLE_RULE_PREFIX + \
                                              service['vip']['id'],
                                        priority=500,
                                        folder=service['vip']['tenant_id'])
                        else:
                            LOG.debug('setting connection limit')
                            # if not HTTP.. use connection limits
                            bigip.virtual_server.set_connection_limit(
                                name=service['vip']['id'],
                                connection_limit=int(
                                        service['vip']['connection_limit']),
                                folder=service['pool']['tenant_id'])
                    else:
                        # clear throttle rule
                        LOG.debug('removing RPS throttle rule if present')
                        bigip.virtual_server.remove_rule(
                                            name=RPS_THROTTLE_RULE_PREFIX + \
                                            service['vip']['id'],
                                            rule_name=rule_name,
                                            priority=500,
                                            folder=service['vip']['tenant_id'])
                        # clear the connection limits
                        LOG.debug('removing connection limits')
                        bigip.virtual_server.set_connection_limit(
                                name=service['vip']['id'],
                                connection_limit=0,
                                folder=service['pool']['tenant_id'])

                    self.plugin_rpc.update_vip_status(
                                            service['vip']['id'],
                                            status=plugin_const.ACTIVE,
                                            status_description='vip updated'
                                           )
                ctx.assured_subnets.append(service['vip']['subnet']['id'])

    def assure_pool_delete(self, service, bigip, ctx):
        # Remove the pool if it is pending delete
        if service['pool']['status'] == plugin_const.PENDING_DELETE:
            LOG.debug(_('Deleting Pool %s' % service['pool']['id']))
            bigip.pool.delete(name=service['pool']['id'],
                              folder=service['pool']['tenant_id'])
            try:
                self.plugin_rpc.pool_destroyed(service['pool']['id'])
            except Exception as e:
                    LOG.error(_("Plugin delete pool %s error: %s"
                                % (service['pool']['id'], e.message)
                                ))

    def assure_vip_network_delete(self, service, bigip, ctx):
        # Clean up an Self IP, SNATs, networks, and folder for
        # services items that we deleted.
        if ctx.delete_vip_service_networks:
            # Don't delete network objects if you just created
            # or updated objects on those networks
            if not service['vip']['subnet']['id'] in ctx.assured_subnets:
                delete_vip_objects = True
                subnet = netaddr.IPNetwork(
                                          service['vip']['subnet']['cidr'])
                # Are there any virtual addresses on this subnet
                virtual_services = \
                        bigip.virtual_server.get_virtual_service_insertion(
                                        folder=service['vip']['tenant_id'])
                for vs in virtual_services:
                    (vs_name, dest) = vs.items()[0]
                    if netaddr.IPAddress(dest['address']) in subnet:
                        delete_vip_objects = False
                        break
                if delete_vip_objects:
                    # If there aren't any virtual addresses, are there
                    # node addresses on this subnet
                    nodes = bigip.pool.get_node_addresses(
                                    folder=service['vip']['tenant_id'])
                    for node in nodes:
                        if netaddr.IPAddress(node) in subnet:
                            delete_vip_objects = False
                            break
                if delete_vip_objects:
                    # Since no virtual addresses or nodes found
                    # go ahead and try to delete the Self IP
                    # and SNATs
                    self._delete_local_selfip_snat(service['vip'],
                                                               service)
                    # avoids race condition:
                    # deletion of ip objects must sync before we
                    # remove the vlan from the peer bigips.
                    self.sync_if_clustered(bigip, ctx)
                    # Flag this network so we won't try to go through
                    # this same process if a deleted member is on
                    # this same subnet.
                    ctx.deleted_subnets.append(service['vip']['subnet']['id'])
                    try:
                        self._delete_network(service['vip']['network'])
                    except:
                        pass

    def assure_member_network_delete(self, service, bigip, ctx):
        if ctx.delete_member_service_networks:
            for member in service['members']:
                # Only need to bother if the member is deleted
                if member['status'] == plugin_const.PENDING_DELETE:
                    # Only attempt to delete network object on networks
                    # that did not have object created or updated, and
                    # that have not already had their network objects
                    # deleted by the VIP delete process.
                    if not member['subnet']['id'] in ctx.assured_subnets and \
                       not member['subnet']['id'] in ctx.deleted_subnets:
                        delete_member_objects = True
                        subnet = netaddr.IPNetwork(\
                                              member['subnet']['cidr'])
                        # Are there any virtual addresses on this subnet
                        virtual_services = \
                    bigip.virtual_server.get_virtual_service_insertion(
                                        folder=member['tenant_id'])
                        for vs in virtual_services:
                            (vs_name, dest) = vs.items()[0]
                            if netaddr.IPAddress(dest['address']) in subnet:
                                delete_member_objects = False
                                break
                        if delete_member_objects:
                            # If there aren't any virtual addresses, are
                            # there node addresses on this subnet
                            nodes = bigip.pool.get_node_addresses(
                                            folder=member['tenant_id'])
                            for node in nodes:
                                if netaddr.IPAddress(node) in subnet:
                                    delete_member_objects = False
                                    break
                        if delete_member_objects:
                            # Since no virtual addresses or nodes found
                            # go ahead and try to delete the Self IP
                            # and SNATs
                            if not self.conf.f5_snat_mode:
                                self._delete_floating_default_gateway(
                                                              member,
                                                              service)
                            self._delete_local_selfip_snat(member, service)
                            # avoids race condition:
                            # deletion of ip objects must sync before we
                            # remove the vlan from the peer bigips.
                            self.sync_if_clustered(bigip, ctx)
                            try:
                                self._delete_network(member['network'])
                            except:
                                pass
                            # Flag this network so we won't try to go
                            # through this same process if a deleted
                            # another member is delete on this subnet
                            ctx.deleted_subnets.append(member['subnet']['id'])

    def assure_tenant_cleanup(self, service, bigip, ctx):
        # if something was deleted check whether to do domain+folder teardown
        if service['pool']['status'] == plugin_const.PENDING_DELETE or \
             ctx.delete_vip_service_networks or ctx.delete_member_service_networks:
            existing_monitors = bigip.monitor.get_monitors(
                                    folder=service['pool']['tenant_id'])
            existing_pools = bigip.pool.get_pools(
                                    folder=service['pool']['tenant_id'])
            existing_vips = bigip.virtual_server.get_virtual_service_insertion(
                                    folder=service['pool']['tenant_id'])

            if not existing_monitors and \
               not existing_pools and \
               not existing_vips:
                try:
                    # all domains must be gone before we attempt to delete
                    # the folder or it won't delete due to not being empty
                    for b in self.__bigips.values():
                        b.route.delete_domain(
                                folder=service['pool']['tenant_id'])
                    # make sure each big-ip is not currently
                    # set to the folder that is being deleted.
                    for b in self.__bigips.values():
                        b.system.set_folder('/Common')

                    bigip.system.delete_folder(folder='/uuid_' + \
                                                 service['pool']['tenant_id'])
                    # Need to make sure this folder delete syncs before
                    # something else runs and changes the current folder
                    # to the folder being deleted which will cause problems.
                    self.sync_if_clustered(bigip, ctx)
                except:
                    LOG.error("Error cleaning up tenant " + \
                                       service['pool']['tenant_id'])

    def _assure_service_networks(self, service):

        assured_networks = []
        assured_subnet_local_and_snats = []
        assured_floating_default_gateway = []

        if 'id' in service['vip']:
            if not service['vip']['status'] == plugin_const.PENDING_DELETE:
                self._assure_network(service['vip']['network'])
                assured_networks.append(service['vip']['network']['id'])
                # does the pool network need a self-ip or snat addresses?
                assured_networks.append(service['vip']['network']['id'])
                if 'id' in service['vip']['network']:
                    if not service['vip']['network']['id'] in assured_networks:
                        self._assure_network(
                                        service['vip']['network'])
                        assured_networks.append(
                                        service['vip']['network']['id'])
                    self._assure_local_selfip_snat(
                                        service['vip'], service)
                    assured_subnet_local_and_snats.append(
                                        service['vip']['subnet']['id'])

        for member in service['members']:
            if not member['status'] == plugin_const.PENDING_DELETE:
                network_id = member['network']['id']
                subnet_id = member['subnet']['id']
                if not network_id in assured_networks:
                    self._assure_network(member['network'])
                if not subnet_id in assured_subnet_local_and_snats:
                    # each member gets a local self IP on each device
                    self._assure_local_selfip_snat(member, service)
                # if we are not using SNATS, attempt to become
                # the subnet's default gateway.
                if not self.conf.f5_snat_mode and \
                    subnet_id not in assured_floating_default_gateway:
                    self._assure_floating_default_gateway(member, service)
                    assured_floating_default_gateway.append(subnet_id)

    def _assure_network(self, network):
        # setup all needed L2 network segments on all BigIPs
        for bigip in self.__bigips.values():
            if network['provider:network_type'] == 'vlan':
                if network['shared']:
                    network_folder = 'Common'
                else:
                    network_folder = network['tenant_id']

                # VLAN names are limited to 64 characters including
                # the folder name, so we name them foolish things.

                interface = self.interface_mapping['default']
                tagged = self.tagging_mapping['default']
                vlanid = 0

                if network['provider:physical_network'] in \
                                            self.interface_mapping:
                    interface = self.interface_mapping[
                              network['provider:physical_network']]
                    tagged = self.tagging_mapping[
                              network['provider:physical_network']]

                if tagged:
                    vlanid = network['provider:segmentation_id']
                else:
                    vlanid = 0

                vlan_name = self._get_vlan_name(network)

                bigip.vlan.create(name=vlan_name,
                                  vlanid=vlanid,
                                  interface=interface,
                                  folder=network_folder,
                                  description=network['id'])

            if network['provider:network_type'] == 'flat':
                if network['shared']:
                    network_folder = 'Common'
                else:
                    network_folder = network['id']
                interface = self.interface_mapping['default']
                vlanid = 0
                if network['provider:physical_network'] in \
                                            self.interface_mapping:
                    interface = self.interface_mapping[
                              network['provider:physical_network']]

                vlan_name = self._get_vlan_name(network)

                bigip.vlan.create(name=vlan_name,
                                  vlanid=0,
                                  interface=interface,
                                  folder=network_folder,
                                  description=network['id'])

            # TODO: add vxlan

            # TODO: add gre

    def _assure_local_selfip_snat(self, service_object, service):

        bigip = self._get_bigip()
        # Setup non-floating Self IPs on all BigIPs
        snat_pool_name = service['pool']['tenant_id']
        # Where to put all these objects?
        network_folder = service_object['subnet']['tenant_id']
        if service_object['network']['shared']:
            network_folder = 'Common'
        vlan_name = self._get_vlan_name(service_object['network'])

        # On each BIG-IP create the local Self IP for this subnet
        for bigip in self.__bigips.values():

            local_selfip_name = "local-" \
            + bigip.device_name \
            + "-" + service_object['subnet']['id']

            ports = self.plugin_rpc.get_port_by_name(
                                            port_name=local_selfip_name)
            LOG.debug("got ports: %s" % ports)
            if len(ports) > 0:
                ip_address = ports[0]['fixed_ips'][0]['ip_address']
            else:
                new_port = self.plugin_rpc.create_port_on_subnet(
                                subnet_id=service_object['subnet']['id'],
                                mac_address=None,
                                name=local_selfip_name,
                                fixed_address_count=1)
                ip_address = new_port['fixed_ips'][0]['ip_address']
            netmask = netaddr.IPNetwork(
                               service_object['subnet']['cidr']).netmask
            bigip.selfip.create(name=local_selfip_name,
                                ip_address=ip_address,
                                netmask=netmask,
                                vlan_name=vlan_name,
                                floating=False,
                                folder=network_folder)

        # Setup required SNAT addresses on this subnet
        # based on the HA requirements
        if self.conf.f5_snat_addresses_per_subnet > 0:
            # failover mode dictates SNAT placement on traffic-groups
            if self.conf.f5_ha_type == 'standalone':
                # Create SNATs on traffic-group-local-only
                snat_name = 'snat-traffic-group-local-only-' + \
                 service_object['subnet']['id']
                for i in range(self.conf.f5_snat_addresses_per_subnet):
                    ip_address = None
                    index_snat_name = snat_name + "_" + str(i)
                    ports = self.plugin_rpc.get_port_by_name(
                                            port_name=index_snat_name)
                    if len(ports) > 0:
                        ip_address = ports[0]['fixed_ips'][0]['ip_address']
                    else:
                        new_port = self.plugin_rpc.create_port_on_subnet(
                            subnet_id=service_object['subnet']['id'],
                            mac_address=None,
                            name=index_snat_name,
                            fixed_address_count=1)
                        ip_address = new_port['fixed_ips'][0]['ip_address']
                    if service_object['network']['shared']:
                        ip_address = ip_address + '%0'
                    if service_object['network']['shared']:
                        index_snat_name = '/Common/' + index_snat_name
                    bigip.snat.create(
                     name=index_snat_name,
                     ip_address=ip_address,
                     traffic_group='/Common/traffic-group-local-only',
                     snat_pool_name=None,
                     folder=network_folder
                    )
                    bigip.snat.create_pool(name=snat_pool_name,
                                           member_name=index_snat_name,
                                           folder=service['pool']['tenant_id'])

            elif self.conf.f5_ha_type == 'ha':
                # Create SNATs on traffic-group-1
                snat_name = 'snat-traffic-group-1' + \
                 service_object['subnet']['id']
                for i in range(self.conf.f5_snat_addresses_per_subnet):
                    ip_address = None
                    index_snat_name = snat_name + "_" + str(i)
                    ports = self.plugin_rpc.get_port_by_name(
                                            port_name=index_snat_name)
                    if len(ports) > 0:
                        ip_address = ports[0]['fixed_ips'][0]['ip_address']
                    else:
                        new_port = self.plugin_rpc.create_port_on_subnet(
                            subnet_id=service_object['subnet']['id'],
                            mac_address=None,
                            name=index_snat_name,
                            fixed_address_count=1)
                        ip_address = new_port['fixed_ips'][0]['ip_address']
                    if service_object['network']['shared']:
                        ip_address = ip_address + '%0'
                        index_snat_name = '/Common/' + index_snat_name
                    bigip.snat.create(
                     name=index_snat_name,
                     ip_address=ip_address,
                     traffic_group='/Common/traffic-group-1',
                     snat_pool_name=None,
                     folder=network_folder
                    )
                    bigip.snat.create_pool(name=snat_pool_name,
                                           member_name=index_snat_name,
                                           folder=service['pool']['tenant_id'])

            elif self.conf.f5_ha_type == 'scalen':
                # create SNATs on all provider defined traffic groups
                for traffic_group in self.__traffic_groups:
                    for i in range(self.conf.f5_snat_addresses_per_subnet):
                        snat_name = "snat-" + traffic_group + "-" + \
                         service_object['subnet']['id']
                        ip_address = None
                        index_snat_name = snat_name + "_" + str(i)

                        ports = self.plugin_rpc.get_port_by_name(
                                            port_name=index_snat_name)
                        if len(ports) > 0:
                            ip_address = ports[0]['fixed_ips'][0]['ip_address']
                        else:
                            new_port = self.plugin_rpc.create_port_on_subnet(
                                subnet_id=service_object['subnet']['id'],
                                mac_address=None,
                                name=index_snat_name,
                                fixed_address_count=1)
                            ip_address = new_port['fixed_ips'][0]['ip_address']
                        if service_object['network']['shared']:
                            ip_address = ip_address + '%0'
                        if service_object['network']['shared']:
                            index_snat_name = '/Common/' + index_snat_name
                        bigip.snat.create(
                         name=index_snat_name,
                         ip_address=ip_address,
                         traffic_group=traffic_group,
                         snat_pool_name=None,
                         folder=network_folder
                        )
                        bigip.snat.create_pool(name=snat_pool_name,
                                           member_name=index_snat_name,
                                           folder=service['pool']['tenant_id'])

    def _assure_floating_default_gateway(self, service_object, service):

        bigip = self._get_bigip()

        # Do we already have a port with the gateway_ip belonging
        # to this agent's host?
        #
        # This is another way to check if you want to iterate
        # through all ports owned by this device
        #
        # for port in service_object['subnet_ports']:
        #    if not need_port_for_gateway:
        #        break
        #    for fixed_ips in port['fixed_ips']:
        #        if fixed_ips['ip_address'] == \
        #            service_object['subnet']['gateway_ip']:
        #            need_port_for_gateway = False
        #            break

        # Create a name for the port and for the IP Forwarding Virtual Server
        # as well as the floating Self IP which will answer ARP for the members
        gw_name = "gw-" + service_object['subnet']['id']
        floating_selfip_name = "gw-" + service_object['subnet']['id']
        netmask = netaddr.IPNetwork(
                               service_object['subnet']['cidr']).netmask
        ports = self.plugin_rpc.get_port_by_name(
                                            port_name=gw_name)
        if len(ports) < 1:
            need_port_for_gateway = True

        # There was not port on this agent's host, so get one from Neutron
        if need_port_for_gateway:
            try:
                new_port = \
                  self.plugin_rpc.create_port_on_subnet_with_specific_ip(
                            subnet_id=service_object['subnet']['id'],
                            mac_address=None,
                            name=gw_name,
                            ip_address=service_object['subnet']['gateway_ip'])
                service_object['subnet_ports'].append(new_port)
            except Exception as e:
                ermsg = 'Invalid default gateway for subnet %s:%s - %s.' \
                % (service_object['subnet']['id'],
                   service_object['subnet']['gateway_ip'],
                   e.message)
                ermsg += " SNAT will not function and load balancing"
                ermsg += " support will likely fail. Enable f5_snat_mode"
                ermsg += " and f5_source_monitor_from_member_subnet."
                LOG.error(_(ermsg))

        # Go ahead and setup a floating SelfIP with the subnet's
        # gateway_ip address on this agent's device service group

        network_folder = service_object['subnet']['tenant_id']
        vlan_name = self._get_vlan_name(service_object['network'])
        # Where to put all these objects?
        if service_object['network']['shared']:
            network_folder = 'Common'
            vlan_name = '/Common/' + vlan_name

        # Select a traffic group for the floating SelfIP
        tg = self._get_least_gw_traffic_group()
        bigip.selfip.create(
                            name=floating_selfip_name,
                            ip_address=service_object['subnet']['gateway_ip'],
                            netmask=netmask,
                            vlan_name=vlan_name,
                            floating=True,
                            traffic_group=tg,
                            folder=network_folder)

        # Get the actual traffic group if the Self IP already existed
        tg = bigip.self.get_traffic_group(name=floating_selfip_name,
                                folder=service_object['subnet']['tenant_id'])

        # Setup a wild card ip forwarding virtual service for this subnet
        bigip.virtual_server.create_ip_forwarder(
                            name=gw_name, ip_address='0.0.0.0',
                            mask='0.0.0.0',
                            vlan_name=vlan_name,
                            traffic_group=tg,
                            folder=network_folder)

        # Setup the IP forwarding virtual server to use the Self IPs
        # as the forwarding SNAT addresses
        bigip.virtual_server.set_snat_automap(name=gw_name,
                            folder=network_folder)

    def _delete_network(self, network):
        # setup all needed L2 network segments on all BigIPs
        for bigip in self.__bigips.values():
            if network['provider:network_type'] == 'vlan':
                if network['shared']:
                    network_folder = 'Common'
                else:
                    network_folder = network['tenant_id']
                vlan_name = self._get_vlan_name(network)
                bigip.vlan.delete(name=vlan_name,
                                  folder=network_folder)

            if network['provider:network_type'] == 'flat':
                if network['shared']:
                    network_folder = 'Common'
                else:
                    network_folder = network['id']
                vlan_name = self._get_vlan_name(network)
                bigip.vlan.delete(name=vlan_name,
                                  folder=network_folder)

            # TODO: add vxlan

            # TODO: add gre

    def _delete_local_selfip_snat(self, service_object, service):
        bigip = self._get_bigip()
        network_folder = service_object['subnet']['tenant_id']
        if service_object['network']['shared']:
            network_folder = 'Common'
        snat_pool_name = service['pool']['tenant_id']
        # Setup required SNAT addresses on this subnet
        # based on the HA requirements
        if self.conf.f5_snat_addresses_per_subnet > 0:
            # failover mode dictates SNAT placement on traffic-groups
            if self.conf.f5_ha_type == 'standalone':
                # Create SNATs on traffic-group-local-only
                snat_name = 'snat-traffic-group-local-only-' + \
                 service_object['subnet']['id']
                for i in range(self.conf.f5_snat_addresses_per_subnet):
                    index_snat_name = snat_name + "_" + str(i)
                    if service_object['network']['shared']:
                        tmos_snat_name = "/Common/" + index_snat_name
                    else:
                        tmos_snat_name = index_snat_name
                    bigip.snat.remove_from_pool(name=snat_pool_name,
                                         member_name=tmos_snat_name,
                                         folder=service['pool']['tenant_id'])
                    if bigip.snat.delete(name=tmos_snat_name,
                                         folder=network_folder):
                        # Only if it still exists and can be
                        # deleted because it is not in use can
                        # we safely delete the neutron port
                        self.plugin_rpc.delete_port_by_name(
                                            port_name=index_snat_name)
            elif self.conf.f5_ha_type == 'ha':
                # Create SNATs on traffic-group-1
                snat_name = 'snat-traffic-group-1' + \
                 service_object['subnet']['id']
                for i in range(self.conf.f5_snat_addresses_per_subnet):
                    index_snat_name = snat_name + "_" + str(i)
                    if service_object['network']['shared']:
                        tmos_snat_name = "/Common/" + index_snat_name
                    else:
                        tmos_snat_name = index_snat_name
                    bigip.snat.remove_from_pool(name=snat_pool_name,
                                        member_name=tmos_snat_name,
                                        folder=service['pool']['tenant_id'])
                    if bigip.snat.delete(name=tmos_snat_name,
                                         folder=network_folder):
                        # Only if it still exists and can be
                        # deleted because it is not in use can
                        # we safely delete the neutron port
                        self.plugin_rpc.delete_port_by_name(
                                            port_name=index_snat_name)
            elif self.conf.f5_ha_type == 'scalen':
                # create SNATs on all provider defined traffic groups
                for traffic_group in self.__traffic_groups:
                    for i in range(self.conf.f5_snat_addresses_per_subnet):
                        snat_name = "snat-" + traffic_group + "-" + \
                         service_object['subnet']['id']
                        index_snat_name = snat_name + "_" + str(i)
                        if service_object['network']['shared']:
                            tmos_snat_name = "/Common/" + index_snat_name
                        else:
                            tmos_snat_name = index_snat_name
                        bigip.snat.remove_from_pool(name=snat_pool_name,
                                        member_name=tmos_snat_name,
                                        folder=service['pool']['tenant_id'])
                        if bigip.snat.delete(name=tmos_snat_name,
                                                 folder=network_folder):
                            # Only if it still exists and can be
                            # deleted because it is not in use can
                            # we safely delete the neutron port
                            self.plugin_rpc.delete_port_by_name(
                                                port_name=index_snat_name)
        # On each BIG-IP delete the local Self IP for this subnet
        for bigip in self.__bigips.values():

            local_selfip_name = "local-" \
            + bigip.device_name \
            + "-" + service_object['subnet']['id']
            bigip.selfip.delete(name=local_selfip_name,
                                folder=network_folder)
            self.plugin_rpc.delete_port_by_name(port_name=local_selfip_name)

    def _delete_floating_default_gateway(self, service_object, service):

        bigip = self._get_bigip()

        # Create a name for the port and for the IP Forwarding Virtual Server
        # as well as the floating Self IP which will answer ARP for the members
        gw_name = "gw-" + service_object['subnet']['id']
        floating_selfip_name = "gw-" + service_object['subnet']['id']

        # Go ahead and setup a floating SelfIP with the subnet's
        # gateway_ip address on this agent's device service group

        network_folder = service_object['subnet']['tenant_id']
        if service_object['network']['shared']:
            network_folder = 'Common'

        bigip.selfip.delete(name=floating_selfip_name,
                            folder=network_folder)

        # Setup a wild card ip forwarding virtual service for this subnet
        bigip.virtual_server.delete(name=gw_name,
                                    folder=network_folder)

        # remove neutron default gateway port if the device id is
        # f5_lbass
        gateway_port_id = None
        for port in service_object['subnet_ports']:
            if gateway_port_id:
                break
            for fixed_ips in port['fixed_ips']:
                if str(fixed_ips['ip_address']).strip() == \
                    str(service_object['subnet']['gateway_ip']).strip():
                    gateway_port_id = port['id']
                    break

        # There was not port on this agent's host, so get one from Neutron
        if gateway_port_id:
            try:
                self.plugin_rpc.delete_port(port_id=gateway_port_id,
                                            mac_address=None)
            except Exception as e:
                ermsg = 'Error on delete gateway port for subnet %s:%s - %s.' \
                % (service_object['subnet']['id'],
                   service_object['subnet']['gateway_ip'],
                   e.message)
                ermsg += " You will need to delete this manually"
                LOG.error(_(ermsg))

    def _get_least_vips_traffic_group(self):
        traffic_group = '/Common/traffic-group-1'
        lowest_count = 0
        for tg in self.__vips_on_traffic_groups:
            if self.__vips_on_traffic_groups[tg] <= lowest_count:
                traffic_group = self.__vips_on_traffic_groups[tg]
        return traffic_group

    def _get_least_gw_traffic_group(self):
        traffic_group = '/Common/traffic-group-1'
        lowest_count = 0
        for tg in self.__gw_on_traffic_groups:
            if self.__gw_on_traffic_groups[tg] <= lowest_count:
                traffic_group = self.__gw_on_traffic_groups[tg]
        return traffic_group

    def _get_bigip(self):
        hostnames = sorted(self.__bigips)
        for i in range(len(hostnames)):
            try:
                bigip = self.__bigips[hostnames[i]]
                bigip.system.set_folder('/Common')
                return bigip
            except urllib2.URLError:
                pass
        else:
            raise urllib2.URLError('cannot communicate to any bigips')

    def _get_vlan_name(self, network):
        interface = self.interface_mapping['default']
        tagged = self.tagging_mapping['default']

        if network['provider:physical_network'] in \
                                            self.interface_mapping:
            interface = self.interface_mapping[
                              network['provider:physical_network']]
            tagged = self.tagging_mapping[
                              network['provider:physical_network']]

        if tagged:
            vlanid = network['provider:segmentation_id']
        else:
            vlanid = 0

        return "vlan-" + str(interface).replace(".", "-") + "-" + str(vlanid)

    def _create_app_cookie_persist_rule(self, cookiename):
        rule_text = "when HTTP_REQUEST {\n"
        rule_text += " if { [HTTP::cookie " + str(cookiename)
        rule_text += "] ne \"\" }{\n"
        rule_text += "     persist uie [string tolower [HTTP::cookie \""
        rule_text += cookiename + "\"]] 3600\n"
        rule_text += " }\n"
        rule_text += "}\n\n"
        rule_text += "when HTTP_RESPONSE {\n"
        rule_text += " if { [HTTP::cookie \"" + str(cookiename)
        rule_text += "\"] ne \"\" }{\n"
        rule_text += "     persist add uie [string tolower [HTTP::cookie \""
        rule_text += cookiename + "\"]] 3600\n"
        rule_text += " }\n"
        rule_text += "}\n\n"
        return rule_text

    def _create_http_rps_throttle_rule(self, req_limit):
        rule_text = "when HTTP_REQUEST {\n"
        rule_text += " set expiration_time 300\n"
        rule_text += " set client_ip [IP::client_addr]\n"
        rule_text += " set req_limit " + str(req_limit) + "\n"
        rule_text += " set curr_time [clock seconds]\n"
        rule_text += " set timekey starttime\n"
        rule_text += " set reqkey reqcount\n"
        rule_text += " set request_count [session lookup uie $reqkey]\n"
        rule_text += " if { $request_count eq \"\" } {\n"
        rule_text += "   set request_count 1\n"
        rule_text += "   session add uie $reqkey $request_count "
        rule_text += "$expiration_time\n"
        rule_text += "   session add uie $timekey [expr {$curr_time - 2}]"
        rule_text += "[expr {$expiration_time + 2}]\n"
        rule_text += " } else {\n"
        rule_text += "   set start_time [session lookup uie $timekey]\n"
        rule_text += "   incr request_count\n"
        rule_text += "   session add uie $reqkey $request_count"
        rule_text += "$expiration_time\n"
        rule_text += "   set elapsed_time [expr {$curr_time - $start_time}]\n"
        rule_text += "   if {$elapsed_time < 60} {\n"
        rule_text += "     set elapsed_time 60\n"
        rule_text += "   }\n"
        rule_text += "   set curr_rate [expr {$request_count /"
        rule_text += "($elapsed_time/60)}]\n"
        rule_text += "   if {$curr_rate > $req_limit}{\n"
        rule_text += "     HTTP::respond 503 throttled \"Retry-After\" 60\n"
        rule_text += "   }\n"
        rule_text += " }\n"
        rule_text += "}\n"
        return rule_text

    def _init_connection(self):
        if not self.connected:
            try:
                self.__last_connect_attempt = datetime.datetime.now()

                if not self.conf.icontrol_hostname:
                    raise InvalidConfigurationOption(
                                 opt_name='icontrol_hostname',
                                 opt_value='valid hostname or IP address')
                if not self.conf.icontrol_username:
                    raise InvalidConfigurationOption(
                                 opt_name='icontrol_username',
                                 opt_value='valid username')
                if not self.conf.icontrol_password:
                    raise InvalidConfigurationOption(
                                 opt_name='icontrol_password',
                                 opt_value='valid password')

                self.hostnames = sorted(
                                    self.conf.icontrol_hostname.split(','))

                self.agent_id = self.hostnames[0]

                self.username = self.conf.icontrol_username
                self.password = self.conf.icontrol_password

                LOG.debug(_('opening iControl connections to %s @ %s' % (
                                                            self.username,
                                                            self.hostnames[0])
                            ))

                # connect to inital device:
                first_bigip = bigip.BigIP(self.hostnames[0],
                                        self.username,
                                        self.password,
                                        5,
                                        self.conf.use_namespaces)
                self.__bigips[self.hostnames[0]] = first_bigip
                first_bigip.group_bigips = self.__bigips

                # if there was only one address supplied and
                # this is not a standalone device, get the
                # devices trusted by this device.
                if len(self.hostnames) < 2:
                    if not first_bigip.cluster.get_sync_status() == \
                                                              'Standalone':
                        first_bigip.system.set_folder('/Common')
                        this_devicename = \
                         first_bigip.device.mgmt_dev.get_local_device()
                        devices = first_bigip.device.get_all_device_names()
                        devices.remove[this_devicename]
                        self.hostnames = self.hostnames + \
                    first_bigip.device.mgmt_dev.get_management_address(devices)
                    else:
                        LOG.debug(_(
                            'only one host connected and it is Standalone.'))
                # populate traffic groups
                first_bigip.system.set_folder(folder='/Common')
                self.__traffic_groups = first_bigip.cluster.mgmt_tg.get_list()
                if '/Common/traffic-group-local-only' in self.__traffic_groups:
                    self.__traffic_groups.remove(
                                    '/Common/traffic-group-local-only')
                if '/Common/traffic-group-1' in self.__traffic_groups:
                    self.__traffic_groups.remove('/Common/traffic-group-1')
                for tg in self.__traffic_groups:
                    self.__gw_on_traffic_groups[tg] = 0
                    self.__vips_on_traffic_groups[tg] = 0

                # connect to the rest of the devices
                for host in self.hostnames[1:]:
                    hostbigip = bigip.BigIP(host,
                                            self.username,
                                            self.password,
                                            5,
                                            self.conf.use_namespaces)
                    self.__bigips[host] = hostbigip
                    hostbigip.group_bigips = self.__bigips

                # validate device versions
                for host in self.__bigips:
                    hostbigip = self.__bigips[host]
                    major_version = hostbigip.system.get_major_version()
                    if major_version < f5const.MIN_TMOS_MAJOR_VERSION:
                        raise f5ex.MajorVersionValidateFailed(
                                'device %s must be at least TMOS %s.%s'
                                % (host,
                                   f5const.MIN_TMOS_MAJOR_VERSION,
                                   f5const.MIN_TMOS_MINOR_VERSION))
                    minor_version = hostbigip.system.get_minor_version()
                    if minor_version < f5const.MIN_TMOS_MINOR_VERSION:
                        raise f5ex.MinorVersionValidateFailed(
                                'device %s must be at least TMOS %s.%s'
                                % (host,
                                   f5const.MIN_TMOS_MAJOR_VERSION,
                                   f5const.MIN_TMOS_MINOR_VERSION))

                    hostbigip.device_name = hostbigip.device.get_device_name()

                    LOG.debug(_('connected to iControl %s @ %s ver %s.%s'
                                % (self.username, host,
                                   major_version, minor_version)))

                self.connected = True
            except Exception as e:
                LOG.error(_('Could not communicate with all iControl devices: %s'
                               % e.message))
    
    def sync_if_clustered(self, bigip, ctx):
        if len(bigip.group_bigips) > 1:
            if not ctx.device_group:
                ctx.device_group = bigip.device.get_device_group()
            bigip.cluster.sync(ctx.device_group)
        return ctx.device_group
