# nuitka-project: --onefile
# nuitka-project: --windows-company-name=kawashirov
# nuitka-project: --windows-product-name=vrc2webp
# nuitka-project: --windows-file-version=0.1.1.0
# nuitka-project: --windows-product-version=0.1.1.0
# nuitka-project: --include-data-dir=assets=assets
# nuitka-project: --python-flag=-O
# nuitka-project: --onefile-tempdir-spec=%TEMP%\vrc2webp_%PID%_%TIME%
# nuitka-project: --windows-icon-from-ico=logo\logo.ico

# Т.к. мы только на шындовс, можно выкинуть ненужное.
# nuitka-project: --nofollow-import-to=psutil._pslinux
# nuitka-project: --nofollow-import-to=psutil._psosx
# nuitka-project: --nofollow-import-to=psutil._psbsd
# nuitka-project: --nofollow-import-to=psutil._pssunos
# nuitka-project: --nofollow-import-to=psutil._psaix
#
# nuitka-project: --nofollow-import-to=send2trash.plat_osx
# nuitka-project: --nofollow-import-to=send2trash.plat_gio
#
# nuitka-project: --nofollow-import-to=watchdog.observers.inotify
# nuitka-project: --nofollow-import-to=watchdog.observers.fsevents
# nuitka-project: --nofollow-import-to=watchdog.observers.kqueue


import argparse
import asyncio
import collections
import contextlib
import functools
import heapq
import logging
import os
import random
import re
import time
import typing
import signal
import shutil
import stat
import subprocess
import sys

from pathlib import Path
from asyncio.subprocess import create_subprocess_exec

# Deps
import psutil
import send2trash
import watchdog.observers
import watchdog.events
import yaml

if typing.TYPE_CHECKING:
	from collections.abc import Container, Coroutine

APP_VERION = '0.1.1'
APP_DEBUG = bool(hasattr(sys, 'gettrace') and sys.gettrace())
LOG = logging.getLogger('vrc2webp')


def is_debug():
	return APP_DEBUG


def log_path(path: 'Path|None'):
	return repr(path.as_posix()) if isinstance(path, Path) else repr(path)


def log_exc(exc: 'BaseException'):
	t = type(exc)
	name = getattr(t, '__qualname__', None) or getattr(t, '__name__', None)
	if not name:
		return str(exc)
	if not (module := getattr(t, '__module__', None)):
		return f"{name}: {exc!s}"
	return f"{module}.{name}: {exc!s}"


def sizeof_fmt(num, suffix="B"):
	for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
		if abs(num) < 1024.0:
			return f"{num:3.2f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.2f}Yi{suffix}"


async def wait_any(*aws):
	# asyncio.wait не принимает корутины
	fs = list()
	tmp_tasks = list()
	try:
		for awt in aws:
			if asyncio.isfuture(awt):
				fs.append(awt)
			elif asyncio.iscoroutine(awt):
				t = asyncio.create_task(awt)
				fs.append(t)
				tmp_tasks.append(t)
			else:
				raise TypeError(f"{type(aws)!r} {aws!r}")
		await asyncio.wait(fs, return_when=asyncio.FIRST_COMPLETED)
	finally:
		for t in tmp_tasks:
			t.cancel()


async def process_communicate(process: 'asyncio.subprocess.Process'):
	stdout_data, _ = await process.communicate()
	if not stdout_data:
		stdout_data = b''
	return stdout_data.decode('ascii').replace('\n', ' ').strip()


async def safe_stat(path: 'Path', not_found_ok=False) -> 'os.stat_result|None':
	try:
		if is_debug():
			LOG.debug(f"os.stat({log_path(path)})...")
		return await asyncio.to_thread(os.stat, path)
	except OSError as exc:
		if not_found_ok and isinstance(exc, FileNotFoundError):
			return None
		LOG.warning(f"Failed to os.stat({log_path(path)}): {exc!s}")
	return None


async def path_safe_resolve(path: 'Path|str') -> 'Path|None':
	try:
		return await asyncio.to_thread(Path(path).resolve, strict=True)
	except Exception as exc:
		LOG.warning(f"Failed to resolve {path}: {log_exc(exc)}")
	return None


class WideHelpFormatter(argparse.HelpFormatter):
	def __init__(self, *args, **kwargs):
		kwargs['max_help_position'] = 32
		super().__init__(*args, **kwargs)


class SimpleTaskPool:
	def __init__(self):
		self.max_parallel = 4
		self.pool: 'set[asyncio.Task]' = set()
		self._event = asyncio.Event()

	def _done_callback(self, task):
		self.pool.discard(task)
		self._event.set()

	def is_full(self):
		return len(self.pool) >= self.max_parallel

	async def wait_not_full(self):
		while self.is_full():
			self._event.clear()
			await self._event.wait()
		return True

	async def wait_empty(self):
		while len(self.pool) > 0:
			self._event.clear()
			await self._event.wait()
		return True

	async def submit(self, coro: 'Coroutine', name=None):
		await self.wait_not_full()
		task = asyncio.create_task(coro, name=name)
		self.pool.add(task)
		task.add_done_callback(self._done_callback)
		return task


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


class RecodePriorityQueue:
	def __init__(self):
		super().__init__()
		self._heap: 'list[RecodeEntry]' = list()
		self._item_added = asyncio.Event()

	def push(self, entry: 'RecodeEntry'):
		heapq.heappush(self._heap, entry)
		self._item_added.set()

	def pop_nowait(self) -> 'RecodeEntry':
		# Вытаскиваем RecodeEntry с наименьшим штампом (_heap[0]),
		return heapq.heappop(self._heap)

	def can_pop(self):
		return len(self._heap) > 0 and self._heap[0].stamp - time.time() <= 0

	async def wait(self):
		# Ждать, пока в очереди не появлятся RecodeEntry, доступные для обработки.
		while True:
			if len(self._heap) < 1:
				# Если RecodeEntry нет, то ждем пока что-то появится.
				await self._item_added.wait()
				self._item_added.clear()
				continue
			if (wait_time := self._heap[0].stamp - time.time()) > 0:
				# Если что-то есть, то ждем пока его stamp <= time()
				await asyncio.sleep(wait_time)
				continue
			return

	async def pop(self) -> 'RecodeEntry':
		await self.wait()
		return self.pop_nowait()

	def __len__(self):
		return len(self._heap)


@functools.total_ordering
class RecodeEntry:
	__slots__ = (
		'main', 'stamp', 'id',
		'src_path', 'src_stat', 'src_last_size',
		'tmp_path',
		'dst_path', 'dst_stat', 'dst_created',
		'cwebp_args', 'cwebp_proc', 'cwebp_text',
		'webpinfo_text'
	)

	def __init__(self, main: 'Main', src_path: 'Path'):
		self.main = main
		self.stamp: 'float' = time.time()
		self.id = 0

		self.src_path = src_path
		self.src_stat: 'os.stat_result|None' = None
		self.src_last_size = -1

		self.tmp_path: 'Path|None' = None

		self.dst_path: 'Path|None' = None
		self.dst_stat: 'os.stat_result|None' = None
		# Флажок, что бы не удалить существующий dst, вместо созданного.
		self.dst_created = False

		self.cwebp_args: 'list[str]|None' = None
		self.cwebp_proc: 'asyncio.subprocess.Process|None' = None
		self.cwebp_text: 'str|None' = None

		self.webpinfo_text: 'str|None' = None

	def __eq__(self, other):
		return self.stamp == other.stamp if isinstance(other, RecodeEntry) else NotImplemented

	def __lt__(self, other):
		return self.stamp < other.stamp if isinstance(other, RecodeEntry) else NotImplemented

	def update_stamp(self, alter: 'float'):
		self.stamp = time.time() + alter

	@property
	def log_src_path(self):
		return log_path(self.src_path)

	@property
	def log_tmp_path(self):
		return log_path(self.tmp_path)

	@property
	def log_dst_path(self):
		return log_path(self.dst_path)

	async def _check_src_size(self):
		if is_debug():
			LOG.debug(f"Cheking file size of {self.log_src_path}...")

		if self.main.config.new_files_timeout > 0 and self.src_last_size != self.src_stat.st_size:
			if is_debug() and self.src_last_size > 0:
				LOG.debug(f"Size changed #{self.id} {self.log_src_path}: {self.src_last_size} -> {self.src_stat.st_size}.")
			self.update_stamp(self.main.config.new_files_timeout)
			self.src_last_size = self.src_stat.st_size
			self.main.recode_queue.push(self)
			return False

		min_size = max(self.main.config.ignore_files_less_than, 1)
		if self.src_stat.st_size < min_size:
			if is_debug():
				LOG.debug(f"Skip #{self.id} {self.log_src_path} have size {self.src_stat.st_size} < {min_size}.")
			return False

		max_size = self.main.config.ignore_files_greater_than
		if 0 < max_size < self.src_stat.st_size:
			if is_debug():
				LOG.debug(f"Skip #{self.id} {self.log_src_path} have size {self.src_stat.st_size} > {max_size}.")
			return False

		if is_debug():
			LOG.debug(f"File size of #{self.id} {self.log_src_path} is OK: {self.src_stat.st_size}")
		return True

	async def _try_move_src_to_tmp(self):
		try:
			if is_debug():
				LOG.debug(f"Moving src -> tmp #{self.id} {self.log_src_path} -> {self.log_tmp_path}...")
			# Здесь должен был быть отлет, если src_path занят, но иногда
			# винде почему-то похуй и она позволяет двигать открытые файлы.
			await asyncio.to_thread(os.rename, self.src_path, self.tmp_path)
			if is_debug():
				LOG.debug(f"Moved src -> tmp #{self.id} {self.log_src_path} -> {self.log_tmp_path}.")
			return True
		except OSError as exc:
			LOG.warning(f"Failed to prepare tmp #{self.id} {self.log_src_path} -> {self.log_tmp_path}: {exc!s}")
			# Ошибка перемещения может гововрить о том, что файл занят.
			# Попробуем обработать его еще раз позже.
			# Тут потенциально можно попать в вечный цил перезапусков, нужно подумать что можно сделать.
			dealy = max(self.main.config.new_files_timeout, 5)
			self.update_stamp(dealy)
			self.main.recode_queue.push(self)
			return False

	async def _prepare_dst_name(self):
		self.dst_path = self.src_path
		if self.main.config.vrc_swap_resolution_and_time:
			if match := self.main.vrc_screen_regex.match(self.src_path.name):
				self.dst_path = self.dst_path.with_name(f'VRChat_{match.group(2)}_{match.group(1)}{match.group(3)}')
		self.dst_path = self.dst_path.with_suffix('.webp')

		self.dst_stat = await safe_stat(self.dst_path, not_found_ok=True)
		if self.dst_stat:
			LOG.error(f"Destination file {self.log_dst_path} already exists ({sizeof_fmt(self.dst_stat.st_size)})!")
			return False
		return True

	async def _cwebp_spawn(self):
		self.cwebp_args = [
			self.main.path_cwebp, ('-v' if is_debug() else '-quiet'),
			'-preset', 'picture', '-hint', 'picture', '-q', '100', '-m', '6', '-metadata', 'all', '-low_memory']
		if self.src_path.suffix.lower() in ('.jpg', '.jpeg'):
			self.cwebp_args += ('-noalpha', '-f', '0', '-sharp_yuv')
		else:
			self.cwebp_args += ('-exact', '-alpha_q', '100', '-alpha_filter', 'best', '-lossless', '-z', '9')
		self.cwebp_args += ('-o', self.dst_path, '--', self.tmp_path)
		subprocess_priority = self.main.config.recoders_priority_cpu_subprocess()
		self.dst_created = True
		self.cwebp_proc = await create_subprocess_exec(
			*self.cwebp_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess_priority | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)

	@staticmethod
	def _set_priorities_offthread(pid: 'int', cpu: 'int', io: 'int'):
		proc = psutil.Process(pid=pid)
		proc.nice(value=cpu)
		proc.ionice(ioclass=io)

	async def _cwebp_update_priorities(self):
		try:
			ps_cpu = self.main.config.recoders_priority_cpu_psutil()
			ps_io = self.main.config.recoders_priority_io_psutil()
			await asyncio.to_thread(self._set_priorities_offthread, self.cwebp_proc.pid, ps_cpu, ps_io)
		except Exception as exc:
			LOG.warning(f"Failed to update priorities of #{self.id} {self.cwebp_args!r}: {log_exc(exc)}")

	async def _cwebp_reader(self):
		self.cwebp_text = ''
		chunk_i = 0
		if is_debug():
			LOG.debug(f"Recoder #{self.id} reading cwebp stream...")
		while not self.cwebp_proc.stdout.at_eof():
			chunk = await self.cwebp_proc.stdout.readline()
			if not chunk:
				break
			chunk_deoded = chunk.decode('utf-8')
			chunk_i += 1
			self.cwebp_text += chunk_deoded
			if is_debug():
				LOG.debug(f"Recoder #{self.id} got cwebp chunk {chunk_i}: {chunk_deoded!r}")
		if is_debug():
			LOG.debug(f"Recoder #{self.id} done reading cwebp, got {chunk_i} chunks.")

	async def _cwebp_terminator(self):
		await self.main.ask_stop_event.wait()
		if is_debug():
			LOG.debug(f"Terminating recoder #{self.id}...")
		self.cwebp_proc.terminate()

	async def _cwebp_communicate(self):
		task_terminator = None
		try:
			task_reader = asyncio.create_task(self._cwebp_reader(), name=f'reader-{self.id}')
			task_waiter = asyncio.create_task(self.cwebp_proc.wait(), name=f'waiter-{self.id}')
			task_terminator = asyncio.create_task(self._cwebp_terminator(), name=f'terminator-{self.id}')
			await asyncio.wait([task_reader, task_waiter])
			if self.cwebp_proc.returncode != 0:
				raise RuntimeError(f"returncode={self.cwebp_proc.returncode}")
			if is_debug():
				LOG.debug(f"Recoded #{self.id} {self.log_tmp_path} -> {self.log_dst_path}: {self.cwebp_text}")
		finally:
			if task_terminator and not task_terminator.done():
				task_terminator.cancel()
		return True

	async def _cwebp_safe(self):
		try:
			if is_debug():
				LOG.debug(f"Recoding #{self.id} {self.log_tmp_path} -> {self.log_dst_path}...")
			await self._cwebp_spawn()
			asyncio.create_task(self._cwebp_update_priorities(), name=f"priority-{self.id}-{self.cwebp_proc.pid}")
			return await self._cwebp_communicate()
		except Exception as exc:
			if self.main.ask_stop_event.is_set():
				LOG.info(f"Terminated recoder #{self.id}.")
			else:
				LOG.error(f"Failed to recode #{self.id} {self.cwebp_args!r}: {log_exc(exc)}", exc_info=exc)
				if self.cwebp_text:
					LOG.error(f"cwebp #{self.id} output: {self.cwebp_text}")
		return False

	async def _webpinfo_unsafe(self):
		subprocess_priority = self.main.config.recoders_priority_cpu_subprocess()
		proc = await create_subprocess_exec(
			self.main.path_webpinfo, '-quiet', self.dst_path,
			stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess_priority | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
		self.webpinfo_text = await process_communicate(proc)
		if proc.returncode != 0:
			raise RuntimeError(f"returncode={self.cwebp_proc.returncode}")
		if is_debug():
			LOG.debug(f"Checked #{self.id} {self.log_dst_path}: {self.webpinfo_text}")
		return True

	async def _webpinfo_safe(self):
		try:
			if is_debug():
				LOG.debug(f"Checking #{self.id} {self.log_dst_path}...")
			return await self._webpinfo_unsafe()
		except Exception as exc:
			LOG.error(f"Failed webpinfo #{self.id} {self.log_dst_path}: {log_exc(exc)}", exc_info=exc)
			if self.webpinfo_text:
				LOG.error(f"webpinfo #{self.id} output: {self.webpinfo_text}")
		return False

	async def _apply_mtime(self):
		try:
			if not self.main.config.update_mtime:
				return
			if is_debug():
				LOG.debug(f"Updating mtime of #{self.id} {self.log_dst_path}...")
			await asyncio.to_thread(os.utime, self.dst_path, ns=(self.src_stat.st_atime_ns, self.src_stat.st_mtime_ns))
			if is_debug():
				LOG.debug(f"Updated mtime of #{self.id} {self.log_dst_path}.")
			return True
		except OSError as exc:
			LOG.warning(f"Failed to update mtime of #{self.id} {self.log_dst_path}: {log_exc(exc)}", exc_info=exc)
		return False

	async def _rollback_src_tmp(self):
		try:
			if is_debug():
				LOG.debug(f"Rolling back #{self.id} {self.log_tmp_path} -> {self.log_src_path}...")
			await asyncio.to_thread(os.rename, self.tmp_path, self.src_path)
			if is_debug():
				LOG.debug(f"Rolled back #{self.id} {self.log_tmp_path} -> {self.log_src_path}.")
			return True
		except OSError as exc:
			LOG.error(f"Failed to rollback #{self.id} {self.log_tmp_path} -> {self.log_src_path}: {log_exc(exc)}",
								exc_info=exc)
		return False

	async def _delete_src(self, del_path: 'Path'):
		log_del_path = log_path(del_path)
		try:
			mode = self.main.config.delete_mode
			if mode == 'unlink':
				if is_debug():
					LOG.debug(f"Deleting #{self.id} {log_del_path} permanently...")
				await asyncio.to_thread(os.unlink, del_path)
				return True
			elif mode == 'trash':
				if is_debug():
					LOG.debug(f"Deleting #{self.id} {log_del_path} to trash...")
				await asyncio.to_thread(send2trash.send2trash, del_path)
				return True
		except Exception as exc:
			LOG.error(f"Failed to delete #{self.id} {log_del_path}: {log_exc(exc)}", exc_info=exc)
		return False

	async def _delete_dst(self):
		try:
			if self.dst_created:
				await asyncio.to_thread(os.unlink, self.dst_path)
		except FileNotFoundError:
			pass
		except Exception as exc:
			LOG.error(f"Failed to delete #{self.id} {self.log_dst_path}: {log_exc(exc)}", exc_info=exc)

	async def _do_recode_seq(self):
		if not await self._cwebp_safe():
			return False
		self.dst_stat = await safe_stat(self.dst_path, not_found_ok=False)
		if not self.dst_stat:
			LOG.error(f"Missing output #{self.id} {self.log_dst_path}.")
			return False
		if not await self._webpinfo_safe():
			return False
		return True

	def _check_dst_size(self):
		src_size, dst_size = self.src_stat.st_size, self.dst_stat.st_size
		if src_size < dst_size:
			if is_debug():
				LOG.debug(f"Size of {self.log_dst_path} ({dst_size}) > {self.log_tmp_path} ({src_size}), cancel.")
			return False
		return True

	async def _finalize_success(self):
		# Успешно сконверчено: подсчитываем, удаляем или откатываем времянку.
		self.main.counter_recoded_files += 1
		self.main.counter_recoded_bytes_src += self.src_stat.st_size
		self.main.counter_recoded_bytes_dst += self.dst_stat.st_size
		if is_debug():
			fmt_src = f"{self.log_src_path} ({self.src_stat.st_size}B)"
			fmt_dst = f"{self.log_dst_path} ({self.dst_stat.st_size}B)"
			diff_b = self.src_stat.st_size - self.dst_stat.st_size
			diff_p = diff_b / self.src_stat.st_size
			fmt_diff = f"{diff_b:+}B ({diff_p:+.1%})"
			LOG.debug(f"Recoded #{self.id}: {fmt_src} -> {fmt_dst} {fmt_diff}...")
		await self._apply_mtime()
		# Откатываем файл перед удалением, что бы в корзине он лежал с оригинальным именем без .tmp
		del_path = self.src_path if await self._rollback_src_tmp() else self.tmp_path
		await self._delete_src(del_path)

	async def do_recode(self):
		if self.main.ask_stop_event.is_set():
			return
		self.tmp_path = self.src_path.with_stem(self.src_path.stem + '.tmp')
		self.src_stat = await safe_stat(self.src_path)
		if not self.src_stat:
			# Файл не опрашивается / его нет, так что не перепланируем.
			return
		if not await self._check_src_size():
			return
		if not await self._prepare_dst_name():
			return
		if self.main.ask_stop_event.is_set():
			# Проверяем еще раз, т.к. за время проверок ивент мог измениться
			return
		self.main.counter_tried_files += 1
		if not await self._try_move_src_to_tmp():
			return
		if await self._do_recode_seq():
			if self._check_dst_size():
				await self._finalize_success()
			else:
				# Сконвертилось-то нормально, но размер результата больше, чем оригинала.
				await self._rollback_src_tmp()
				await self._delete_dst()
		else:
			# Что-то пошло не так: откатываем времянку, удаляем целевой.
			self.main.counter_failed_files += 1
			if await self._rollback_src_tmp():
				LOG.warning(f"Rolled back #{self.id}: {self.log_src_path}.")
			await self._delete_dst()


class PathsScaner:
	def __init__(self, main: 'Main'):
		self.main = main
		self.scanned: 'set[Path]' = set()
		self.scanned_resolved: 'set[Path]' = set()
		self.queue: 'collections.deque[Path]' = collections.deque()

	def submit(self, path: 'Path'):
		if self.main.config.randomize_scan and len(self.queue) < 1:
			if random.randint(0, 1):
				self.queue.append(path)
			else:
				self.queue.appendleft(path)
		else:
			self.queue.append(path)

	def _pop(self):
		if self.main.config.randomize_scan and len(self.queue) > 1:
			if random.randint(0, 1):
				return self.queue.popleft()
			else:
				return self.queue.pop()
		else:
			return self.queue.popleft()

	async def _single_unsafe(self, path: 'Path'):
		if path in self.scanned:
			if is_debug():
				LOG.debug(f"Path {log_path(path)} already scanned!")
			return
		self.scanned.add(path)

		# Защита от повторов и бесконечных циклов по ссылкам
		resolved_path = await asyncio.wait_for(asyncio.to_thread(path.resolve, strict=True), 5)
		if resolved_path in self.scanned_resolved:
			if is_debug():
				LOG.debug(f"Resolved path {log_path(resolved_path)} of path {log_path(path)} already scanned!")
			return
		self.scanned_resolved.add(resolved_path)

		path_stat: os.stat_result = await asyncio.wait_for(
			asyncio.to_thread(os.stat, path, follow_symlinks=True), 5)
		if not self.main.check_common(path, path_stat):
			return
		if stat.S_ISDIR(path_stat.st_mode):
			if not self.main.check_directory(path, path_stat):
				return
			listdir: list[str] = await asyncio.wait_for(asyncio.to_thread(os.listdir, path), 10)
			if self.main.config.randomize_scan:
				random.shuffle(listdir)
			else:
				listdir.sort()
			for entry in listdir:
				self.submit(path / entry)
			return
		if stat.S_ISREG(path_stat.st_mode):
			if not self.main.check_file(path, path_stat):
				return
			if is_debug():
				LOG.debug(f"File {log_path(path)} is acceptable.")
			entry = RecodeEntry(self.main, path)
			self.main.recode_queue.push(entry)
			return
		LOG.warning(f"Unknown file system object: {log_path(path)} (mode={path_stat.st_mode}), ignored.")

	async def _single_safe(self, path: 'Path'):
		try:
			await self._single_unsafe(path)
		except asyncio.TimeoutError:
			LOG.warning(f"Checking path {log_path(path)} is timed out, is file system lagging?")
			# Пере-планируем отлетевший по таймауту путь на потом.
			self.submit(path)
		except Exception as exc:
			LOG.warning(f"Failed to scan path: {log_path(path)}: {log_exc(exc)}")

	async def scan(self):
		counter = 0
		while len(self.queue) > 0:
			if self.main.ask_stop_event.is_set():
				LOG.info(f"Stopping FS scan, {len(self.queue)} items left.")
				return
			path = self._pop()
			counter += 1
			if is_debug():
				LOG.debug(f"Scanning {counter}: {log_path(path)}...")
			# Тут без параллелизма, т.к. нет смысла дрочить ФС и нет смысла получать список файлов быстро
			await self._single_safe(path)


class PathsObserverEventHandler(watchdog.events.FileSystemEventHandler):
	def __init__(self, _observer: 'PathsObserver', _root_path: 'Path'):
		self.root_path: 'Path' = _root_path
		self.observer = _observer
		self.main = _observer.main

	def on_created(self, event: 'watchdog.events.FileSystemEvent') -> None:
		# DirCreatedEvent или FileCreatedEvent
		self.main.loop.call_soon_threadsafe(self.handle_event_sync, event)

	def handle_event_sync(self, event: 'watchdog.events.FileSystemEvent'):
		# Это проще чем run_coroutine_threadsafe, т.к. лишние обертки для concurrent Future ннужны.
		asyncio.create_task(self.handle_event(event))

	async def handle_event(self, event: 'watchdog.events.FileSystemEvent'):
		if not isinstance(event, watchdog.events.FileCreatedEvent):
			return

		path = Path(event.src_path)
		if not path:
			LOG.warning(f"A new file detected, but was not able to resolve path {event.src_path!r}.")
			return

		path_stat = await safe_stat(path)
		if not path_stat:
			return

		if not self.main.check_common(path, path_stat):
			return False
		if not self.main.check_file(path, path_stat):
			return False

		rel_path: 'Path|None' = None
		try:
			rel_path = path.relative_to(self.root_path)
		except ValueError:
			LOG.warning(f'New detected {log_path(path)} is not relative to {log_path(self.root_path)}')
			return

		# parents относительного пути всегда кончается точкой '.'
		for rel_part in rel_path.parents[:-1]:
			abs_part = self.root_path / rel_part
			abs_part_stat = await safe_stat(abs_part)
			if not self.main.check_common(abs_part, abs_part_stat):
				return False
			if not self.main.check_directory(abs_part, abs_part_stat):
				return False

		# Если до сих пор никаких фильтров не отлетело, то можно смело планировать.
		if is_debug():
			LOG.debug(f"File {log_path(path)} is acceptable.")
		entry = RecodeEntry(self.main, path)
		self.main.recode_queue.push(entry)


class PathsObserver(watchdog.observers.Observer):
	def __init__(self, main: 'Main'):
		super().__init__()
		self.root_paths: 'set[Path]' = set()
		self.main = main
		self.handlers = dict()

	async def observe(self):
		LOG.info(f"Preparing file system observer...")
		self.root_paths.clear()
		for path in self.main.config.watch_paths:
			if path := await path_safe_resolve(path):
				self.root_paths.add(path)
		for path in self.root_paths:
			LOG.info(f"Scheduling watch path: {log_path(path)}...")
			handler = PathsObserverEventHandler(self, path)
			self.handlers[path] = handler
			self.schedule(handler, str(path), recursive=True)
		try:
			self.start()
			LOG.info(f"Started file system observer in {len(self.root_paths)} paths.")
			await self.main.ask_stop_event.wait()
		finally:  # Может быть asyncio.CancelledError
			if self.is_alive():
				if is_debug():
					LOG.debug(f"Stopping observer...")
				self.stop()
				LOG.info(f"Observer stopped.")


class Main:
	def __init__(self):
		self.argparser: 'argparse.ArgumentParser|None' = None
		self.args: 'argparse.Namespace|None' = None
		self.loop: 'asyncio.AbstractEventLoop|None' = None

		# ask_stop выставляется, когда необходимо закончить все работы
		self.ask_stop = 0
		self.ask_stop_event = asyncio.Event()
		# ask_done выставляется, когда новых RecodeEntry больше не будет
		self.ask_done_event = asyncio.Event()

		self.config_path: 'Path|None' = None
		self.config: 'Config|None' = None

		self.observer: 'PathsObserver|None' = None
		self.scanner: 'PathsScaner|None' = None

		self.recode_pool = SimpleTaskPool()
		self.recode_queue: 'RecodePriorityQueue' = RecodePriorityQueue()

		self.counter_tried_files = 0
		self.counter_failed_files = 0
		self.counter_recoded_files = 0
		self.reported_recoded_files = 0
		self.counter_recoded_bytes_src = 0
		self.counter_recoded_bytes_dst = 0

		self.vrc_screen_regex = re.compile(r'^VRChat_(\d+x\d+)_([-\d]+_[-\d]+\.\d+)(.*)$')

		self.path_self = Path(__file__).parent.resolve(strict=True)
		self.path_assets = self.path_self / 'assets'
		self.path_default_config = self.path_assets / 'default.yaml'
		self.path_cwebp = self.path_assets / 'cwebp.exe'
		self.path_webpinfo = self.path_assets / 'webpinfo.exe'
		self.path_logs = self.path_self / 'logs'

	def setup_debug(self):
		global APP_DEBUG
		if self.args.debug:
			APP_DEBUG = True

	def setup_log(self):
		global LOG
		level = logging.DEBUG if is_debug() else logging.INFO
		logging.basicConfig(level=level)

		std_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
		std_handler = logging.StreamHandler(stream=sys.stdout)
		std_handler.setLevel(level)
		std_handler.setFormatter(std_formatter)

		for old_handler in list(logging.root.handlers):
			logging.root.removeHandler(old_handler)

		LOG = logging.getLogger('vrc2webp')
		LOG.setLevel(level=level)

		log_asyncio = logging.getLogger('asyncio')
		log_asyncio.setLevel(level=logging.DEBUG if is_debug() else logging.WARNING)

		loggers = (LOG, log_asyncio)

		for logger in loggers:
			for old_handler in list(logger.handlers):
				logger.removeHandler(old_handler)
			logger.addHandler(std_handler)

		try:
			from logging.handlers import RotatingFileHandler
			self.path_logs.mkdir(parents=True, exist_ok=True)
			file_handler = RotatingFileHandler(
				str(self.path_logs / 'vrc2webp.log'), maxBytes=10 * 1024 * 1024, backupCount=10, encoding='utf-8')
			file_handler.setLevel(level)
			file_handler.setFormatter(std_formatter)
			for logger in loggers:
				logger.addHandler(file_handler)
		except Exception as exc:
			LOG.error(f"Failed to create log file: {log_exc(exc)}", exc_info=exc)

		LOG.info(f"Logging initialized. APP_DEBUG={APP_DEBUG}")

	def handle_signal_reentrant(self, signum: 'int', signame: 'str'):
		self.ask_stop += 1
		self.ask_stop_event.set()
		if self.ask_stop >= 5:
			LOG.info(f"Asked fo stop ({signame}/{signum}) {self.ask_stop} times, forcing app crash...")
			sys.exit(1)
		else:
			more = 5 - self.ask_stop
			LOG.info(f"Asked fo stop ({signame}/{signum}) {self.ask_stop} times, ask {more} more times to force app crash...")

	def setup_asyncio(self):
		self.loop = asyncio.get_running_loop()
		self.loop.set_debug(is_debug())
		for signame in ('SIGABRT', 'SIGINT', 'SIGTERM', 'SIGBREAK'):
			def handler(signum: 'int', frame):
				# Не всякий код можно выполнять в обработчике сигнала,
				# часто пукает с ошибкой RuntimeError: reentrant call
				# по этому в обработчике только планируем
				self.loop.call_soon_threadsafe(self.handle_signal_reentrant, signum, signame)

			signal.signal(getattr(signal, signame), handler)

	def read_config(self):
		self.config = Config()
		if is_debug():
			LOG.debug(f"Loading default config {log_path(self.path_default_config)}...")
		self.config.load_yaml_file(self.path_default_config)
		if self.args.config:
			if is_debug():
				LOG.debug(f"Loading custom config {log_path(self.args.config)}...")
			self.config_path = self.args.config.resolve(strict=True)
			self.config.load_yaml_file(self.config_path)

	def setup_priority(self):
		try:
			if is_debug():
				LOG.debug("Changing own priority...")
			p = psutil.Process()
			p.nice(self.config.own_priority_cpu_psutil())
			p.ionice(self.config.own_priority_io_psutil())
			if is_debug():
				LOG.debug("Changed own priorities.")
		except Exception as exc:
			LOG.error(f"Failed to change own priority: {log_exc(exc)}", exc_info=exc)

	async def test_generic_exe(self, exe_name, program_args):
		if is_debug():
			LOG.debug(f"Testing {exe_name}...")
		try:
			cwebp_process = await create_subprocess_exec(
				*program_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
				creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
			stdout_text = await process_communicate(cwebp_process)
			if cwebp_process.returncode != 0:
				raise Exception(f"{exe_name} test failed ({cwebp_process.returncode}): {stdout_text}")

			LOG.info(f"{exe_name} test OK: {stdout_text}")
		except Exception as exc:
			LOG.error(f"Failed to test {exe_name} ({program_args!r}): {log_exc(exc)}", exc_info=exc)
			raise exc

	def test_cwebp_exe(self):
		return self.test_generic_exe('cwebp.exe', [str(self.path_cwebp), '-version'])

	def test_webpinfo_exe(self):
		return self.test_generic_exe('webpinfo.exe', [str(self.path_webpinfo), '-version'])

	def log_msg_recode_queue(self):
		return f"{len(self.recode_queue)} files pending recoding."

	def log_msg_recode_pool(self):
		return f"{len(self.recode_pool.pool)} files recoding right now."

	async def job_observe(self):
		try:
			if self.config.watch_mode not in ('observe', 'both'):
				return
			self.observer = PathsObserver(self)
			await self.observer.observe()
		except Exception as exc:
			LOG.critical(f"Observe loop crashed: {log_exc(exc)}", exc_info=exc)
			self.ask_stop_event.set()

	async def job_scan(self):
		try:
			if self.config.watch_mode not in ('scan', 'both'):
				return
			LOG.info(f"Scanning items in {len(self.config.watch_paths)} watch paths.")
			self.scanner = PathsScaner(self)
			for path in self.config.watch_paths:
				self.scanner.submit(path)
			await self.scanner.scan()
			LOG.info(' '.join([
				f"Done scanning {len(self.scanner.scanned)} items in {len(self.config.watch_paths)} watch paths.",
				self.log_msg_recode_queue(), self.log_msg_recode_pool()]))
		except Exception as exc:
			LOG.critical(f"Scan loop crashed: {log_exc(exc)}", exc_info=exc)
			self.ask_stop_event.set()

	def check_common(self, path: 'Path', path_stat: 'os.stat_result'):
		path_name = path.name
		if any(path_name.startswith(prefix) for prefix in self.config.ignore_objects_name_stats_with):
			if is_debug():
				LOG.debug(f"Object {log_path(path)} have ignorant prefix")
			return False
		if any(path_name.endswith(suffix) for suffix in self.config.ignore_objects_name_ends_with):
			if is_debug():
				LOG.debug(f"Object {log_path(path)} have ignorant suffix")
			return False
		path_str = str(path)
		if any(sub in path_str for sub in self.config.ignore_objects_path_contains):
			if is_debug():
				LOG.debug(f"Object {log_path(path)} have ignorant substring in path.")
			return False
		win_attrs = path_stat.st_file_attributes
		if self.config.ignore_objects_hidden and (win_attrs & stat.FILE_ATTRIBUTE_HIDDEN):
			if is_debug():
				LOG.debug(f"Skip object {log_path(path)} is hidden.")
			return False
		if self.config.ignore_objects_readonly and (win_attrs & stat.FILE_ATTRIBUTE_READONLY):
			if is_debug():
				LOG.debug(f"Skip object {log_path(path)} is readonly.")
			return False
		if self.config.ignore_objects_system and (win_attrs & stat.FILE_ATTRIBUTE_SYSTEM):
			if is_debug():
				LOG.debug(f"Skip object {log_path(path)} is system.")
			return False
		return True

	def check_file(self, path: 'Path', path_stat: 'os.stat_result'):
		# Проверка на размер файла тут не производится, т.к. он может меняться,
		# если файл обнаружен через ивент обсервера. Проверка размера производится прямо перед перекодирванием.
		suffixes = path.suffixes
		if len(suffixes) < 1:
			if is_debug():
				LOG.debug(f"Skip file {log_path(path)} have no extensions.")
			return False
		if suffixes[-1].lower() not in self.config.file_extensions:
			if is_debug():
				LOG.debug(f"Skip file {log_path(path)} have no matching extension.")
			return False
		if len(suffixes) > 1 and suffixes[-2].lower() == '.tmp':
			if is_debug():
				LOG.debug(f"Skip file {log_path(path)} is temporary.")
			return False
		return True

	def check_directory(self, path: 'Path', path_stat: 'os.stat_result' = None):
		return True  # TODO

	def recode_pool_update_max_parallel(self):
		if self.config.max_parallel_recodes < 1:
			if is_debug():
				LOG.debug('cpu_count...')
			self.config.max_parallel_recodes = max(psutil.cpu_count(), 1)
			if is_debug():
				LOG.debug(f'max_parallel_recodes = {self.config.max_parallel_recodes}')
		self.recode_pool.max_parallel = self.config.max_parallel_recodes

	async def pop_recode_queue(self) -> 'RecodeEntry|None':
		# Если в очереди есть элемент, уже готовый к обработке, то возвращаем его сразу.
		if self.recode_queue.can_pop():
			return self.recode_queue.pop_nowait()
		# Если такого элемента нету, то ждем пока он придет или события на завершения.
		wait_for = [self.recode_queue.wait(), self.ask_stop_event.wait()]
		if len(self.recode_queue) < 1:
			# Чекаем завершение, только если элементов уже совсем нет.
			# Если какие-то RecodeEntry еще есть, то их надо отработать, даже если стрельнул ask_done_event
			wait_for.append(self.ask_done_event.wait())
		await wait_any(*wait_for)
		# Если ивенты стрельнули раньше, чем появился элемент в очереди, то вернется None
		return self.recode_queue.pop_nowait() if self.recode_queue.can_pop() else None

	async def recoding_loop(self):
		counter = 0
		while True:
			await asyncio.sleep(0)

			if self.ask_stop_event.is_set():
				if is_debug():
					LOG.debug(f"Terminating recoding loop due to ask_stop...")
				return

			# Если очередь пуста, то проверяем не пришла ли просьба на завершение.
			if self.ask_done_event.is_set() and len(self.recode_queue) < 1:
				if is_debug():
					LOG.debug(f"Terminating recoding loop due to ask_done...")
				return

			if not (entry := await self.pop_recode_queue()):
				continue

			counter += 1
			if is_debug():
				LOG.debug(f"Pooling (i={counter}, q={len(self.recode_queue)}) recode of {log_path(entry.src_path)}...")
			self.recode_pool_update_max_parallel()
			entry.id = counter
			await self.recode_pool.submit(entry.do_recode(), name=f'recode-{counter}')

	async def job_recoding(self):
		try:
			if is_debug():
				LOG.debug(f"Starting recoding loop, awaiting for files to recode...")
			await self.recoding_loop()
			while len(self.recode_pool.pool) > 0:
				LOG.info(f"Waiting {len(self.recode_pool.pool)} recoding tasks to complete before exit...")
				await asyncio.wait(self.recode_pool.pool, return_when=asyncio.FIRST_COMPLETED)
			LOG.info(f"All recoding processes completed.")
		except Exception as exc:
			LOG.critical(f"Recoding loop crashed: {log_exc(exc)}", exc_info=exc)
			self.ask_stop_event.set()

	async def job_reporting(self):
		reported_recoded_files = -1
		reported_tried_files = -1
		cancelled = False
		while not cancelled:
			try:
				await asyncio.sleep(10)
			except asyncio.CancelledError:
				cancelled = True
			if cancelled or reported_recoded_files != self.counter_recoded_files or reported_tried_files != self.counter_tried_files:
				reported_recoded_files = self.counter_recoded_files
				reported_tried_files = self.counter_tried_files
				src_fmt = sizeof_fmt(self.counter_recoded_bytes_src)
				dst_fmt = sizeof_fmt(self.counter_recoded_bytes_dst)
				diff = (self.counter_recoded_bytes_src - self.counter_recoded_bytes_dst)
				diff_fmt = sizeof_fmt(diff)
				percent = diff / self.counter_recoded_bytes_src if self.counter_recoded_bytes_src > 0 else 0.0
				LOG.info(' '.join([
					f"Reduced size from {src_fmt} to {dst_fmt} by {diff_fmt} ({percent:.1%}).",
					f"Recoded {self.counter_recoded_files}, tried {self.counter_tried_files} files.",
					self.log_msg_recode_queue(), self.log_msg_recode_pool()]))

	async def main_recode_async(self):
		self.setup_asyncio()
		await self.test_cwebp_exe()
		await self.test_webpinfo_exe()

		task_recoding = asyncio.create_task(self.job_recoding(), name='job_recoding')
		task_observe = asyncio.create_task(self.job_observe(), name='job_observe')
		task_scan = asyncio.create_task(self.job_scan(), name='job_observe')
		task_reporting = asyncio.create_task(self.job_reporting(), name='job_reporting')
		LOG.info(f"All recoding/observe/scan tasks started, awaiting for files...")

		await asyncio.gather(task_observe, task_scan, return_exceptions=True)

		LOG.info(f"All observe/scan tasks completed, awaiting recoding task completion...")
		self.ask_done_event.set()

		await asyncio.gather(task_recoding, return_exceptions=True)

		task_reporting.cancel()
		await asyncio.gather(task_reporting, return_exceptions=True)

		LOG.info(f"All tasks completed. App exit.")

	def main_recode(self):
		self.setup_debug()
		self.setup_log()
		self.read_config()
		self.setup_priority()
		asyncio.run(self.main_recode_async(), debug=is_debug())

	def main_export(self) -> 'int':
		try:
			self.setup_debug()
			self.setup_log()
			if not self.args.config:
				raise ValueError('Config path not provided!')
			LOG.info(f"Exporting default config to {log_path(self.args.config)} ...")
			arg_config: Path = self.args.config.resolve()
			if is_debug():
				LOG.debug(f"Copying {log_path(self.path_default_config)} -> {log_path(arg_config)}...")
			arg_config.parent.mkdir(parents=True, exist_ok=True)
			shutil.copyfile(self.path_default_config, arg_config)
			LOG.info(f"Exported default config to {log_path(arg_config)} ...")
			return 0
		except BaseException as exc:
			LOG.error(f"Failed to export default config: {log_exc(exc)}", exc_info=exc)
		return 1

	async def main_test_async(self) -> 'int':
		LOG.info("Testing embedded exe binaries...")
		self.setup_asyncio()
		test_ok = True
		tests = [
			('cwebp', self.test_cwebp_exe()),
			('webpinfo', self.test_webpinfo_exe())]
		for binary, coro in tests:
			result = await asyncio.gather(coro, return_exceptions=True)
			if isinstance(result, BaseException):
				test_ok = False
				LOG.error(f"Test embedded {binary!r} failed: {result}", exc_info=result)
		LOG.info(f"Tested {len(tests)} embedded exe binaries: {test_ok}")
		return 0 if test_ok else 1

	def main_test(self) -> 'int':
		self.setup_debug()
		self.setup_log()
		return asyncio.run(self.main_test_async(), debug=is_debug())

	def main(self):
		self.argparser = argparse.ArgumentParser(prog='vrc2webp', formatter_class=WideHelpFormatter)

		self.argparser.add_argument('-v', '--version', action='version', version=f'vrc2webp {APP_VERION}')

		group = self.argparser.add_mutually_exclusive_group(required=False)
		group.add_argument(
			'-r', '--recode', action='store_true',
			help=' '.join([
				'Start monitoring and recoding process.',
				'Default configuration (for current user) for VRChat is used if no custom --config provided.']))
		group.add_argument(
			'-e', '--export', action='store_true',
			help=' '.join([
				'Export default YAML config file. So you can customize it and use with --recode.',
				'Where to export path must be provided with --config.']))
		group.add_argument(
			'-t', '--test', action='store_true',
			help='Test embedded binaries.')

		self.argparser.add_argument(
			'-c', '--config', action='store', metavar='path', type=Path,
			help='Path to YAML config file. You can generate one with --export.')

		self.argparser.add_argument(
			'-d', '--debug', action='store_true',
			help='Enable debug mode.')

		self.args = self.argparser.parse_args()

		if self.args.test:
			sys.exit(self.main_test())
		elif self.args.export:
			sys.exit(self.main_export())
		elif self.args.recode:
			sys.exit(self.main_recode())
		else:
			self.argparser.print_help()
			sys.exit(0)


if __name__ == '__main__':
	Main().main()
