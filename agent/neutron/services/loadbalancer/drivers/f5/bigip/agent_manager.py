# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 New Dream Network, LLC (DreamHost)
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Mark McClain, DreamHost

import weakref

from oslo.config import cfg
from neutron.agent import rpc as agent_rpc
from neutron.common import constants
from neutron import context
from neutron.openstack.common import importutils
from neutron.common import log
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task
from neutron.services.loadbalancer.drivers.f5.bigip import (
    agent_api,
    plugin_driver
)

LOG = logging.getLogger(__name__)

__VERSION__ = "0.1.1"

OPTS = [
    cfg.StrOpt(
        'device_driver',
        default=('neutron.services.loadbalancer.drivers'
                 '.f5.bigip.icontrol_driver.iControlDriver'),
        help=_('The driver used to provision BigIPs'),
    )
]


class LogicalServiceCache(object):
    """Manage a cache of known services."""

    class Service(object):
        """Inner classes used to hold values for weakref lookups."""
        def __init__(self, port_id, pool_id):
            self.port_id = port_id
            self.pool_id = pool_id

        def __eq__(self, other):
            return self.__dict__ == other.__dict__

        def __hash__(self):
            return hash((self.port_id, self.pool_id))

    def __init__(self):
        LOG.debug(_("Initializing LogicalServiceCache version %s"
                    % __VERSION__))
        self.services = set()
        self.port_lookup = weakref.WeakValueDictionary()
        self.pool_lookup = weakref.WeakValueDictionary()

    def put(self, service):
        if 'port_id' in service['vip']:
            port_id = service['vip']['port_id']
        else:
            port_id = None
        pool_id = service['pool']['id']
        s = self.Service(port_id, pool_id)
        if s not in self.services:
            self.services.add(s)
            if port_id:
                self.port_lookup[port_id] = s
            self.pool_lookup[pool_id] = s

    def remove(self, service):
        if not isinstance(service, self.Service):
            if 'port_id' in service['vip']:
                port_id = service['vip']['port_id']
            else:
                port_id = None
            sevice = self.Service(
                port_id, service['pool']['id']
            )
        if service in self.services:
            self.services.remove(sevice)

    def remove_by_pool_id(self, pool_id):
        s = self.pool_lookup.get(pool_id)
        if s:
            self.services.remove(s)

    def get_by_pool_id(self, pool_id):
        return self.pool_lookup.get(pool_id)

    def get_by_port_id(self, port_id):
        return self.port_lookup.get(port_id)

    def get_pool_ids(self):
        return self.pool_lookup.keys()

    def get_tenant_ids(self):
        tenant_ids = {}
        for service in self.services:
            tenant_ids[service['pool']['tenant_id']] = 1
        return tenant_ids.keys()


class LbaasAgentManager(periodic_task.PeriodicTasks):

    # history
    #   1.0 Initial version
    #   1.1 Support agent_updated call
    RPC_API_VERSION = '1.1'

    def __init__(self, conf):
        LOG.debug(_('initializing LbaasAgentManager with conf %s' % conf))
        self.conf = conf
        try:
            self.driver = importutils.import_object(
                conf.device_driver, self.conf)
        except ImportError:
            msg = _('Error importing loadbalancer device driver: %s')
            raise SystemExit(msg % conf.device_driver)

        self.agent_state = {
            'binary': 'neutron-loadbalancer-agent',
            'host': conf.host,
            'topic': plugin_driver.TOPIC_LOADBALANCER_AGENT,
            'configurations': {'device_driver': conf.device_driver},
            'agent_type': constants.AGENT_TYPE_LOADBALANCER,
            'start_flag': True}

        self.admin_state_up = True

        self.context = context.get_admin_context_without_session()
        self._setup_rpc()
        self.needs_resync = False
        self.cache = LogicalServiceCache()

    @log.log
    def _setup_rpc(self):
        self.plugin_rpc = agent_api.LbaasAgentApi(
            plugin_driver.TOPIC_PROCESS_ON_HOST,
            self.context,
            self.conf.host
        )

        self.state_rpc = agent_rpc.PluginReportStateAPI(
            plugin_driver.TOPIC_PROCESS_ON_HOST)
        report_interval = self.conf.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

    def _report_state(self):
        try:
            service_count = len(self.cache.services)
            self.agent_state['configurations']['services'] = service_count
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_("Failed reporting state!"))

    def initialize_service_hook(self, started_by):
        self.sync_state()

    @periodic_task.periodic_task
    def periodic_resync(self, context):
        if self.needs_resync:
            self.needs_resync = False
            self.sync_state()

    @periodic_task.periodic_task(spacing=6)
    def collect_stats(self, context):
        for pool_id in self.cache.get_pool_ids():
            try:
                stats = self.driver.get_stats(
                                  self.cache.get_by_pool_id(pool_id))
                if stats:
                    self.plugin_rpc.update_pool_stats(pool_id, stats)
            except Exception:
                LOG.exception(_('Error upating stats'))
                self.needs_resync = True

    def _vip_plug_callback(self, action, port):
        if action == 'plug':
            self.plugin_rpc.plug_vip_port(port['id'])
        elif action == 'unplug':
            self.plugin_rpc.unplug_vip_port(port['id'])

    def sync_state(self):
        known_services = set(self.cache.get_pool_ids())
        try:
            ready_logical_services = set(self.plugin_rpc.get_ready_services(
                                         tenant_ids=self.cache.get_tenant_ids()
                                                                            )
                                         )

            for deleted_id in known_services - ready_logical_services:
                self.destroy_service(deleted_id)

            for pool_id in ready_logical_services:
                self.refresh_service(pool_id)

        except Exception:
            LOG.exception(_('Unable to retrieve ready services'))
            self.needs_resync = True

        self.remove_orphans()

    @log.log
    def refresh_service(self, pool_id):
        try:
            logical_config = self.plugin_rpc.get_logical_service(pool_id)
            # update is create or update
            self.driver.sync(logical_config)
            self.cache.put(logical_config)
        except Exception:
            LOG.exception(_('Unable to refresh service for pool: %s'), pool_id)
            self.needs_resync = True

    @log.log
    def destroy_service(self, pool_id):
        service = self.cache.get_by_pool_id(pool_id)
        if not service:
            return
        try:
            self.driver.destroy(self.cache.get_by_pool_id(pool_id))
            self.plugin_rpc.pool_destroyed(pool_id)
        except Exception:
            LOG.exception(_('Unable to destroy service for pool: %s'), pool_id)
            self.needs_resync = True
        self.cache.remove(service)

    @log.log
    def remove_orphans(self):
        try:
            self.driver.remove_orphans(self.cache.get_pool_ids())
        except NotImplementedError:
            pass  # Not all drivers will support this

    @log.log
    def reload_pool(self, context, pool_id=None, host=None):
        """Handle RPC cast from plugin to reload a pool."""
        if pool_id:
            self.refresh_service(pool_id)

    def create_vip(self, context, vip, network):
        """Handle RPC cast from plugin to create_vip"""
        self.driver.create_vip(vip, network)

    def update_vip(self, context, old_vip, vip, old_network, network):
        """Handle RPC cast from plugin to update_vip"""
        self.driver.update_vip(old_vip, vip, old_network, network)

    def delete_vip(self, context, vip, network):
        """Handle RPC cast from plugin to delete_vip"""
        self.driver.delete_vip(vip, network)

    def create_pool(self, context, pool, network):
        """Handle RPC cast from plugin to create_pool"""
        self.driver.create_pool(pool, network)

    def update_pool(self, context, old_pool, pool, old_network, network):
        """Handle RPC cast from plugin to update_pool"""
        self.driver.update_pool(old_pool, pool, old_network, network)

    def delete_pool(self, context, pool, network):
        """Handle RPC cast from plugin to delete_pool"""
        self.driver.delete_pool(pool, network)

    def create_member(self, context, member, network):
        """Handle RPC cast from plugin to create_member"""
        self.driver.create_member(member, network)

    def update_member(self, context, old_member, member, old_network, network):
        """Handle RPC cast from plugin to update_member"""
        self.driver.update_member(old_member, member, old_network, network)

    def delete_member(self, context, member, network):
        """Handle RPC cast from plugin to delete_member"""
        self.driver.delete_member(member, network)

    def create_pool_health_monitor(self, context, health_monitor,
                                   pool, network):
        """Handle RPC cast from plugin to create_pool_health_monitor"""
        self.driver.create_pool_health_monitor(health_monitor,
                                               pool, network)

    def update_health_monitor(self, context, old_health_monitor,
                              health_monitor, pool, network):
        """Handle RPC cast from plugin to update_health_monitor"""
        self.driver.update_health_monitor(old_health_monitor, health_monitor,
                                          pool, network)

    def delete_pool_health_monitor(self, context, health_monitor,
                                   pool, network):
        """Handle RPC cast from plugin to delete_pool_health_monitor"""
        self.driver.delete_pool_health_monitor(health_monitor,
                                               pool, network)

    @log.log
    def agent_updated(self, context, payload):
        """Handle the agent_updated notification event."""
        if payload['admin_state_up'] != self.admin_state_up:
            self.admin_state_up = payload['admin_state_up']
            if self.admin_state_up:
                self.needs_resync = True
            else:
                for pool_id in self.cache.get_pool_ids():
                    self.destroy_service(pool_id)
            LOG.info(_("agent_updated by server side %s!"), payload)