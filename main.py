import functools
import os
import shutil
import signal
import sys
import argparse
import asyncio
import subprocess
import re
import logging
import typing

from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from asyncio.subprocess import create_subprocess_exec

import send2trash

import watchdog
import watchdog.observers
import watchdog.events

import psutil
import yaml

if typing.TYPE_CHECKING:
	from asyncio import Future, Queue
	from typing import List, Set, Coroutine

APP_DEBUG = bool(hasattr(sys, 'gettrace') and sys.gettrace())
LOG = logging.getLogger('vrc2webp')


def is_debug():
	return APP_DEBUG


def log_path(path: 'Path|None'):
	return repr(path.as_posix()) if isinstance(path, Path) else repr(path)


class WideHelpFormatter(argparse.HelpFormatter):
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


class _EventHandler(watchdog.events.FileSystemEventHandler):
	def __init__(self, _main: 'Main'):
		self.main = _main

	def on_created(self, event: 'watchdog.events.FileSystemEvent') -> None:
		# DirCreatedEvent или FileCreatedEvent
		asyncio.run_coroutine_threadsafe(self.main.handle_event(event), self.main.loop)


def is_jpeg_source(path: 'str'):
	return path.endswith('.jpg') or path.endswith('.jpeg')


def check_file(pathlike: 'os.PathLike|str'):
	try:
		os.stat(pathlike)
	except FileNotFoundError:
		return False
	except OSError as exc:
		LOG.warning(f"Failed to check file {str(pathlike)!r}: {exc}")
	return True


async def process_communicate(process):
	stdout_data, _ = await process.communicate()
	if not stdout_data:
		stdout_data = b''
	return stdout_data.decode('ascii').replace('\n', ' ').strip()


class ConfigError(RuntimeError):
	pass


class Config:
	delete_mode: 'int'
	watch_paths: 'List[Path]'
	file_extensions: 'List[str]'
	vrc_swap_resolution_and_time: 'bool'
	max_parallel_recodes: 'int'

	@staticmethod
	def _type_check(name, obj, types):
		if not isinstance(obj, types):
			raise ConfigError(f"Invalid type of {name!r}: {type(obj)!r}: {obj!r}")

	@staticmethod
	def _get_bool(key: 'str', raw_config: 'dict'):
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
	def _get_int(key: 'str', raw_config: 'dict'):
		value = raw_config.get(key)  # type: bool
		if isinstance(value, int):
			return value
		elif isinstance(value, str):
			return int(value.strip())
		raise ConfigError(f"Invalid bool int of {key}: {type(value)!r}: {value!r}")

	def _apply_delmode(self, raw_config: 'dict'):
		delmode = str(raw_config.get('delete-mode')).lower()  # type: str|int
		if delmode == 'keep' or delmode == '0':
			self.delete_mode = 0
		elif delmode == 'trash' or delmode == '1':
			self.delete_mode = 1
		elif delmode == 'unlink' or delmode == '2':
			self.delete_mode = 2

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

	@classmethod
	def load_yaml_file(cls, path: 'Path') -> 'Config':
		config = cls()
		try:
			raw_config = None  # type: dict|None
			with open(path, 'rt') as stream:
				raw_config = yaml.safe_load(stream)
				cls._type_check('root', raw_config, dict)
			config._apply_delmode(raw_config)
			config._apply_watch_paths(raw_config)
			config._apply_file_extensions(raw_config)
			config.vrc_swap_resolution_and_time = cls._get_bool('vrc-swap-resolution-and-time', raw_config)
			config.max_parallel_recodes = cls._get_int('max-parallel-recodes', raw_config)
			return config
		except (OSError, RuntimeError) as exc:
			msg = f"Failed to load config ({str(path)!r}): {exc}"
			LOG.error(msg)
			raise RuntimeError(msg)


class Main:
	def __init__(self):
		self.argparser = None  # type: argparse.ArgumentParser|None
		self.args = None  # type: argparse.Namespace|None
		self.loop = None  # type: asyncio.AbstractEventLoop|None
		self.ask_stop = 0
		self.ask_stop_event = asyncio.Event()

		self.config_path = None  # type: Path|None
		self.config = None  # type: Config|None

		self.observer = None  # type: watchdog.observers.Observer|None
		self.handler = _EventHandler(self)

		self.recode_pool = SimpleTaskPool()
		self.recode_queue = asyncio.Queue()  # type: Queue[Path]

		self.vrc_screen_regex = re.compile(r'^VRChat_([x\d]+)_([-\d]+_[-\d]+\.\d+)(.*)$')

		self.path_self = Path(__file__).resolve(strict=True).parent
		self.path_assets = self.path_self / 'assets'
		self.path_default_config = self.path_assets / 'default.yaml'
		self.path_cwebp = self.path_assets / 'cwebp.exe'
		self.path_webpinfo = self.path_assets / 'webpinfo.exe'
		self.path_logs = self.path_self / 'logs'

	def setup_debug(self):
		global APP_DEBUG

		if self.args.debug:
			APP_DEBUG = True

		if is_debug():
			self.loop.set_debug(True)

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
			LOG.error(f"Failed to create log file: {exc}", exc_info=exc)

		LOG.info(f"Logging initialized. APP_DEBUG={APP_DEBUG}")

	def handle_signal(self, sigid: 'int', frame, signame=None):
		self.ask_stop += 1
		self.ask_stop_event.set()
		if self.ask_stop >= 5:
			LOG.info(f"Asked fo stop ({signame}/{sigid}) five times, forcing app crash...")
			sys.exit(1)
		else:
			more = 5 - self.ask_stop
			LOG.info(f"Asked fo stop ({signame}/{sigid}) {self.ask_stop} times, ask {more} more times to force app crash...")

	def setup_async(self):
		self.loop = asyncio.get_event_loop()
		self.loop.set_debug(is_debug())
		for signame in ('SIGABRT', 'SIGINT', 'SIGTERM', 'SIGBREAK'):
			signal.signal(getattr(signal, signame), functools.partial(self.handle_signal, signame=signame))

	@staticmethod
	def try_load_config_type_check(name, obj, types):
		if not isinstance(obj, types):
			raise ConfigError(f"Wrong type of {name!r}: {type(obj)!r}: {obj!r}")

	def read_config(self):
		self.config_path = self.args.config.resolve(strict=True) if self.args.config else self.path_default_config
		self.config = Config.load_yaml_file(self.config_path)

	def setup_priority(self):
		try:
			if is_debug():
				LOG.debug("Changing own priority...")
			p = psutil.Process()
			p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
			p.ionice(psutil.IOPRIO_LOW)
			if is_debug():
				LOG.debug("Changed own priorities.")
		except Exception as exc:
			LOG.error(f"Failed to change own priority: {exc}", exc_info=exc)

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
				LOG.warning(f"Failed to scan: {log_path(path)}: {exc}")
		LOG.info(f"Done scanning {counter} items in watch paths.")

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

			LOG.info(f"{exe_name} test OK: " + stdout_text)
		except Exception as exc:
			LOG.error(f"Failed to test {exe_name} ({program_args!r}): {exc!s}", exc_info=exc)
			raise exc

	def test_cwebp_exe(self):
		return self.test_generic_exe('cwebp.exe', [str(self.path_cwebp), '-version'])

	def test_webpinfo_exe(self):
		return self.test_generic_exe('webpinfo.exe', [str(self.path_webpinfo), '-version'])

	def setup_observer(self):
		LOG.info(f"Preparing file system observer...")
		self.observer = watchdog.observers.Observer()
		for path in self.config.watch_paths:
			path = path.resolve(strict=True)
			LOG.info(f"Scheduling watch path: {log_path(path)}...")
			self.observer.schedule(self.handler, str(path), recursive=True)
		self.observer.start()

		async def observer_stopper():
			await self.ask_stop_event.wait()
			LOG.info(f"Stopping observer...")
			self.observer.stop()
			LOG.info(f"Observer stopped.")

		self.loop.create_task(observer_stopper(), name='observer_stopper')
		LOG.info(f"Started file system observer in {len(self.config.watch_paths)} paths.")

	def is_acceptable_pic_file_name(self, name: 'str'):
		low = name.lower()
		for ext in self.config.file_extensions:
			if low.endswith(ext) and not low.endswith('.tmp' + ext):
				return True
		return False

	async def handle_path(self, path: 'str|Path'):
		path = Path(path).resolve(strict=True)
		if is_debug():
			LOG.debug(f"Handling {log_path(path)}...")
		if not self.is_acceptable_pic_file_name(path.name):
			if is_debug():
				LOG.debug(f"File {log_path(path)} is not acceptable...")
			return
		if is_debug():
			LOG.debug(f"Enqueuing {log_path(path)}...")
		self.recode_queue.put_nowait(path)

	async def handle_event(self, event: 'watchdog.events.FileSystemEvent'):
		if not isinstance(event, watchdog.events.FileCreatedEvent):
			return
		await asyncio.sleep(5)
		if is_debug():
			LOG.debug(f"A new file detected: {str(event.src_path)!r}")
		await self.handle_path(event.src_path)

	async def do_recode_precheck(self, src_path: 'Path') -> 'os.stat_result|None':
		try:
			if is_debug():
				LOG.debug(f"Testing {log_path(src_path)}...")
			return await asyncio.to_thread(os.stat, src_path)
		except OSError as exc:
			LOG.warning(f"Failed to pre-check {log_path(src_path)}: {exc!s}")
			return None

	async def do_recode_prepare(self, src_path: 'Path', tmp_path: 'Path', stat_result: 'os.stat_result'):
		try:
			if stat_result.st_size < 1:
				LOG.info(f"File {log_path(src_path)} is empty. (Yet?)")
				return False
			if is_debug():
				LOG.debug(f"Moving {log_path(src_path)} -> {log_path(tmp_path)}...")
			await asyncio.to_thread(os.rename, src_path, tmp_path)
			if is_debug():
				LOG.debug(f"Moved {log_path(src_path)} -> {log_path(tmp_path)}.")
			return True
		except OSError as exc:
			LOG.info(f"Failed to prepare {log_path(src_path)} -> {log_path(tmp_path)}: {exc!s}")
			return False

	def do_recode_dst_name(self, src_path: 'Path'):
		new_name = src_path.with_suffix('.webp')
		if self.config.vrc_swap_resolution_and_time and (match := self.vrc_screen_regex.match(src_path.name)):
			new_name = new_name.with_name(f'VRChat_{match.group(2)}_{match.group(1)}{match.group(3)}')
		return new_name

	async def do_recode_cwebp(self, src_path: 'Path', tmp_path: 'Path', dst_path: 'Path'):
		if is_debug():
			LOG.debug(f"Recoding {log_path(tmp_path)} -> {log_path(dst_path)}...")
		args = ['-quiet', '-preset', 'picture', '-hint', 'picture',
						'-q', '100', '-m', '6', '-metadata', 'all']
		if is_jpeg_source(src_path.name):
			args.extend(('-noalpha', '-f', '0', '-sharp_yuv'))
		else:
			args.extend(('-exact', '-alpha_q', '100', '-alpha_filter', 'best', '-lossless', '-z', '9'))
		args.extend(('-o', dst_path, '--', tmp_path))
		proc = await create_subprocess_exec(
			self.path_cwebp, *args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess.IDLE_PRIORITY_CLASS | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
		proc_text = await process_communicate(proc)
		if proc.returncode == 0:
			if is_debug():
				LOG.debug(f"Recoded {log_path(tmp_path)} -> {log_path(dst_path)}: {proc_text}")
			return True
		else:
			log_cmd = [self.path_cwebp, *args]
			LOG.error(f"Failed to recode {log_cmd!r} ({proc.returncode}): {proc_text}")
			return False

	async def do_recode_webpinfo(self, dst_path: 'Path'):
		if is_debug():
			LOG.debug(f"Checking {log_path(dst_path)}...")
		proc = await create_subprocess_exec(
			self.path_webpinfo, '-quiet', dst_path,
			stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
		proc_text = await process_communicate(proc)
		if proc.returncode == 0:
			if is_debug():
				LOG.debug(f"Checked {log_path(dst_path)}: {proc_text}")
			return True
		else:
			LOG.error(f"WEBP error {log_path(dst_path)} ({proc.returncode}): {proc_text}")
			return False

	async def do_recode_rollback(self, tmp_path: 'Path', src_path: 'Path'):
		try:
			if is_debug():
				LOG.debug(f"Rolling back {log_path(tmp_path)} -> {log_path(src_path)}...")
			await asyncio.to_thread(os.rename, tmp_path, src_path)
			return True
		except OSError as exc:
			LOG.error(f"Failed to rollback {log_path(tmp_path)}: {exc}", exc_info=exc)
		return False

	async def do_recode_delete(self, tmp_path: 'Path', src_path: 'Path'):
		try:
			if self.config.delete_mode == 2:
				if is_debug():
					LOG.debug(f"Deleting {log_path(tmp_path)} permanently...")
				await asyncio.to_thread(os.unlink, tmp_path)
				return True
			elif self.config.delete_mode == 1:
				if is_debug():
					LOG.debug(f"Deleting {log_path(tmp_path)} to trash...")
				await asyncio.to_thread(send2trash.send2trash, tmp_path)
				return True
			elif self.config.delete_mode == 0:
				return await self.do_recode_rollback(tmp_path, src_path)
		except OSError as exc:
			LOG.error(f"Failed to finalize (delete) {log_path(tmp_path)}: {exc}", exc_info=exc)
		return False

	async def do_recode(self, src_path: 'Path', counter: 'int'):
		tmp_path = src_path.with_stem(src_path.stem + '.tmp')
		if is_debug():
			LOG.debug(f"Checking to recode {log_path(src_path)} ({counter})...")
		if not (stat_result := await self.do_recode_precheck(src_path)):
			return
		if not await self.do_recode_prepare(src_path, tmp_path, stat_result):
			LOG.info(f"Re-queueing {log_path(src_path)} ({counter})...")
			self.recode_queue.put_nowait(src_path)
			return
		dst_path = self.do_recode_dst_name(src_path)
		LOG.info(f"Trying to recode {log_path(src_path.parent)} ({counter}): {src_path.name!r} -> {dst_path.name!r}...")
		ok_cwebp = await self.do_recode_cwebp(src_path, tmp_path, dst_path)
		ok_webpinfo = await self.do_recode_webpinfo(dst_path)
		if ok_cwebp and ok_webpinfo:
			LOG.info(f"Recoded {log_path(src_path.parent)} ({counter}): {src_path.name!r} -> {dst_path.name!r}.")
			await self.do_recode_delete(tmp_path, src_path)
		elif await self.do_recode_rollback(tmp_path, src_path):
			LOG.info(f"Rolled back {log_path(src_path)} ({counter}).")

	def recode_pool_update_max_parallel(self):
		if self.config.max_parallel_recodes < 1:
			if is_debug():
				LOG.debug('Polling cpu_count...')
			self.config.max_parallel_recodes = max(psutil.cpu_count(), 1)
			if is_debug():
				LOG.debug(f'max_parallel_recodes = {self.config.max_parallel_recodes}')
		self.recode_pool.max_parallel = self.config.max_parallel_recodes

	async def recoding_loop_get_path(self):
		self.recode_pool_update_max_parallel()
		await self.recode_pool.wait_not_full()
		return await self.recode_queue.get()

	async def recoding_loop_get_path_timed(self):
		try:
			# TODO Python 3.11 async with asyncio.timeout(3):
			# Ждем до 5сек за пулом перекодеров и очередью
			return await asyncio.wait_for(self.recoding_loop_get_path(), 3)
		except (asyncio.TimeoutError, asyncio.CancelledError):
			return None

	async def recoding_loop(self):
		counter = 0
		while not self.ask_stop_event.is_set():
			if is_debug():
				LOG.debug('Awaiting file path to process...')
			src_path = await self.recoding_loop_get_path_timed()
			if not src_path:
				continue
			counter += 1
			if is_debug():
				LOG.debug(f"Pooling (i={counter}, q={self.recode_queue.qsize()}) recode of : {log_path(src_path)}...")
			self.recode_pool_update_max_parallel()
			await self.recode_pool.submit(self.do_recode(src_path, counter), name=f'recode-{counter}')

		while len(self.recode_pool.pool) > 0:
			LOG.info(f"Waiting {len(self.recode_pool.pool)} recoding tasks to complete before exit...")
			await asyncio.wait(self.recode_pool.pool, return_when=asyncio.FIRST_COMPLETED)
		LOG.info(f"All recoding processes completed.")

	async def main_recode_async(self):
		await self.test_cwebp_exe()
		await self.test_webpinfo_exe()
		recoding_loop = self.loop.create_task(self.recoding_loop(), name='recoding_loop')
		self.setup_observer()
		scan_watch_paths = self.loop.create_task(self.scan_watch_paths(), name='scan_watch_paths')
		await asyncio.wait([recoding_loop, scan_watch_paths])
		LOG.info(f"All recoding tasks completed. App exit.")

	def main_recode(self):
		self.setup_debug()
		self.setup_log()
		self.read_config()
		self.setup_priority()
		self.setup_async()
		self.loop.run_until_complete(self.main_recode_async())

	def main_export(self) -> 'int':
		try:
			self.setup_debug()
			self.setup_log()
			if not self.args.config:
				raise ValueError('Config path not provided!')
			LOG.info(f"Exporting default config to {log_path(self.args.config)} ...")
			arg_config = self.args.config.resolve(strict=True)
			if is_debug():
				LOG.debug(f"Copying {log_path(self.path_default_config)} -> {log_path(arg_config)}...")
			shutil.copyfile(self.path_default_config, arg_config)
			return 0
		except BaseException as exc:
			LOG.error(f"Failed to export default config: {exc}", exc_info=exc)
		return 1

	def main_test(self) -> 'int':
		self.setup_debug()
		self.setup_log()
		self.setup_async()
		LOG.info("Testing embedded exe binaries...")
		test_ok = True
		tests = [
			('cwebp', self.test_cwebp_exe()),
			('webpinfo', self.test_webpinfo_exe())]

		for binary, coro in tests:
			result = self.loop.run_until_complete(asyncio.gather(coro, return_exceptions=True))
			if isinstance(result, BaseException):
				test_ok = False
				LOG.error(f"Test {binary!r} failed: {result}", exc_info=result)
		LOG.info(f"Tested {len(tests)} embedded exe binaries: {test_ok}")
		return 0 if test_ok else 1

	def main(self):
		self.argparser = argparse.ArgumentParser(prog='vrc2webp', formatter_class=WideHelpFormatter)

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
