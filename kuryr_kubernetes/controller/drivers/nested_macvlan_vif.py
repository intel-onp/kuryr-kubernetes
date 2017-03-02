# All Rights Reserved.
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

from kuryr.lib._i18n import _LE
from kuryr.lib import constants as kl_const
from kuryr.lib import exceptions as kl_exc

from neutronclient.common import exceptions as n_exc
from oslo_config import cfg as oslo_cfg
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes import config
from kuryr_kubernetes import constants as const
from kuryr_kubernetes.controller.drivers import generic_vif
from kuryr_kubernetes import exceptions as k_exc
from kuryr_kubernetes import os_vif_util as ovu


LOG = logging.getLogger(__name__)

class NestedMacvlanPodVIFDriver(generic_vif.GenericPodVIFDriver):

    def activate_vif(self, pod, vif):
        # NOTE(mchiappe): there is no way to get feedback on the actual
        # interface creation, just set 'active' for the CNI part to be able to
        # continue.
        vif.active = True

    def request_vif(self, pod, project_id, subnets, security_groups):
        neutron = clients.get_neutron_client()

        rq = self._get_port_request(pod, project_id, subnets,
                                    security_groups)

        neutron_port = neutron.create_port(rq).get('port')
        container_mac = neutron_port['mac_address']
        container_ips = neutron_port['fixed_ips']

        if not container_ips:
            raise exceptions.KuryrException(
                "Neutron port {0} does not have fixed_ips."
                .format(neutron_port['id']))

        vm_port = self._get_parent_port(neutron, pod)
        self._add_to_allowed_address_pairs(neutron, vm_port, container_ips,
                                           container_mac)

        vif_plugin = const.K8S_OS_VIF_NOOP_PLUGIN
        vif = ovu.neutron_to_osvif_vif(vif_plugin, neutron_port, subnets,
                                       nested_type=ovu.NestedType.MACVLAN)
        vif.parent_ifname = config.CONF.binding.link_iface
        return vif

    def release_vif(self, pod, vif):
        neutron = clients.get_neutron_client()
        ports = neutron.list_ports(id=vif.id)

        if ports['ports']:
            neutron_port = ports['ports'][0]
        else:
            LOG.error(_LE("Neutron port %s not found!"), vif.id)
            return # TODO(mchiappe): just return?

        container_ips = neutron_port['fixed_ips']
        vm_port = self._get_parent_port(neutron, pod)
        self._remove_from_allowed_address_pairs(neutron, vm_port, container_ips)

        try:
            neutron.delete_port(vif.id)
        except n_exc.PortNotFoundClient:
            LOG.debug('Unable to release port %s as it no longer exists.',
                      vif.id)

    def _get_port_request(self, pod, project_id, subnets, security_groups):
        port_req_body = {'project_id': project_id,
                         'name': self._get_port_name(pod),
                         'network_id': self._get_network_id(subnets),
                         'fixed_ips': ovu.osvif_to_neutron_fixed_ips(subnets),
                         'device_owner': kl_const.DEVICE_OWNER,
                         'device_id': self._get_device_id(pod),
                         'admin_state_up': True,
                         'binding:host_id': self._get_host_id(pod)}

        if security_groups:
            port_req_body['security_groups'] = security_groups

        return {'port': port_req_body}

    def _add_to_allowed_address_pairs(self, neutron, port, ip_addresses,
                                      mac_address=None):
        #Should this be moved to luryr.lib? It's now in both k8s and libnetwork kuryr
        address_pairs = port['allowed_address_pairs']
        for ip_entry in ip_addresses:
            pair = {'ip_address': ip_entry['ip_address']}
            if mac_address:
                pair['mac_address'] = mac_address
            address_pairs.append(pair)

        self._update_port_address_pairs(neutron, port['id'], address_pairs)

    def _remove_from_allowed_address_pairs(self, neutron, port, ip_addresses):
        address_pairs = port['allowed_address_pairs']
        filter = frozenset(ip_entry['ip_address'] for ip_entry in ip_addresses)
        updated_address_pairs = []

        # filter allowed IPs by copying
        for address_pair in address_pairs:
            if address_pair['ip_address'] in filter:
                continue
            updated_address_pairs.append(address_pair)

        self._update_port_address_pairs(neutron, port['id'], updated_address_pairs)

    def _update_port_address_pairs(self, neutron, port_id, address_pairs):
        try:
            neutron.update_port(
                    port_id,
                    {'port': {
                             'allowed_address_pairs': address_pairs
                    }})
        except n_exc.NeutronClientException as ex:
            LOG.error(_LE("Error happened during updating Neutron "
                "port %(port_id)s: %(ex)s"), port_id, ex)
            raise ex

    def _get_parent_port(self, neutron, pod):
        node_subnet_id = config.CONF.neutron_defaults.worker_nodes_subnet
        if not node_subnet_id:
            raise oslo_cfg.RequiredOptError('worker_nodes_subnet',
                    'neutron_defaults')

        try:
            # REVISIT(garyloug): Assumption is being made that hostIP is the IP
            #                  of the parent interface on the node(vm).
            node_fixed_ip = pod['status']['hostIP']
        except KeyError:
            if pod['status']['conditions'][0]['type'] != "Initialized":
                LOG.debug("Pod condition type is not 'Initialized'")

            LOG.error(_LE("Failed to get parent vm port ip"))
            raise kl_exc.NoResourceException

        try:
            fixed_ips = ['subnet_id=%s' % str(node_subnet_id),
                         'ip_address=%s' % str(node_fixed_ip)]
            ports = neutron.list_ports(fixed_ips=fixed_ips)
        except n_exc.NeutronClientException as ex:
            LOG.error(_LE("Parent vm port with fixed ips %s not found!"),
                fixed_ips)
            raise ex

        if ports['ports']:
            return ports['ports'][0]
        else:
            LOG.error(_LE("Neutron port for vm port with fixed ips %s"
                " not found!"), fixed_ips)
            raise kl_exc.NoResourceException

