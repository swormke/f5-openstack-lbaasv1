import os
import time
import json 

from suds import WebFault
from common.logger import Log
from common import constants as const


# Management - Device
class Device(object):
    def __init__(self, bigip):
        self.bigip = bigip

        # add iControl interfaces if they don't exist yet
        self.bigip.icontrol.add_interfaces(
                                           ['Management.Device',
                                            'Management.Trust']
                                           )

        # iControl helper objects
        self.mgmt_dev = self.bigip.icontrol.Management.Device
        self.mgmt_trust = self.bigip.icontrol.Management.Trust

        # create empty lock instance ID
        self.lock = None

    def get_device_name(self):
        return os.path.basename(self.mgmt_dev.get_local_device())

    def get_all_device_names(self):
        return [os.path.basename(dev) for dev in self.mgmt_dev.get_list()]

    def get_lock(self):
        current_lock = self._get_lock()
        new_lock = int(time.time())

        if current_lock:
            if (new_lock - current_lock) > const.CONNECTION_TIMEOUT:
                Log.info('Device', 'Locking device %s with lock %s'
                           % (self.mgmt_dev.get_local_device(), new_lock))
                self._set_lock(new_lock)
                return True
            else:
                return False
        else:
            Log.info('Device', 'Locking device %s with lock %s'
                       % (self.mgmt_dev.get_local_device(), new_lock))
            self._set_lock(int(time.time()))
            return True

    def release_lock(self):
        dev_name = self.mgmt_dev.get_local_device()
        current_lock = self._get_lock()

        if current_lock == self.lock:
            Log.info('Device', 'Releasing device lock for %s'
                       % self.mgmt_dev.get_local_device())
            self.mgmt_dev.set_comment([dev_name], [''])
            return True
        else:
            Log.info('Device', 'Device has foreign lock instance on %s '
                       % self.mgmt_dev.get_local_device() + ' with lock %s '
                       % current_lock)
            return False

    def _get_lock(self):
        dev_name = self.mgmt_dev.get_local_device()
        current_lock = self.mgmt_dev.get_comment([dev_name])[0]

        if current_lock.startswith(const.DEVICE_LOCK_PREFIX):
            return int(current_lock.replace(const.DEVICE_LOCK_PREFIX, ''))

    def _set_lock(self, lock):
        dev_name = self.mgmt_dev.get_local_device()
        self.lock = lock
        lock_comment = const.DEVICE_LOCK_PREFIX + str(lock)
        self.mgmt_dev.set_comment([dev_name], [lock_comment])

    def get_mgmt_addr(self):
        return self.mgmt_dev.get_management_address(
                                                [self.get_device_name()]
                                                    )[0]

    def get_configsync_addr(self):
        return self.mgmt_dev.get_configsync_address(
                                                [self.get_device_name()]
                                                    )[0]

    def set_configsync_addr(self, addr):
        if not addr:
            addr = 'none'

        self.mgmt_dev.set_configsync_address([self.get_device_name()],
                                             [addr])

    def get_primary_mirror_addr(self):
        return self.mgmt_dev.get_primary_mirror_address(
                                                [self.get_device_name()]
                                                        )[0]

    def set_primary_mirror_addr(self, addr):
        if not addr:
            addr = 'none'

        self.mgmt_dev.set_primary_mirror_address([self.get_device_name()],
                                                 [addr])

    def get_failover_addrs(self):
        return self.mgmt_dev.get_unicast_addresses(
                                                   [self.get_device_name()]
                                                   )[0]

    def set_failover_addrs(self, addrs):
        if not addrs:
            addrs = ['none']

        if isinstance(addrs, list):
            seq = self.mgmt_dev.typefactory.create('Common.StringSequence')
            unicast_defs = []

            for addr in addrs:
                ipport_def = self.mgmt_dev.typefactory.create(
                                                'Common.IPPortDefinition')
                ipport_def.address = addr
                ipport_def.port = 1026
                unicast_def = self.mgmt_dev.typefactory.create(
                                       'Management.Device.UnicastAddress')
                unicast_def.effective = ipport_def
                unicast_def.source = ipport_def

                unicast_defs.append(unicast_def)

            seq.item = unicast_defs

            self.mgmt_dev.set_unicast_addresses([self.get_device_name()],
                                                [seq])
        else:
            raise TypeError()

    def get_failover_state(self):
        current_dev_name = self.get_device_name()
        return self.mgmt_dev.get_failover_state([current_dev_name])[0]

    def get_device_group(self):
        device_groups = self.bigip.cluster.mgmt_dg.get_list()
        device_group_types = self.bigip.cluster.mgmt_dg.get_type(
                                                         device_groups)
        for i in range(len(device_group_types)):
            if device_group_types[i] == 'DGT_FAILOVER':
                return os.path.basename(device_groups[i])
        return None

    def remove_from_device_group(self, device_group_name=None):
        if not device_group_name:
            device_group_name = self.get_device_group()

        if device_group_name:
            device_entry_seq = self.mgmt_dev.typefactory.create(
                                        'Common.StringSequence')
            device_entry_seq.values = [self.bigip.add_folder(
                                        'Common',
                                         self.get_device_name())]
            device_entry_seq_seq = self.mgmt_dev.typefactory.create(
                                        'Common.StringSequenceSequence')
            device_entry_seq_seq.values = [device_entry_seq]
            try:
                self.bigip.cluster.mgmt_dg.remove_device(
                                        [device_group_name],
                                        device_entry_seq_seq)
            except WebFault as wf:
                if not "was not found" in str(wf.message):
                    raise

    def remove_all_peers(self):
        current_dev_name = self.get_device_name()
        devs_to_remove = []
        for dev in self.get_all_device_names():
            if dev != current_dev_name:
                devs_to_remove.append(dev)
        if devs_to_remove:
            self.mgmt_trust.remove_device(devs_to_remove)
        self.remove_metadata({
                              'root_device_name': None,
                              'root_device_mgmt_address': None})

    def reset_trust(self, new_name):
        self.remove_all_peers()
        self.mgmt_trust.reset_all(new_name, False, '', '')
        self.remove_metadata({
                              'root_device_name': None,
                              'root_device_mgmt_address': None})

    def set_metadata(self, device_dict):
        local_device = self.mgmt_dev.get_local_device()
        if isinstance(device_dict, dict):
            str_comment = json.dumps(device_dict)
            self.mgmt_dev.set_description([local_device],
                                      [str_comment])
        else:
            self.mgmt_dev.set_description([local_device],
                                      [device_dict])

    def get_metadata(self, device=None):
        if not device:
            device = self.mgmt_dev.get_local_device()
        str_comment = self.mgmt_dev.get_description(
                    [device])[0]
        try:
            return json.loads(str_comment)
        except:
            return {}

    def remove_metadata(self, remove_dict, device=None):
        if not device:
            device = self.mgmt_dev.get_local_device()
        if isinstance(remove_dict, dict):
            str_comment = self.mgmt_dev.get_description(
                                    [device])[0]
            try:
                existing_dict = json.loads(str_comment)
                for key in remove_dict:
                    if key in existing_dict:
                        del(existing_dict[key])
                str_comment = json.dumps(existing_dict)
                self.mgmt_dev.set_description([device],
                                      [str_comment])
            except:
                self.mgmt_dev.set_description([device], [''])
        else:
            self.mgmt_dev.set_description([device], [''])

    def update_metadata(self, device_dict, device=None):
        if not device:
            device = self.mgmt_dev.get_local_device()
        if isinstance(device_dict, dict):
            str_comment = self.mgmt_dev.get_description(
                                    [device])[0]
            try:
                existing_dict = json.loads(str_comment)
            except:
                existing_dict = {}
            for key in device_dict:
                if not device_dict[key]:
                    if key in existing_dict:
                        del(existing_dict[key])
                else:
                    existing_dict[key] = device_dict[key]
            str_comment = json.dumps(existing_dict)
            self.mgmt_dev.set_description([device],
                                      [str_comment])
