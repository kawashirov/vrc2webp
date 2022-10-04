import os
import sys
import shutil
import signal
import asyncio
import subprocess
import re
import logging
import typing

from argparse import ArgumentParser, HelpFormatter, Namespace
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from asyncio.subprocess import create_subprocess_exec

import psutil
import yaml

from send2trash import send2trash
from watchdog.observers import Observer
from watchdog.events import FileSystemEvent, FileCreatedEvent, FileSystemEventHandler

if typing.TYPE_CHECKING:
	from asyncio import Future, Queue
	from typing import List, Set, Container, Coroutine, Self

APP_VERION = '0.1.0'
APP_DEBUG = bool(hasattr(sys, 'gettrace') and sys.gettrace())
LOG = logging.getLogger('vrc2webp')


def is_debug():
	return APP_DEBUG


def log_path(path: 'Path|None'):
	return repr(path.as_posix()) if isinstance(path, Path) else repr(path)


def check_file(pathlike: 'os.PathLike|str'):
	try:
		os.stat(pathlike)
	except FileNotFoundError:
		return False
	except OSError as exc:
		LOG.warning(f"Failed to check file {str(pathlike)!r}: {type(exc)!r}: {exc}")
	return True


def sizeof_fmt(num, suffix="B"):
	for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
		if abs(num) < 1024.0:
			return f"{num:3.2f}{unit}{suffix}"
		num /= 1024.0
	return f"{num:.2f}Yi{suffix}"


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


async def path_safe_resolve(path: 'Path|str'):
	try:
		path = Path(path)
		return await asyncio.to_thread(path.resolve, strict=True)
	except Exception as exc:
		LOG.warning(f"Failed to resolve {path}: {type(exc)!r}: {exc}")
	return None


class WideHelpFormatter(HelpFormatter):
	def __init__(self, *args, **kwargs):
		kwargs['max_help_position'] = 32
		super().__init__(*args, **kwargs)


class SimpleTaskPool:
	def __init__(self):
		self.max_parallel = 4
		self.pool = set()  # type: Set[Future]
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

	async def submit(self, coro: 'Coroutine', name=None):
		await self.wait_not_full()
		task = asyncio.create_task(coro, name=name)
		self.pool.add(task)
		task.add_done_callback(self._done_callback)
		return task


class _EventHandler(FileSystemEventHandler):
	def __init__(self, _main: 'Main'):
		self.main = _main

	def on_created(self, event: 'FileSystemEvent') -> None:
		# DirCreatedEvent или FileCreatedEvent
		asyncio.run_coroutine_threadsafe(self.main.handle_event(event), self.main.loop)


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

	own_priority_cpu: 'str'
	recoders_priority_cpu: 'str'
	own_priority_io: 'str'
	recoders_priority_io: 'str'
	watch_paths: 'List[Path]'
	recursive: 'bool'
	file_extensions: 'List[str]'
	vrc_swap_resolution_and_time: 'bool'
	max_parallel_recodes: 'int'
	update_mtime: 'bool'
	delete_mode: 'str'

	@staticmethod
	def _type_check(name, obj, types):
		if not isinstance(obj, types):
			raise ConfigError(f"Invalid type of {name!r}: {type(obj)!r}: {obj!r}")

	@staticmethod
	def _get_bool(raw_config: 'dict', key: 'str'):
		value = raw_config.get(key)  # type: bool
		if isinstance(value, (bool, int)):
			return bool(value)
		elif isinstance(value, str):
			value_trim = value.lower().strip()
			if value_trim in ('1', 'true', 'yes', 'y'):
				return True
			elif value_trim in ('0', 'false', 'no', 'n'):
				return False
		raise ConfigError(f"Invalid bool value of {key}: {type(value)!r}: {value!r}")

	@staticmethod
	def _get_int(raw_config: 'dict', key: 'str'):
		value = raw_config[key]  # type: bool
		if isinstance(value, int):
			return value
		elif isinstance(value, str):
			return int(value.strip())
		raise ConfigError(f"Invalid bool int of {key}: {type(value)!r}: {value!r}")

	@staticmethod
	def _get_keyword(raw_config: 'dict', key: 'str', keywords: 'Container[str]'):
		if (value := raw_config[key]) is not None:
			value = str(value).lower()
			if value in keywords:
				return value
		raise ConfigError(f"Invalid keyword of {key}: {type(value)!r}: {value!r}")

	def _apply_watch_paths(self, raw_config: 'dict'):
		self.watch_paths = raw_config.get('watch-paths')
		self._type_check('watch-paths', self.watch_paths, list)
		for i in range(len(self.watch_paths)):
			self.watch_paths[i] = Path(os.path.expandvars(str(self.watch_paths[i])))
		if len(self.watch_paths) < 1:
			raise ConfigError(f"No items in 'watch-paths'.")

	def _apply_file_extensions(self, raw_config: 'dict'):
		self.file_extensions = raw_config.get('file-extensions')  # type: List[str]
		self._type_check('file-extensions', self.file_extensions, list)
		for i in range(len(self.file_extensions)):
			filext = str(self.file_extensions[i]).lower().strip()
			if not filext.startswith('.'):
				raise ConfigError(f"Item 'file-extensions[{i}]' doesnt start with dot (.): {filext}")
			self.file_extensions[i] = filext

	def load_yaml_file(self, path: 'Path') -> 'Self':
		try:
			raw_config = None  # type: dict|None
			with open(path, 'rt') as stream:
				raw_config = yaml.safe_load(stream)
				self._type_check('root', raw_config, dict)
			self.own_priority_cpu = self._get_keyword(raw_config, 'own-priority-cpu', self.CPU_PRIORITIES)
			self.recoders_priority_cpu = self._get_keyword(raw_config, 'recoders-priority-cpu', self.CPU_PRIORITIES)
			self.own_priority_io = self._get_keyword(raw_config, 'own-priority-io', self.IO_PRIORITIES)
			self.recoders_priority_io = self._get_keyword(raw_config, 'recoders-priority-io', self.IO_PRIORITIES)
			self._apply_watch_paths(raw_config)
			# self.recursive = self._get_bool(raw_config, 'recursive')
			self._apply_file_extensions(raw_config)
			self.vrc_swap_resolution_and_time = self._get_bool(raw_config, 'vrc-swap-resolution-and-time')
			self.max_parallel_recodes = self._get_int(raw_config, 'max-parallel-recodes')
			self.update_mtime = self._get_bool(raw_config, 'update-mtime')
			self.delete_mode = self._get_keyword(raw_config, 'delete-mode', ('keep', 'trash', 'trash'))
			return self
		except (OSError, RuntimeError) as exc:
			msg = f"Failed to load config ({str(path)!r}): {type(exc)!r}: {exc}"
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


class RecodeEntry:
	def __init__(self, main: 'Main', src_path: 'Path'):
		self.main = main
		self.id = 0
		self.src_path = src_path
		self.tmp_path = None  # type: Path|None
		self.dst_path = None  # type: Path|None
		self.src_stat = None  # type: os.stat_result|None
		self.dst_stat = None  # type: os.stat_result|None

		self.cwebp_args = None
		self.cwebp_proc = None  # type: asyncio.subprocess.Process|None
		self.cwebp_text = None

		self.webpinfo_text = None

	@property
	def log_src_path(self):
		return log_path(self.src_path)

	@property
	def log_tmp_path(self):
		return log_path(self.tmp_path)

	@property
	def log_dst_path(self):
		return log_path(self.dst_path)

	async def do_recode_prepare(self):
		try:
			if self.src_stat.st_size < 1:
				LOG.info(f"File #{self.id} {self.log_src_path} is empty. (Yet?)")
				return False
			if is_debug():
				LOG.debug(f"Moving #{self.id} {self.log_src_path} -> {self.log_tmp_path}...")
			await asyncio.to_thread(os.rename, self.src_path, self.tmp_path)
			if is_debug():
				LOG.debug(f"Moved #{self.id} {self.log_src_path} -> {self.log_tmp_path}.")
			return True
		except OSError as exc:
			LOG.info(f"Failed to prepare #{self.id} {self.log_src_path} -> {self.log_tmp_path}: {exc!s}")
			return False

	def get_dst_name(self):
		self.dst_path = self.src_path
		if self.main.config.vrc_swap_resolution_and_time:
			if match := self.main.vrc_screen_regex.match(self.src_path.name):
				self.dst_path = self.dst_path.with_name(f'VRChat_{match.group(2)}_{match.group(1)}{match.group(3)}')
		self.dst_path = self.dst_path.with_suffix('.webp')
		return self.dst_path

	async def cwebp_spawn(self):
		self.cwebp_args = [
			self.main.path_cwebp, ('-v' if is_debug() else '-quiet'),
			'-preset', 'picture', '-hint', 'picture', '-q', '100', '-m', '6', '-metadata', 'all', '-low_memory']
		if self.src_path.suffix.lower() in ('.jpg', '.jpeg'):
			self.cwebp_args += ('-noalpha', '-f', '0', '-sharp_yuv')
		else:
			self.cwebp_args += ('-exact', '-alpha_q', '100', '-alpha_filter', 'best', '-lossless', '-z', '9')
		self.cwebp_args += ('-o', self.dst_path, '--', self.tmp_path)
		subprocess_priority = self.main.config.recoders_priority_cpu_subprocess()
		self.cwebp_proc = await create_subprocess_exec(
			*self.cwebp_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess_priority | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)

	@staticmethod
	def set_priorities_offthread(pid: 'int', cpu: 'int', io: 'int'):
		proc = psutil.Process(pid=pid)
		proc.nice(value=cpu)
		proc.ionice(ioclass=io)

	async def cwebp_update_priorities(self):
		try:
			ps_cpu = self.main.config.recoders_priority_cpu_psutil()
			ps_io = self.main.config.recoders_priority_io_psutil()
			await asyncio.to_thread(self.set_priorities_offthread, self.cwebp_proc.pid, ps_cpu, ps_io)
		except Exception as exc:
			LOG.warning(f"Failed to update priorities of #{self.id} {self.cwebp_args!r}: {type(exc)!r}: {exc}")

	async def cwebp_reader(self):
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

	async def cwebp_terminator(self):
		await self.main.ask_stop_event.wait()
		if is_debug():
			LOG.debug(f"Terminating recoder #{self.id}...")
		self.cwebp_proc.terminate()

	async def cwebp_communicate(self):
		task_terminator = None
		try:
			task_reader = asyncio.create_task(self.cwebp_reader(), name=f'reader-{self.id}')
			task_waiter = asyncio.create_task(self.cwebp_proc.wait(), name=f'waiter-{self.id}')
			task_terminator = asyncio.create_task(self.cwebp_terminator(), name=f'terminator-{self.id}')
			await asyncio.wait([task_reader, task_waiter])
			if self.cwebp_proc.returncode != 0:
				raise RuntimeError(f"returncode={self.cwebp_proc.returncode}")
			if is_debug():
				LOG.debug(f"Recoded #{self.id} {self.log_tmp_path} -> {self.log_dst_path}: {self.cwebp_text}")
		finally:
			if task_terminator and not task_terminator.done():
				task_terminator.cancel()
		return True

	async def cwebp_safe(self):
		try:
			if is_debug():
				LOG.debug(f"Recoding #{self.id} {self.log_tmp_path} -> {self.log_dst_path}...")
			await self.cwebp_spawn()
			asyncio.create_task(self.cwebp_update_priorities(), name=f"priority-{self.id}-{self.cwebp_proc.pid}")
			return await self.cwebp_communicate()
		except Exception as exc:
			if self.main.ask_stop_event.is_set():
				LOG.info(f"Terminated recoder #{self.id}.")
			else:
				LOG.error(f"Failed to recode #{self.id} {self.cwebp_args!r}: {type(exc)!r}: {exc}", exc_info=exc)
				if self.cwebp_text:
					LOG.error(f"cwebp #{self.id} output: {self.cwebp_text}")
		return False

	async def webpinfo_unsafe(self):
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

	async def webpinfo_safe(self):
		try:
			if is_debug():
				LOG.debug(f"Checking #{self.id} {self.log_dst_path}...")
			return await self.webpinfo_unsafe()
		except Exception as exc:
			LOG.error(f"Failed webpinfo #{self.id} {self.log_dst_path}: {type(exc)!r}: {exc}", exc_info=exc)
			if self.webpinfo_text:
				LOG.error(f"webpinfo #{self.id} output: {self.webpinfo_text}")
		return False

	async def apply_mtime(self):
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
			LOG.warning(f"Failed to update mtime of #{self.id} {self.log_dst_path}: {type(exc)!r}: {exc}", exc_info=exc)
		return False

	async def rollback_src_tmp(self):
		try:
			if is_debug():
				LOG.debug(f"Rolling back #{self.id} {self.log_tmp_path} -> {self.log_src_path}...")
			await asyncio.to_thread(os.rename, self.tmp_path, self.src_path)
			if is_debug():
				LOG.debug(f"Rolled back #{self.id} {self.log_tmp_path} -> {self.log_src_path}.")
			return True
		except OSError as exc:
			LOG.error(f"Failed to rollback #{self.id} {self.log_tmp_path} -> {self.log_src_path}: {type(exc)!r}: {exc}",
								exc_info=exc)
		return False

	async def delete_or_rollback_src_tmp(self):
		try:
			mode = self.main.config.delete_mode
			if mode == 'unlink':
				if is_debug():
					LOG.debug(f"Deleting #{self.id} {self.log_tmp_path} permanently...")
				await asyncio.to_thread(os.unlink, self.tmp_path)
				return True
			elif mode == 'trash':
				if is_debug():
					LOG.debug(f"Deleting #{self.id} {self.log_tmp_path} to trash...")
				await asyncio.to_thread(send2trash, self.tmp_path)
				return True
			elif mode == 'keep':
				return await self.rollback_src_tmp()
		except Exception as exc:
			LOG.error(f"Failed to finalize (delete) #{self.id} {self.log_tmp_path}: {type(exc)!r}: {exc}", exc_info=exc)
		return False

	async def delete_dst(self):
		try:
			await asyncio.to_thread(os.unlink, self.dst_path)
		except FileNotFoundError:
			pass
		except Exception as exc:
			LOG.error(f"Failed to delete {self.log_dst_path}: {type(exc)!r}: {exc}", exc_info=exc)

	async def do_recode_seq(self):
		self.dst_stat = await safe_stat(self.dst_path, not_found_ok=True)
		if self.dst_stat:
			LOG.error(f"Destination file {self.log_dst_path} already exists ({sizeof_fmt(self.dst_stat.st_size)})!")
			return False
		if not await self.cwebp_safe():
			return False
		self.dst_stat = await safe_stat(self.dst_path, not_found_ok=False)
		if not self.dst_stat:
			return False
		if not await self.webpinfo_safe():
			return False
		return True

	async def do_recode(self):
		if self.main.ask_stop_event.is_set():
			if is_debug():
				LOG.debug(f"Not starting recode #{self.id}.")
			return
		self.tmp_path = self.src_path.with_stem(self.src_path.stem + '.tmp')
		if is_debug():
			LOG.debug(f"Checking to recode #{self.id} {self.log_src_path}...")
		self.src_stat = await safe_stat(self.src_path)
		if not self.src_stat:
			return
		if not await self.do_recode_prepare():
			LOG.info(f"Re-queueing #{self.id} {self.log_src_path}...")
			self.main.recode_queue.put_nowait(self)
			return
		self.get_dst_name()
		if await self.do_recode_seq():
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
			await self.apply_mtime()
			await self.delete_or_rollback_src_tmp()
		else:
			# Что-то пошло не так: откатываем времянку, удаляем целевой.
			self.main.counter_failed_files += 1
			if await self.rollback_src_tmp():
				LOG.info(f"Rolled back #{self.id}: {self.log_src_path}.")
			await self.delete_dst()


class Main:
	def __init__(self):
		self.argparser = None  # type: ArgumentParser|None
		self.args = None  # type: Namespace|None
		self.loop = None  # type: asyncio.AbstractEventLoop|None
		self.ask_stop = 0
		self.ask_stop_event = asyncio.Event()

		self.config_path = None  # type: Path|None
		self.config = None  # type: Config|None

		self.observer = None  # type: Observer|None
		self.handler = _EventHandler(self)

		self.recode_pool = SimpleTaskPool()
		self.recode_queue = asyncio.Queue()  # type: Queue[RecodeEntry]

		self.counter_failed_files = 0
		self.counter_recoded_files = 0
		self.reported_recoded_files = 0
		self.counter_recoded_bytes_src = 0
		self.counter_recoded_bytes_dst = 0

		self.vrc_screen_regex = re.compile(r'^VRChat_([x\d]+)_([-\d]+_[-\d]+\.\d+)(.*)$')

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
		for old_handler in list(LOG.handlers):
			LOG.removeHandler(old_handler)
		LOG.addHandler(std_handler)

		try:
			self.path_logs.mkdir(parents=True, exist_ok=True)
			file_handler = RotatingFileHandler(
				str(self.path_logs / 'vrc2webp.log'), maxBytes=10 * 1024 * 1024, backupCount=10, encoding='utf-8')
			file_handler.setLevel(level)
			file_handler.setFormatter(std_formatter)
			LOG.addHandler(file_handler)
		except Exception as exc:
			LOG.error(f"Failed to create log file: {type(exc)!r}: {exc}", exc_info=exc)

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
			LOG.error(f"Failed to change own priority: {type(exc)!r}: {exc}", exc_info=exc)

	def log_msg_recode_queue(self):
		return f"{self.recode_queue.qsize()} files pending recoding."

	def log_msg_recode_pool(self):
		return f"{len(self.recode_pool.pool)} files recoding right now."

	async def scan_watchpaths_single(self, path: 'Path', queue: 'deque[Path]'):
		if await asyncio.to_thread(os.path.islink, path):
			realpath = Path(await asyncio.to_thread(os.path.realpath, path))
			queue.append(realpath)
			return
		if await asyncio.to_thread(os.path.isdir, path):
			listdir = await asyncio.to_thread(os.listdir, path)
			queue.extend(path / entry for entry in listdir)
			return
		if await asyncio.to_thread(os.path.isfile, path):
			await self.handle_path(path)

	async def scan_watch_paths(self):
		queue = deque(self.config.watch_paths)
		counter = 0
		while len(queue) > 0:
			if self.ask_stop_event.is_set():
				LOG.info(f"Stopping FS scan, {len(queue)} items left.")
				return
			path = queue.popleft()
			counter += 1
			try:
				# Тут без параллелизма, т.к. нет смысла дрочить ФС и нет смысла получать список файлов быстро
				if is_debug():
					LOG.debug(f"Scanning {counter}: {log_path(path)}...")
				try:
					await asyncio.wait_for(self.scan_watchpaths_single(path, queue), 10)
				except TimeoutError:
					LOG.warning(f"Checking path {log_path(path)} is not done in 10 sec, is file system lagging?")
					queue.append(path)
			except OSError as exc:
				LOG.warning(f"Failed to scan: {log_path(path)}: {type(exc)!r}: {exc}")
		LOG.info(' '.join([
			f"Done scanning {counter} items in {len(self.config.watch_paths)} watch paths.",
			self.log_msg_recode_queue(), self.log_msg_recode_pool()]))

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
			LOG.error(f"Failed to test {exe_name} ({program_args!r}): {exc!s}", exc_info=exc)
			raise exc

	def test_cwebp_exe(self):
		return self.test_generic_exe('cwebp.exe', [str(self.path_cwebp), '-version'])

	def test_webpinfo_exe(self):
		return self.test_generic_exe('webpinfo.exe', [str(self.path_webpinfo), '-version'])

	async def observer_stopper(self):
		await self.ask_stop_event.wait()
		LOG.info(f"Stopping observer...")
		self.observer.stop()
		LOG.info(f"Observer stopped.")

	async def setup_observer(self):
		LOG.info(f"Preparing file system observer...")
		self.observer = Observer()
		for path in self.config.watch_paths:
			if path := await path_safe_resolve(path):
				LOG.info(f"Scheduling watch path: {log_path(path)}...")
				self.observer.schedule(self.handler, str(path), recursive=True)
		self.observer.start()
		asyncio.create_task(self.observer_stopper(), name='observer_stopper')
		LOG.info(f"Started file system observer in {len(self.config.watch_paths)} paths.")

	def is_acceptable_src_path(self, name: 'Path'):
		suffixes = name.suffixes
		if len(suffixes) < 1:
			return False
		if suffixes[-1].lower() not in self.config.file_extensions:
			return False
		if len(suffixes) > 1 and suffixes[-2].lower() == '.tmp':
			return False
		return True

	async def handle_path(self, path: 'str|Path'):
		path = await path_safe_resolve(path)
		if path is None:
			return
		if is_debug():
			LOG.debug(f"Handling {log_path(path)}...")
		if not self.is_acceptable_src_path(path):
			if is_debug():
				LOG.debug(f"File {log_path(path)} is not acceptable...")
			return
		if is_debug():
			LOG.debug(f"Enqueuing {log_path(path)}...")
		entry = RecodeEntry(self, path)
		self.recode_queue.put_nowait(entry)

	async def handle_event(self, event: 'FileSystemEvent'):
		if not isinstance(event, FileCreatedEvent):
			return
		await asyncio.sleep(5)
		if is_debug():
			LOG.debug(f"A new file detected: {str(event.src_path)!r}")
		await self.handle_path(event.src_path)

	def recode_pool_update_max_parallel(self):
		if self.config.max_parallel_recodes < 1:
			if is_debug():
				LOG.debug('Polling cpu_count...')
			self.config.max_parallel_recodes = max(psutil.cpu_count(), 1)
			if is_debug():
				LOG.debug(f'max_parallel_recodes = {self.config.max_parallel_recodes}')
		self.recode_pool.max_parallel = self.config.max_parallel_recodes

	async def recoding_loop_get_entry(self) -> 'RecodeEntry':
		self.recode_pool_update_max_parallel()
		await self.recode_pool.wait_not_full()
		return await self.recode_queue.get()

	async def recoding_loop_get_entry_timed(self) -> 'RecodeEntry|None':
		try:
			# TODO Python 3.11 async with asyncio.timeout(3):
			# Ждем до 5сек за пулом перекодеров и очередью
			return await asyncio.wait_for(self.recoding_loop_get_entry(), 3)
		except (asyncio.TimeoutError, asyncio.CancelledError):
			return None

	async def recoding_loop(self):
		counter = 0
		LOG.info(f"Started recoding loop. Awaiting for files to recode...")
		while not self.ask_stop_event.is_set():
			# if is_debug():
			# LOG.debug('Awaiting file path to process...')
			entry = await self.recoding_loop_get_entry_timed()
			if not entry:
				continue
			counter += 1
			if is_debug():
				LOG.debug(f"Pooling (i={counter}, q={self.recode_queue.qsize()}) recode of {log_path(entry.src_path)}...")
			self.recode_pool_update_max_parallel()
			entry.id = counter
			await self.recode_pool.submit(entry.do_recode(), name=f'recode-{counter}')

		while len(self.recode_pool.pool) > 0:
			LOG.info(f"Waiting {len(self.recode_pool.pool)} recoding tasks to complete before exit...")
			await asyncio.wait(self.recode_pool.pool, return_when=asyncio.FIRST_COMPLETED)
		LOG.info(f"All recoding processes completed.")

	async def reporting_loop(self):
		reported_recoded_files = -1
		cancelled = False
		while not cancelled:
			try:
				await asyncio.sleep(10)
			except asyncio.CancelledError:
				cancelled = True
			if cancelled or reported_recoded_files != self.counter_recoded_files:
				reported_recoded_files = self.counter_recoded_files
				src_fmt = sizeof_fmt(self.counter_recoded_bytes_src)
				dst_fmt = sizeof_fmt(self.counter_recoded_bytes_dst)
				diff = (self.counter_recoded_bytes_src - self.counter_recoded_bytes_dst)
				diff_fmt = sizeof_fmt(diff)
				percent = diff / self.counter_recoded_bytes_src if self.counter_recoded_bytes_src > 0 else 0.0
				LOG.info(' '.join([
					f"Recoded {self.counter_recoded_files} files from {src_fmt} to {dst_fmt}.",
					f"Reduced size by {diff_fmt} ({percent:.1%}).",
					self.log_msg_recode_queue(), self.log_msg_recode_pool()]))

	async def main_recode_async(self):
		self.setup_asyncio()
		await self.test_cwebp_exe()
		await self.test_webpinfo_exe()
		recoding_loop = asyncio.create_task(self.recoding_loop(), name='recoding_loop')
		await self.setup_observer()
		scan_watch_paths = asyncio.create_task(self.scan_watch_paths(), name='scan_watch_paths')
		reporting_loop = asyncio.create_task(self.reporting_loop(), name='reporting_loop')
		await asyncio.wait([recoding_loop, scan_watch_paths])
		reporting_loop.cancel()
		await asyncio.wait([reporting_loop])  # no exc
		LOG.info(f"All recoding tasks completed. App exit.")

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
			arg_config = self.args.config.resolve()  # type: Path
			if is_debug():
				LOG.debug(f"Copying {log_path(self.path_default_config)} -> {log_path(arg_config)}...")
			arg_config.parent.mkdir(parents=True, exist_ok=True)
			shutil.copyfile(self.path_default_config, arg_config)
			LOG.info(f"Exported default config to {log_path(arg_config)} ...")
			return 0
		except BaseException as exc:
			LOG.error(f"Failed to export default config: {type(exc)!r}: {exc}", exc_info=exc)
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
		self.argparser = ArgumentParser(prog='vrc2webp', formatter_class=WideHelpFormatter)

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
