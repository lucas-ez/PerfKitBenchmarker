# Copyright 2014 Google Inc. All rights reserved.
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
import logging

from perfkitbenchmarker import flags
from perfkitbenchmarker import resource

FLAGS = flags.FLAGS

# These are the new disk type names
EPHEMERAL_HDD = 'ephemeral_hdd'
EPHEMERAL_SSD = 'ephemeral_ssd'
BUILDING_REPLICATED_HDD = 'building_replicated_hdd'
BUILDING_REPLICATED_SSD = 'building_replicated_ssd'


def DiskTypeIsLocal(disk_type):
  return disk_type in {EPHEMERAL_HDD, EPHEMERAL_SSD}


NEW_DISK_TYPE_NAMES = {
    EPHEMERAL_HDD,
    EPHEMERAL_SSD,
    BUILDING_REPLICATED_HDD,
    BUILDING_REPLICATED_SSD}

# These are the (deprecated) old disk type names
STANDARD = 'standard'
REMOTE_SSD = 'remote_ssd'
PIOPS = 'piops'  # Provisioned IOPS (SSD) in AWS
LOCAL = 'local'

# Map old disk type names to new disk type names
DISK_TYPES_MAPPING = {
    STANDARD: BUILDING_REPLICATED_HDD,
    REMOTE_SSD: BUILDING_REPLICATED_SSD,
    PIOPS: BUILDING_REPLICATED_SSD,
    LOCAL: EPHEMERAL_SSD
}


def WarnAndTranslateDiskTypes(name):
  """Translate old disk types to new disk types, printing warnings if needed.

  Args:
    name: a string specifying a disk type, either new or old.

  Returns:
    The disk type to use, in the new disk type taxonomy.

  Raises:
    ValueError, if name is not a disk type name.
  """

  if name in NEW_DISK_TYPE_NAMES:
    return name
  elif name in DISK_TYPES_MAPPING:
    new_name = DISK_TYPES_MAPPING[name]
    logging.warning('Disk type name %s is deprecated and will be removed. '
                    'Translating to %s for now.', name, new_name)
    return new_name
  else:
    raise ValueError('%s is not a disk type name', name)


def WarnAndCopyFlag(old_name, new_name, translator=None):
  """Copy a value from an old flag to a new one, warning the user.
  """

  if FLAGS[old_name].present:
    logging.warning('Flag --%s is deprecated and will be removed. Please '
                    'switch to --%s.' % (old_name, new_name))
    if not FLAGS[new_name].present:
      if translator:
        FLAGS[new_name].value = translator(FLAGS[old_name].value)
      else:
        FLAGS[new_name].value = FLAGS[old_name].value

      # Mark the new flag as present so we'll print it out in our list
      # of flag values.
      FLAGS[new_name].present = True
    # Remove the old flag so we can't accidentally use it.
    del FLAGS.FlagDict()[old_name]


def WarnAndTranslateDiskFlags():
  """Translate old disk-related flags to new disk-related flags.
  """

  WarnAndCopyFlag('scratch_disk_type', 'disk_type',
                  translator=WarnAndTranslateDiskTypes)

  WarnAndCopyFlag('scratch_disk_iops', 'aws_provisioned_iops')

  WarnAndCopyFlag('scratch_disk_size', 'disk_size')


class BaseDiskSpec(object):
  """Stores the information needed to create a disk."""

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
    self.disk_size = flags.disk_size or self.disk_size
    self.disk_type = flags.disk_type or self.disk_type
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
