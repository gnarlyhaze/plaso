#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2012 The Plaso Project Authors.
# Please see the AUTHORS file for details on individual authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This file contains classes used for preprocessing in plaso."""

import abc
import logging

from plaso.lib import errors
from plaso.lib import registry
from plaso.parsers.plist_plugins import interface as plist_interface
from plaso.winreg import cache
from plaso.winreg import path_expander as winreg_path_expander
from plaso.winreg import winregistry

from binplist import binplist
from xml.etree import ElementTree


class PreprocessPlugin(object):
  """Class that defines the preprocess plugin object interface.

  Any preprocessing plugin that implements this interface
  should define which operating system this plugin supports.

  The OS variable supports the following values:
    + Windows
    + Linux
    + MacOSX

  Since some plugins may require knowledge gained from
  other checks all plugins have a weight associated to it.
  The weight variable can have values from one to three:
    + 1 - Requires no prior knowledge, can run immediately.
    + 2 - Requires knowledge from plugins with weight 1.
    + 3 - Requires knowledge from plugins with weight 2.

  The default weight of 3 is assigned to plugins, so each
  plugin needs to overwrite that value if needed.

  The plugins are grouped by the operating system they work
  on and then on their weight. That means that if the tool
  is run against a Windows system all plugins that support
  Windows are grouped together, and only plugins with weight
  one are run, then weight two followed by the rest of the
  plugins with the weight of three. There is no priority or
  guaranteed order of plugins that have the same weight, which
  makes it important to define the weight appropriately.
  """
  __metaclass__ = registry.MetaclassRegistry
  __abstract = True

  # Defines the OS that this plugin supports.
  SUPPORTED_OS = []
  # Weight is an INT, with the value of 1-3.
  WEIGHT = 3

  # Defines the preprocess attribute to be set.
  ATTRIBUTE = ''

  def __init__(self, obj_store):
    """Initializes the preprocess plugin object.

    Args:
      obj_store: the object store.
    """
    super(PreprocessPlugin, self).__init__()
    self._obj_store = obj_store

  def Run(self, collector):
    """Set the attribute of the object store to the value from GetValue.

    Args:
      collector: the preprocess collector (instance of PreprocessCollector).
    """
    setattr(self._obj_store, self.ATTRIBUTE, self.GetValue(collector))
    logging.info(u'[PreProcess] Set attribute: {0:s} to {1:s}'.format(
        self.ATTRIBUTE, getattr(self._obj_store, self.ATTRIBUTE, u'N/A')))

  @abc.abstractmethod
  def GetValue(self, collector):
    """Return the value for the attribute.

    Args:
      collector: the preprocess collector (instance of PreprocessCollector).
    """
    raise NotImplementedError

  @property
  def plugin_name(self):
    """Return the name of the plugin."""
    return self.__class__.__name__


class MacPlistPreprocess(PreprocessPlugin):
  """Class that defines the Mac OS X plist preprocess plugin object."""
  __abstract = True

  SUPPORTED_OS = ['MacOSX']
  WEIGHT = 2

  # Path to the plist file to be parsed, can depend on paths discovered
  # in previous preprocessors.
  PLIST_PATH = ''

  # The key that's value should be returned back. It is an ordered list
  # of preference. If the first value is found it will be returned and no
  # others will be searched.
  PLIST_KEYS = ['']

  def GetValue(self, collector):
    """Return the value gathered from a plist file for preprocessing.

    Args:
      collector: the preprocess collector (instance of PreprocessCollector).
    """
    try:
      file_path, _, file_name = self.PLIST_PATH.rpartition(u'/')
      paths = collector.GetFilePaths(file_path, file_name, u'/')
    except errors.PathNotFound as exception:
      raise errors.PreProcessFail(u'Unable to find path: {0:s}'.format(
          exception))

    if not paths:
      raise errors.PreProcessFail(u'Unable to find path: {0:s}'.format(
          self.PLIST_PATH))

    try:
      path = paths.next()
    except StopIteration:
      raise errors.PreProcessFail(u'Unable to open file: Path not found.')

    try:
      file_entry = collector.OpenFileEntry(path)
      file_object = file_entry.GetFileObject()
    except IOError as exception:
      raise errors.PreProcessFail(
          u'Unable to open file: {0:s} with error: {1:s}'.format(
              path, exception))

    return self.ParseFile(file_entry, file_object)

  def ParseFile(self, file_entry, file_object):
    """Parses the plist file and returns the parsed key."""
    try:
      plist_file = binplist.BinaryPlist(file_object)
      top_level_object = plist_file.Parse()
    except binplist.FormatError as exception:
      raise errors.PreProcessFail(
          u'File is not a plist: {0:s} with error: {1:s}'.format(
              file_entry.path_spec.comparable, exception))
    except OverflowError as exception:
      raise errors.PreProcessFail(
          u'Unable to process plist: {0:s} with error: {1:s}'.format(
              file_entry.path_spec.comparable, exception))

    if not plist_file:
      raise errors.PreProcessFail(
          u'File is not a plist: {0:s}'.format(file_entry.path_spec.comparable))

    match = None
    key_name = ''
    for plist_key in self.PLIST_KEYS:
      match = plist_interface.GetKeys(
          top_level_object, frozenset([plist_key]))
      if match:
        key_name = plist_key
        break

    if not match:
      raise errors.PreProcessFail(
          u'No plist keys found, trying to locate: {0:s}'.format(','.join(
              self.PLIST_KEYS)))

    return self.ParseKey(match, key_name)

  def ParseKey(self, key, key_name):
    """Fetch the first discovered key from PLIST_KEYS and return value."""
    value = key.get(key_name, None)
    if not value:
      raise errors.PreProcessFail(u'Value not found.')

    return value


class MacXMLPlistPreprocess(MacPlistPreprocess):
  """Class that defines the Mac OS X XML plist preprocess plugin object."""
  __abstract = True

  def ParseFile(self, file_entry, file_object):
    """Parse the file and return parsed key."""
    # TODO: Move to defusedxml for safer XML parsing.
    try:
      xml = ElementTree.parse(file_object)
    except ElementTree.ParseError:
      raise errors.PreProcessFail(u'File is not a XML file.')
    except IOError:
      raise errors.PreProcessFail(u'File is not a XML file.')

    xml_root = xml.getroot()
    key_name = ''
    match = None
    for key in self.PLIST_KEYS:
      match = self._GetKeys(xml_root, key)
      if match:
        key_name = key
        break

    if not match:
      raise errors.PreProcessFail(
          u'Keys not found inside plist file: {0:s}.'.format(
              u','.join(self.PLIST_KEYS)))

    return self.ParseKey(match, key_name)

  def _GetKeys(self, xml_root, key_name):
    """Return a dict with the requested keys."""
    match = {}

    generator = xml_root.iter()
    for key in generator:
      if 'key' in key.tag and key_name in key.text:
        value_key = generator.next()
        value = ''
        for subkey in value_key.iter():
          if 'string' in subkey.tag:
            value = subkey.text
        match[key.text] = value

    # Now we need to go over the match dict and retrieve values.
    return match


class WinRegistryPreprocess(PreprocessPlugin):
  """Class that defines the Windows Registry preprocess plugin object.

  By default registry needs information about system paths, which excludes
  them to run in priority 1, in some cases they may need to run in priority
  3, for instance if the registry key is dependent on which version of Windows
  is running, information that is collected during priority 2.
  """
  __abstract = True

  SUPPORTED_OS = ['Windows']
  WEIGHT = 2

  REG_KEY = '\\'
  REG_PATH = '{sysregistry}'
  REG_FILE = 'SOFTWARE'

  def __init__(self, obj_store):
    """Initializes the Window Registry preprocess plugin object.

    Args:
      obj_store: the object store.
    """
    super(WinRegistryPreprocess, self).__init__(obj_store)
    self._path_expander = None

  def GetValue(self, collector):
    """Return the value gathered from a registry key for preprocessing.

    Args:
      collector: the preprocess collector (instance of PreprocessCollector).
    """
    sys_dirs = list(collector.FindPaths(self.REG_PATH))
    if not sys_dirs:
      raise errors.PreProcessFail(
          u'Unable to find file name: {0:s}/{1:s}'.format(
              self.REG_PATH, self.REG_FILE))

    sys_dir = sys_dirs[0]
    file_names = list(collector.GetFilePaths(
        sys_dir, self.REG_FILE, sys_dir[0]))
    if not file_names:
      raise errors.PreProcessFail(
          u'Unable to find file name: {0:s}/{1:s}'.format(
              sys_dir, self.REG_FILE))
    if len(file_names) != 1:
      raise errors.PreProcessFail(
          u'Found more than a single file for: {0:s}/{1:s} [{2:d}]'.format(
              sys_dir, self.REG_FILE, len(file_names)))

    file_name = file_names[0]

    try:
      file_entry = collector.OpenFileEntry(file_name)
      file_object = file_entry.GetFileObject()
      file_object.close()
    except IOError as exception:
      raise errors.PreProcessFail(
          u'Unable to open file: {0:s} with error: {1:s}'.format(
              file_name, exception))

    codepage = getattr(self._obj_store, 'code_page', 'cp1252')

    win_registry = winregistry.WinRegistry(
        winregistry.WinRegistry.BACKEND_PYREGF)

    try:
      winreg_file = win_registry.OpenFile(file_entry, codepage=codepage)
    except IOError as exception:
      raise errors.PreProcessFail(
          u'Unable to open Registry file: {0:s} with error: {1:s}'.format(
              file_name, exception))

    self.winreg_file = winreg_file

    if self._path_expander is None:
      reg_cache = cache.WinRegistryCache()
      reg_cache.BuildCache(winreg_file, self.REG_FILE)
      self._path_expander = winreg_path_expander.WinRegistryKeyPathExpander(
          self._obj_store, reg_cache)

    try:
      key_path = self._path_expander.ExpandPath(self.REG_KEY)
    except KeyError:
      key_path = u''

    try:
      key = winreg_file.GetKeyByPath(key_path)
    except IOError as exception:
      raise errors.PreProcessFail(
          u'Unable to fetch Registry key: {0:s} with error: {1:s}'.format(
              key_path, exception))

    if not key:
      raise errors.PreProcessFail(
          u'Registry key {0:s} does not exist.'.format(self.REG_KEY))

    return self.ParseKey(key)

  @abc.abstractmethod
  def ParseKey(self, key):
    """Extract information from a registry key and save in storage."""


class PreprocessGetPath(PreprocessPlugin):
  """Return a simple path."""
  __abstract = True

  WEIGHT = 1
  ATTRIBUTE = 'nopath'
  PATH = 'doesnotexist'

  def GetValue(self, collector):
    """Return the path as found by the collector.

    Args:
      collector: the preprocess collector (instance of PreprocessCollector).
    """
    try:
      paths = list(collector.FindPaths(self.PATH))
    except errors.PathNotFound as exception:
      raise errors.PreProcessFail(u'Unable to find path: {0:s}'.format(
          exception))
    if paths:
      return paths[0]


def GuessOS(col_obj):
  """Return a string representing what we think the underlying OS is.

  The available return strings are:
    + Windows
    + MacOSX
    + Linux

  Args:
    col_obj: The collection object.

  Returns:
     A string indicating which OS we are dealing with.
  """
  # TODO: Add error handling for WindowsError, a builtin
  # error in Windows, but not found otherwise (so no global error exists).
  # This causes the tool to crash on Windows if preprocessor is unable to
  # guess the OS, like when accidentally run against a directory.
  try:
    if list(col_obj.FindPaths(u'/(Windows|WINNT)/System32')):
      return 'Windows'
  except (OSError, errors.PathNotFound):
    pass

  try:
    if list(col_obj.FindPaths(u'/System/Library')):
      return 'MacOSX'
  except (OSError, errors.PathNotFound):
    pass

  try:
    if list(col_obj.FindPaths(u'/etc')):
      return 'Linux'
  except (OSError, errors.PathNotFound):
    pass

  return 'None'