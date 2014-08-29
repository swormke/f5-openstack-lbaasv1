#!/usr/bin/env python

##############################################################################
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright 2014 by F5 Networks and/or its suppliers. All rights reserved.
##############################################################################

from distutils.core import setup
import platform

data_files=[('/usr/bin', ['usr/bin/f5-bigip-lbaas-agent']),
            ('/etc/neutron', ['etc/neutron/f5-bigip-lbaas-agent.ini'])]

dist = platform.dist()[0]
if dist == 'centos' or dist == 'redhat':
    data_files.append(('/etc/init.d', ['etc/init.d/f5-bigip-lbaas-agent'] ))

setup(name='f5-bigip-lbaas-agent',
      version='1.0.3.icehouse-1',
      description='F5 LBaaS Agent for OpenStack',
      author='F5 DevCentral',
      author_email='devcentral@f5.com',
      url='http://devcentral.f5.com/f5',
      py_modules=['neutron.services.loadbalancer.drivers.f5.bigip.agent',
                  'neutron.services.loadbalancer.drivers.f5.bigip.agent_api',
                  'neutron.services.loadbalancer.drivers.f5.bigip.agent_manager',
                  'neutron.services.loadbalancer.drivers.f5.bigip.constants',
                  'neutron.services.loadbalancer.drivers.f5.bigip.icontrol_driver'],
      packages=  ['f5', 'f5.common', 'f5.bigip', 'f5.bigip.bigip_interfaces', 'f5.bigip.pycontrol'],
      data_files=data_files
     )
