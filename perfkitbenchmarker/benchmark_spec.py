# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Container for all data required for a benchmark to run."""

import copy
import logging
import pickle
import copy_reg
import os
import thread
import threading
import uuid

from perfkitbenchmarker import configs
from perfkitbenchmarker import context
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import flags
from perfkitbenchmarker import static_virtual_machine as static_vm
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import providers  # NOQA


def PickleLock(lock):
    return UnPickleLock, (lock.locked(),)


def UnPickleLock(locked, *args):
    lock = threading.Lock()
    if locked:
        if not lock.acquire(False):
            raise pickle.UnpicklingError("Cannot acquire lock")
    return lock


copy_reg.pickle(thread.LockType, PickleLock)
# Config constants.
VM_GROUPS = 'vm_groups'
CONFIG_FLAGS = 'flags'
DISK_COUNT = 'disk_count'
VM_COUNT = 'vm_count'
DEFAULT_COUNT = 1
CLOUD = 'cloud'
OS_TYPE = 'os_type'
STATIC_VMS = 'static_vms'
VM_SPEC = 'vm_spec'
DISK_SPEC = 'disk_spec'

GCP = 'GCP'
AZURE = 'Azure'
AWS = 'AWS'
ALICLOUD = 'AliCloud'
KUBERNETES = 'Kubernetes'
DIGITALOCEAN = 'DigitalOcean'
OPENSTACK = 'OpenStack'
CLOUDSTACK = 'CloudStack'
RACKSPACE = 'Rackspace'
DEBIAN = 'debian'
RHEL = 'rhel'
WINDOWS = 'windows'
UBUNTU_CONTAINER = 'ubuntu_container'

FLAGS = flags.FLAGS

flags.DEFINE_enum('cloud', GCP,
                  [GCP, AZURE, AWS, DIGITALOCEAN, KUBERNETES, OPENSTACK,
                   RACKSPACE, CLOUDSTACK, ALICLOUD],
                  'Name of the cloud to use.')
flags.DEFINE_enum(
    'os_type', DEBIAN, [DEBIAN, RHEL, UBUNTU_CONTAINER, WINDOWS],
    'The VM\'s OS type. Ubuntu\'s os_type is "debian" because it is largely '
    'built on Debian and uses the same package manager. Likewise, CentOS\'s '
    'os_type is "rhel". In general if two OS\'s use the same package manager, '
    'and are otherwise very similar, the same os_type should work on both of '
    'them.')
flags.DEFINE_string('scratch_dir', None,
                    'Base name for all scratch disk directories in the VM.'
                    'Upon creation, these directories will have numbers'
                    'appended to them (for example /scratch0, /scratch1, etc).')


class BenchmarkSpec(object):
  """Contains the various data required to make a benchmark run."""

  def __init__(self, benchmark_config, benchmark_name, benchmark_uid):
    """Initialize a BenchmarkSpec object.

    Args:
      benchmark_config: A Python dictionary representation of the configuration
        for the benchmark. For a complete explanation, see
        perfkitbenchmarker/configs/__init__.py.
      benchmark_name: string. Name of the benchmark.
      benchmark_uid: An identifier unique to this run of the benchmark even
        if the same benchmark is run multiple times with different configs.
    """
    self.config = benchmark_config
    self.name = benchmark_name
    self.uid = benchmark_uid
    self.vms = []
    self.networks = {}
    self.firewalls = {}
    self.networks_lock = threading.Lock()
    self.firewalls_lock = threading.Lock()
    self.vm_groups = {}
    self.deleted = False
    self.file_name = os.path.join(vm_util.GetTempDir(), self.uid)
    self.uuid = str(uuid.uuid4())
    self.always_call_cleanup = False
    self._flags = None

    # Set the current thread's BenchmarkSpec object to this one.
    context.SetThreadBenchmarkSpec(self)

  @property
  def FLAGS(self):
    """Returns the result of merging config flags with the global flags."""
    if self._flags is None:
      self._flags = configs.GetMergedFlags(self.config)
    return self._flags

  def _GetCloudForGroup(self, group_name):
    """Gets the cloud for a VM group by looking at flags and the config.

    The precedence is as follows (in decreasing order):
      * FLAGS.cloud (if specified on the command line)
      * The "cloud" key in the group config (set by a config override)
      * The "cloud" key in the group config (set by the config file)
      * FLAGS.cloud (the default value)
    """
    group_spec = self.config[VM_GROUPS][group_name]
    if not FLAGS[CLOUD].present and CLOUD in group_spec:
      return group_spec[CLOUD]
    return FLAGS.cloud

  def _GetOsTypeForGroup(self, group_name):
    """Gets the OS type for a VM group by looking at flags and the config.

    The precedence is as follows (in decreasing order):
      * FLAGS.os_type (if specified on the command line)
      * The "os_type" key in the group config (set by a config override)
      * The "os_type" key in the group config (set by the config file)
      * FLAGS.os_type (the default value)
    """
    group_spec = self.config[VM_GROUPS][group_name]
    if not FLAGS[OS_TYPE].present and OS_TYPE in group_spec:
      return group_spec[OS_TYPE]
    return FLAGS.os_type

  def ConstructVirtualMachines(self):
    """Constructs the BenchmarkSpec's VirtualMachine objects."""
    vm_group_specs = self.config[VM_GROUPS]

    for group_name, group_spec in vm_group_specs.iteritems():
      vms = []
      vm_count = group_spec.get(VM_COUNT, DEFAULT_COUNT)
      if vm_count is None:
        vm_count = FLAGS.num_vms
      disk_count = group_spec.get(DISK_COUNT, DEFAULT_COUNT)

      try:
        # First create the Static VMs.
        if STATIC_VMS in group_spec:
          static_vm_specs = group_spec[STATIC_VMS][:vm_count]
          for spec_kwargs in static_vm_specs:
            vm_spec = static_vm.StaticVmSpec(**spec_kwargs)
            static_vm_class = static_vm.GetStaticVmClass(vm_spec.os_type)
            vms.append(static_vm_class(vm_spec))

        os_type = self._GetOsTypeForGroup(group_name)
        cloud = self._GetCloudForGroup(group_name)

        # Then create a VmSpec and possibly a DiskSpec which we can
        # use to create the remaining VMs.
        vm_spec_class = virtual_machine.GetVmSpecClass(cloud)
        vm_spec = vm_spec_class(**group_spec[VM_SPEC][cloud])

        if DISK_SPEC in group_spec:
          disk_spec_class = disk.GetDiskSpecClass(cloud)
          disk_spec = disk_spec_class(**group_spec[DISK_SPEC][cloud])
          disk_spec.ApplyFlags(FLAGS)
        else:
          disk_spec = None

      except TypeError as e:
        # This is what we get if one of the kwargs passed into a spec's
        # __init__ method was unexpected.
        raise ValueError(
            'Config contained an unexpected parameter. Error message:\n%s' % e)

      # Create the remaining VMs using the specs we created earlier.
      for _ in xrange(vm_count - len(vms)):
        vm_spec.ApplyFlags(FLAGS)
        vm = self._CreateVirtualMachine(vm_spec, os_type, cloud)
        if disk_spec:
          vm.disk_specs = [copy.copy(disk_spec) for _ in xrange(disk_count)]
          # In the event that we need to create multiple disks from the same
          # DiskSpec, we need to ensure that they have different mount points.
          if (disk_count > 1 and disk_spec.mount_point):
            for i, spec in enumerate(vm.disk_specs):
              spec.mount_point += str(i)
        vms.append(vm)

      self.vm_groups[group_name] = vms
      self.vms.extend(vms)

  def Prepare(self):
    """Prepares the VMs and networks necessary for the benchmark to run."""
    vm_util.RunThreaded(lambda net: net.Create(), self.networks.values())

    if self.vms:
      vm_util.RunThreaded(self.PrepareVm, self.vms)
      if FLAGS.os_type != WINDOWS:
        vm_util.GenerateSSHConfig(self.vms)

  def Delete(self):
    if FLAGS.run_stage not in ['all', 'cleanup'] or self.deleted:
      return

    if self.vms:
      try:
        vm_util.RunThreaded(self.DeleteVm, self.vms)
      except Exception:
        logging.exception('Got an exception deleting VMs. '
                          'Attempting to continue tearing down.')

    for firewall in self.firewalls.itervalues():
      try:
        firewall.DisallowAllPorts()
      except Exception:
        logging.exception('Got an exception disabling firewalls. '
                          'Attempting to continue tearing down.')

    for net in self.networks.itervalues():
      try:
        net.Delete()
      except Exception:
        logging.exception('Got an exception deleting networks. '
                          'Attempting to continue tearing down.')
    self.deleted = True

  def _CreateVirtualMachine(self, vm_spec, os_type, cloud):
    """Create a vm in zone.

    Args:
      vm_spec: A virtual_machine.BaseVmSpec object.
      os_type: The type of operating system for the VM. See the flag of the
          same name for more information.
      cloud: The cloud for the VM. See the flag of the same name for more
          information.
    Returns:
      A virtual_machine.BaseVirtualMachine object.
    """
    vm = static_vm.StaticVirtualMachine.GetStaticVirtualMachine()
    if vm:
      return vm

    vm_class = virtual_machine.GetVmClass(cloud, os_type)
    if vm_class is None:
      raise errors.Error(
          'VMs of type %s" are not currently supported on cloud "%s".' %
          (os_type, cloud))

    return vm_class(vm_spec)

  def PrepareVm(self, vm):
    """Creates a single VM and prepares a scratch disk if required.

    Args:
        vm: The BaseVirtualMachine object representing the VM.
    """
    vm.Create()
    logging.info('VM: %s', vm.ip_address)
    logging.info('Waiting for boot completion.')
    for port in vm.remote_access_ports:
      vm.AllowPort(port)
    vm.AddMetadata(benchmark=self.name, perfkit_uuid=self.uuid,
                   benchmark_uid=self.uid)
    vm.WaitForBootCompletion()
    vm.OnStartup()
    if any((spec.disk_type == disk.LOCAL for spec in vm.disk_specs)):
      vm.SetupLocalDisks()
    for disk_spec in vm.disk_specs:
      vm.CreateScratchDisk(disk_spec)

    # This must come after Scratch Disk creation to support the
    # Containerized VM case
    vm.PrepareVMEnvironment()

  def DeleteVm(self, vm):
    """Deletes a single vm and scratch disk if required.

    Args:
        vm: The BaseVirtualMachine object representing the VM.
    """
    if vm.is_static and vm.install_packages:
      vm.PackageCleanup()
    vm.Delete()
    vm.DeleteScratchDisks()

  def PickleSpec(self):
    """Pickles the spec so that it can be unpickled on a subsequent run."""
    # FlagValues objects can't be pickled without getting an error.
    flags, self._flags = self._flags, None
    with open(self.file_name, 'wb') as pickle_file:
      pickle.dump(self, pickle_file, 2)
    self._flags = flags

  @classmethod
  def GetSpecFromFile(cls, name):
    """Unpickles the spec and returns it.

    Args:
      name: The name of the benchmark (and the name of the pickled file).

    Returns:
      A BenchmarkSpec object.
    """
    file_name = '%s/%s' % (vm_util.GetTempDir(), name)
    try:
      with open(file_name, 'rb') as pickle_file:
        spec = pickle.load(pickle_file)
    except Exception as e:  # pylint: disable=broad-except
      logging.error('Unable to unpickle spec file for benchmark %s.', name)
      raise e
    # Always let the spec be deleted after being unpickled so that
    # it's possible to run cleanup even if cleanup has already run.
    spec.deleted = False
    context.SetThreadBenchmarkSpec(spec)
    return spec
