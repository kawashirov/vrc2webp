import contextlib
import os
import typing
import subprocess
import logging
from pathlib import Path

import psutil
import yaml

from .util import log_path, log_exc

if typing.TYPE_CHECKING:
	from collections.abc import Container

LOG = logging.getLogger('vrc2webp')


class ConfigError(RuntimeError):
	pass


class Config:
	CPU_PRIORITIES = ('above_normal', 'normal', 'below_normal', 'idle')
	CPU_PRIORITIES_SUBPROCESS = {
		'above_normal': subprocess.ABOVE_NORMAL_PRIORITY_CLASS,
		'normal': subprocess.NORMAL_PRIORITY_CLASS,
		'below_normal': subprocess.BELOW_NORMAL_PRIORITY_CLASS,
		'idle': subprocess.IDLE_PRIORITY_CLASS}
	CPU_PRIORITIES_PSUTIL = {
		'above_normal': psutil.ABOVE_NORMAL_PRIORITY_CLASS,
		'normal': psutil.NORMAL_PRIORITY_CLASS,
		'below_normal': psutil.BELOW_NORMAL_PRIORITY_CLASS,
		'idle': psutil.IDLE_PRIORITY_CLASS}
	IO_PRIORITIES = ('normal', 'low', 'very_low')
	IO_PRIORITIES_PSUTIL = {
		'normal': psutil.IOPRIO_NORMAL,
		'low': psutil.IOPRIO_LOW,
		'very_low': psutil.IOPRIO_VERYLOW}
	WATCH_MODES = ('observe', 'scan', 'both')

	own_priority_cpu: 'str'
	recoders_priority_cpu: 'str'
	own_priority_io: 'str'
	recoders_priority_io: 'str'
	watch_paths: 'list[Path]'
	watch_mode: 'str'
	randomize_scan: 'bool'
	recursive: 'bool'
	new_files_timeout: 'float'
	file_extensions: 'list[str]'
	ignore_files_less_than: 'int'
	ignore_files_greater_than: 'int'
	ignore_objects_hidden: 'bool'
	ignore_objects_readonly: 'bool'
	ignore_objects_system: 'bool'
	ignore_objects_name_stats_with: 'list[str]'
	ignore_objects_name_ends_with: 'list[str]'
	ignore_objects_path_contains: 'list[str]'
	vrc_swap_resolution_and_time: 'bool'
	max_parallel_recodes: 'int'
	update_mtime: 'bool'
	delete_mode: 'str'

	@classmethod
	@contextlib.contextmanager
	def _get_context(cls, raw_config: 'dict', key: 'str', types=None):
		value = None
		try:
			value = raw_config[key]
			if types and not isinstance(value, types):
				raise TypeError(f"Invalid type of {key!r}: {type(value).__name__}: {value!r}")
			yield value
		except Exception as exc:
			raise ConfigError(f"Invalid value of {key!r}: {type(value).__name__}: {value!r}") from exc

	@classmethod
	def _get_bool(cls, raw_config: 'dict', key: 'str') -> 'bool':
		with cls._get_context(raw_config, key) as value:
			if isinstance(value, (bool, int)):
				return bool(value)
			else:
				value_trim = str(value).strip().lower()
				if value_trim in ('1', 'true', 'yes', 'y'):
					return True
				elif value_trim in ('0', 'false', 'no', 'n'):
					return False
			raise ValueError()

	@classmethod
	def _get_int(cls, raw_config: 'dict', key: 'str') -> 'int':
		with cls._get_context(raw_config, key) as value:
			return value if isinstance(value, int) else int(str(value).strip())

	@classmethod
	def _get_float(cls, raw_config: 'dict', key: 'str') -> 'float':
		with cls._get_context(raw_config, key) as value:
			return float(value) if isinstance(value, (int, float)) else float(str(value).strip())

	@classmethod
	def _get_size_bytes(cls, raw_config: 'dict', key: 'str') -> 'int':
		with cls._get_context(raw_config, key) as value:
			value = str(value).lower()
			if value.endswith('m'):
				return round(float(value[:-1]) * 1024 * 1024)
			elif value.endswith('k'):
				return round(float(value[:-1]) * 1024)
			else:
				return int(value)

	@classmethod
	def _get_keyword(cls, raw_config: 'dict', key: 'str', keywords: 'Container[str]') -> 'str':
		with cls._get_context(raw_config, key) as value:
			value = str(value).lower()
			if value in keywords:
				return value
			raise ValueError(f"{value!r} not in allowed keywords: {keywords!r}")

	@classmethod
	def _get_list_strs(cls, raw_config: 'dict', key: 'str') -> 'list[str]':
		with cls._get_context(raw_config, key, types=list) as value:
			for i in range(len(value)):
				value[i] = str(value[i]).strip()
			return value

	@classmethod
	def _get_paths(cls, raw_config: 'dict', key: 'str') -> 'list[Path]':
		with cls._get_context(raw_config, key, types=list) as value:
			if len(value) < 1:
				raise ValueError(f"No items in {key!r}.")
			for i in range(len(value)):
				value[i] = Path(os.path.expandvars(str(value[i])))
			return value

	@classmethod
	def _get_file_extensions(cls, raw_config: 'dict', key: 'str') -> 'list[str]':
		with cls._get_context(raw_config, key, types=list) as value:
			for i in range(len(value)):
				filext = str(value[i]).lower().strip()
				if not filext.startswith('.'):
					raise ValueError(f"Item '{key}[{i}]' does not starts with dot (.): {filext}")
				value[i] = filext
			return value

	def load_yaml_file(self, path: 'Path'):
		try:
			raw_config: 'dict|None' = None
			with open(path, 'rt') as stream:
				raw_config: 'dict' = yaml.safe_load(stream)
				if not isinstance(raw_config, dict):
					raise ConfigError(f"Invalid config structure: root is not dict, but {type(raw_config).__name__}")
			self.own_priority_cpu = self._get_keyword(raw_config, 'own-priority-cpu', self.CPU_PRIORITIES)
			self.recoders_priority_cpu = self._get_keyword(raw_config, 'recoders-priority-cpu', self.CPU_PRIORITIES)
			self.own_priority_io = self._get_keyword(raw_config, 'own-priority-io', self.IO_PRIORITIES)
			self.recoders_priority_io = self._get_keyword(raw_config, 'recoders-priority-io', self.IO_PRIORITIES)
			self.watch_paths = self._get_paths(raw_config, 'watch-paths')
			self.watch_mode = self._get_keyword(raw_config, 'watch-mode', self.WATCH_MODES)
			self.randomize_scan = self._get_bool(raw_config, 'randomize-scan')
			# self.recursive = self._get_bool(raw_config, 'recursive')
			self.new_files_timeout = self._get_float(raw_config, 'new-files-timeout')
			self.file_extensions = self._get_file_extensions(raw_config, 'file-extensions')
			self.ignore_files_less_than = self._get_size_bytes(raw_config, 'ignore-files-less-than')
			self.ignore_files_greater_than = self._get_size_bytes(raw_config, 'ignore-files-greater-than')
			self.ignore_objects_hidden = self._get_bool(raw_config, 'ignore-objects-hidden')
			self.ignore_objects_readonly = self._get_bool(raw_config, 'ignore-objects-readonly')
			self.ignore_objects_system = self._get_bool(raw_config, 'ignore-objects-system')
			self.ignore_objects_name_stats_with = self._get_list_strs(raw_config, 'ignore-objects-name-stats-with')
			self.ignore_objects_name_ends_with = self._get_list_strs(raw_config, 'ignore-objects-name-ends-with')
			self.ignore_objects_path_contains = self._get_list_strs(raw_config, 'ignore-objects-path-contains')
			self.vrc_swap_resolution_and_time = self._get_bool(raw_config, 'vrc-swap-resolution-and-time')
			self.max_parallel_recodes = self._get_int(raw_config, 'max-parallel-recodes')
			self.update_mtime = self._get_bool(raw_config, 'update-mtime')
			self.delete_mode = self._get_keyword(raw_config, 'delete-mode', ('keep', 'trash', 'trash'))
			return self
		except Exception as exc:
			msg = f"Failed to load config {log_path(path)}: {log_exc(exc)}"
			LOG.error(msg, exc_info=exc)
			raise RuntimeError(msg)

	def own_priority_cpu_psutil(self):
		return self.CPU_PRIORITIES_PSUTIL[self.own_priority_cpu]

	def own_priority_io_psutil(self):
		return self.IO_PRIORITIES_PSUTIL[self.own_priority_io]

	def recoders_priority_cpu_subprocess(self):
		return self.CPU_PRIORITIES_SUBPROCESS[self.recoders_priority_cpu]

	def recoders_priority_cpu_psutil(self):
		return self.CPU_PRIORITIES_PSUTIL[self.recoders_priority_cpu]

	def recoders_priority_io_psutil(self):
		return self.IO_PRIORITIES_PSUTIL[self.recoders_priority_io]
