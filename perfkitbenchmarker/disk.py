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

"""Module containing abstract classes related to disks.

Disks can be created, deleted, attached to VMs, and detached from VMs.
"""

import abc

from perfkitbenchmarker import resource

STANDARD = 'standard'
REMOTE_SSD = 'remote_ssd'
PIOPS = 'piops'  # Provisioned IOPS (SSD) in AWS
LOCAL = 'local'

_DISK_SPEC_REGISTRY = {}


def GetDiskSpecClass(cloud):
  """Get the DiskSpec class corresponding to 'cloud'."""
  return _DISK_SPEC_REGISTRY.get(cloud, BaseDiskSpec)


class AutoRegisterDiskSpecMeta(type):
  """Metaclass which automatically registers DiskSpecs."""

  def __init__(cls, name, bases, dct):
    if cls.CLOUD in _DISK_SPEC_REGISTRY:
      raise Exception('BaseDiskSpec subclasses must have a CLOUD attribute.')
    else:
      _DISK_SPEC_REGISTRY[cls.CLOUD] = cls
    super(AutoRegisterDiskSpecMeta, cls).__init__(name, bases, dct)


class BaseDiskSpec(object):
  """Stores the information needed to create a disk."""

  __metaclass__ = AutoRegisterDiskSpecMeta
  CLOUD = None

  def __init__(self, disk_size=None, disk_type=None,
               mount_point=None, num_striped_disks=1,
               disk_number=None, device_path=None):
    """Initializes the DiskSpec object.

    Args:
      disk_size: Size of the disk in GB.
      disk_type: Disk types in string. See cloud specific disk classes for
          more information on acceptable values.
      mount_point: Directory of mount point in string.
      num_striped_disks: The number of disks to stripe together. If this is 1,
          it means no striping will occur. This must be >= 1.
    """
    self.disk_size = disk_size
    self.disk_type = disk_type
    self.mount_point = mount_point
    self.num_striped_disks = num_striped_disks
    self.disk_number = disk_number
    self.device_path = device_path

  def ApplyFlags(self, flags):
    """Applies flags to the DiskSpec."""
    self.disk_size = flags.scratch_disk_size or self.disk_size
    self.disk_type = flags.scratch_disk_type or self.disk_type
    self.num_striped_disks = flags.num_striped_disks or self.num_striped_disks
    self.mount_point = flags.scratch_dir or self.mount_point


class BaseDisk(resource.BaseResource):
  """Object representing a Base Disk."""

  is_striped = False

  def __init__(self, disk_spec):
    super(BaseDisk, self).__init__()
    self.disk_size = disk_spec.disk_size
    self.disk_type = disk_spec.disk_type
    self.mount_point = disk_spec.mount_point
    self.num_striped_disks = disk_spec.num_striped_disks

    # Linux related attributes.
    self.device_path = disk_spec.device_path

    # Windows related attributes.

    # The disk number corresponds to the order in which disks were attached to
    # the instance. The System Disk has a disk number of 0. Any local disks
    # have disk numbers ranging from 1 to the number of local disks on the
    # system. Any additional disks that were attached after boot will have
    # disk numbers starting at the number of local disks + 1. These disk
    # numbers are used in diskpart scripts in order to identify the disks
    # that we want to operate on.
    self.disk_number = disk_spec.disk_number

  @abc.abstractmethod
  def Attach(self, vm):
    """Attaches the disk to a VM.

    Args:
      vm: The BaseVirtualMachine instance to which the disk will be attached.
    """
    pass

  @abc.abstractmethod
  def Detach(self):
    """Detaches the disk from a VM."""
    pass

  def GetDevicePath(self):
    """Returns the path to the device inside a Linux VM."""
    return self.device_path


class StripedDisk(BaseDisk):
  """Object representing several disks striped together."""

  is_striped = True

  def __init__(self, disk_spec, disks):
    """Initializes a StripedDisk object.

    Args:
      disk_spec: A BaseDiskSpec containing the desired mount point.
      disks: A list of BaseDisk objects that constitute the StripedDisk.
      device_path: The path of the striped device in a Linux VM.
    """
    super(StripedDisk, self).__init__(disk_spec)
    self.disks = disks

  def _Create(self):
    for disk in self.disks:
      disk.Create()

  def _Delete(self):
    for disk in self.disks:
      disk.Delete()

  def Attach(self, vm):
    for disk in self.disks:
      disk.Attach(vm)

  def Detach(self):
    for disk in self.disks:
      disk.Detach()
