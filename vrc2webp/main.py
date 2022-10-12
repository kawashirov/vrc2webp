import argparse
import asyncio
import logging
import os
import re
import typing
import signal
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from asyncio.subprocess import create_subprocess_exec

import psutil

from .version import APP_VERION
from . import util
from .util import is_debug, log_path, log_exc
from . import config
from . import scanner
from . import observer
from . import recode

if typing.TYPE_CHECKING:
	from .recode import RecodeEntry

LOG = logging.getLogger('vrc2webp')


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
		self.config: 'config.Config|None' = None

		self.observer: 'observer.PathsObserver|None' = None
		self.scanner: 'scanner.PathsScaner|None' = None

		self.recode_pool = util.SimpleTaskPool()
		self.recode_queue = recode.RecodePriorityQueue()

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

		# Когда используем nuitka, то логи будут во временной папачке в распаковке
		# Когда запускаем с интерпретатора, то будут в рабочей директории
		self.path_logs = (self.path_self if getattr(util, '__compiled__', False) else Path()).resolve() / 'logs'

	def setup_debug(self):
		if self.args.debug:
			util.set_debug(True)

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
			if self.path_logs:
				self.path_logs.mkdir(parents=True, exist_ok=True)
				file_handler = RotatingFileHandler(
					str(self.path_logs / 'vrc2webp.log'), maxBytes=10 * 1024 * 1024, backupCount=10, encoding='utf-8')
				file_handler.setLevel(level)
				file_handler.setFormatter(std_formatter)
				for logger in loggers:
					logger.addHandler(file_handler)
		except Exception as exc:
			LOG.error(f"Failed to create log file: {log_exc(exc)}", exc_info=exc)

		LOG.info(f"Logging initialized. APP_DEBUG={is_debug()}")

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
		self.config = config.Config()
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
			stdout_text = await util.process_communicate(cwebp_process)
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
			self.observer = observer.PathsObserver(self)
			await self.observer.observe()
		except Exception as exc:
			LOG.critical(f"Observe loop crashed: {log_exc(exc)}", exc_info=exc)
			self.ask_stop_event.set()

	async def job_scan(self):
		try:
			if self.config.watch_mode not in ('scan', 'both'):
				return
			LOG.info(f"Scanning items in {len(self.config.watch_paths)} watch paths.")
			self.scanner = scanner.PathsScaner(self)
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
		await util.wait_any(*wait_for)
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
				sizeof_fmt = util.sizeof_fmt
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
		self.argparser = argparse.ArgumentParser(prog='vrc2webp', formatter_class=util.WideHelpFormatter)

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
