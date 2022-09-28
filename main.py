import functools
import os
import sys
import subprocess
import asyncio
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

if typing.TYPE_CHECKING:
	from asyncio import Future, Queue
	from typing import Set, Coroutine


def _dbg_gettrace():
	return hasattr(sys, 'gettrace') and sys.gettrace()


APP_DEBUG = _dbg_gettrace()


class SimpleTaskPool:
	def __init__(self):
		self.max_parallel = 4
		self.pool = set()  # type: Set[Future]
		self.event = asyncio.Event()

	def _done_callback(self, task):
		self.pool.discard(task)
		self.event.set()

	async def submit(self, coro: 'Coroutine', name=None):
		while len(self.pool) >= self.max_parallel:
			self.event.clear()
			await self.event.wait()
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


class Main:
	def __init__(self):
		self.log = None  # type: logging.Logger|None
		self.loop = None  # type: asyncio.BaseEventLoop|None

		self.observer = None  # type: watchdog.observers.Observer|None
		self.handler = _EventHandler(self)

		self.recode_pool = SimpleTaskPool()
		self.fsscan_pool = SimpleTaskPool()
		self.recode_queue = asyncio.Queue()  # type: Queue[Path]

		self.delete_mode = 1
		self.path_self = Path(__file__).resolve().parent
		self.path_assets = self.path_self / 'assets'
		self.path_logs = self.path_self / 'logs'
		self.path_cwebp = self.path_assets / 'cwebp.exe'
		self.path_webpinfo = self.path_assets / 'webpinfo.exe'
		self.recode_pool.max_parallel = max(1, psutil.cpu_count() - 1)
		self.watchpaths = [str(Path.home() / 'Pictures' / 'VRChat' / '2021-11 - Copy')]
		self.suffixes = ['.png', '.jpg', '.jpeg']
		self.recursive = True

	def setup_log(self):
		level = logging.DEBUG if APP_DEBUG else logging.INFO
		logging.basicConfig(level=level)

		std_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
		std_handler = logging.StreamHandler(stream=sys.stdout)
		std_handler.setLevel(level)
		std_handler.setFormatter(std_formatter)

		for old_handler in list(logging.root.handlers):
			logging.root.removeHandler(old_handler)

		self.log = logging.getLogger('vrc2webp')
		self.log.setLevel(level=level)
		for old_handler in list(self.log.handlers):
			self.log.removeHandler(old_handler)
		self.log.addHandler(std_handler)

		try:
			self.path_logs.mkdir(parents=True, exist_ok=True)
			file_handler = RotatingFileHandler(
				str(self.path_logs / 'vrc2webp.log'), maxBytes=10 * 1024 * 1024, backupCount=10, encoding='utf-8')
			file_handler.setLevel(level)
			file_handler.setFormatter(std_formatter)
			self.log.addHandler(file_handler)
		except Exception as exc:
			self.log.error(f"Failed to create log file: {exc}", exc_info=exc)

		self.log.info(f"Logging initialized. APP_DEBUG={APP_DEBUG}")

	def setup_priority(self):
		try:
			self.log.debug("Changing own priority...")
			p = psutil.Process()
			p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
			p.ionice(psutil.IOPRIO_LOW)
			self.log.debug("Changed own priority.")
		except Exception as exc:
			self.log.error(f"Failed to change own priority: {exc}", exc_info=exc)

	async def scan_watchpaths_single(self, path, queue):
		if await asyncio.to_thread(os.path.islink, path):
			realpath = await asyncio.to_thread(os.path.realpath, path)
			queue.append(realpath)
			return
		if await asyncio.to_thread(os.path.isdir, path):
			listdir = await asyncio.to_thread(os.listdir, path)
			queue.extend(os.path.join(path, entry) for entry in listdir)
			return
		if await asyncio.to_thread(os.path.isfile, path):
			await self.handle_path(path)

	async def scan_watchpaths(self):
		queue = deque(self.watchpaths)
		counter = 0
		while len(queue) > 0:
			path = queue.popleft()
			counter += 1
			try:
				# Тут без параллелизма, т.к. нет смысла дрочить ФС и нет смысла получать список файлов быстро
				self.log.debug(f"Scanning ({counter}) {path!r}...")
				await self.scan_watchpaths_single(path, queue)
			except OSError as exc:
				self.log.warning(f"Failed to scan {path!r}: {exc}")
		self.log.info(f"Scanned {counter} items in watch paths.")

	@staticmethod
	def is_jpeg_source(path: 'str'):
		return path.endswith('.jpg') or path.endswith('.jpeg')

	@staticmethod
	async def process_communicate(process):
		stdout_data, _ = await process.communicate()
		if not stdout_data:
			stdout_data = b''
		return stdout_data.decode('ascii').replace('\n', ' ').strip()

	async def test_generic_exe(self, exe_name, program_args):
		self.log.debug(f"Testing {exe_name}...")
		cwebp_path = str(self.path_cwebp)
		try:
			cwebp_process = await create_subprocess_exec(
				*program_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
				creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
			stdout_text = await self.process_communicate(cwebp_process)
			if cwebp_process.returncode != 0:
				raise Exception(f"{exe_name} test failed ({cwebp_process.returncode}): {stdout_text}")

			self.log.info(f"{exe_name} test OK: " + stdout_text)
		except Exception as exc:
			self.log.error(f"Failed to test {exe_name} ({cwebp_path!r}): {exc!s}", exc_info=exc)
			raise exc

	async def setup_observer(self):
		self.log.info(f"Preparing file system observer...")
		self.observer = watchdog.observers.Observer()
		for path in self.watchpaths:
			self.log.info(f"Scheduling watch path: {path!r}")
			self.observer.schedule(self.handler, path, recursive=True)
		self.observer.start()
		self.log.info(f"Started file system observer in {len(self.watchpaths)} paths.")

	def is_acceptable_pic_file_name(self, name: 'str'):
		low = name.lower()
		for suffix in self.suffixes:
			if low.endswith(suffix) and not low.endswith('.tmp' + suffix):
				return True
		return False

	async def handle_path(self, path: 'str|Path'):
		path = Path(path).resolve()
		# self.log.debug(f"Handling {path!r}...")
		if not self.is_acceptable_pic_file_name(path.name):
			return
		# self.log.debug(f"Enqueuing {path!r}...")
		self.recode_queue.put_nowait(path)

	async def handle_event(self, event: 'watchdog.events.FileSystemEvent'):
		if not isinstance(event, watchdog.events.FileCreatedEvent):
			return
		await asyncio.sleep(5)
		self.log.debug(f"A new file detected: {str(event.src_path)!r}")
		await self.handle_path(event.src_path)

	async def do_recode_precheck(self, src_path: 'Path') -> 'os.stat_result|None':
		try:
			return await asyncio.to_thread(os.stat, src_path)
		except OSError as exc:
			self.log.warning(f"Failed to precheck {str(src_path)!r}: {exc!s}")
			return None

	async def do_recode_prepare(self, src_path: 'Path', tmp_path: 'Path', stat_result):
		try:
			if stat_result.st_size < 1:
				self.log.info(f"File {src_path.name!r} is empty. (Yet?)")
				return False
			await asyncio.to_thread(os.rename, src_path, tmp_path)
			return True
		except OSError as exc:
			self.log.info(f"Failed to prepare {src_path.name!r} -> {tmp_path.name!r}: {exc!s}")
			return False

	async def do_recode_cwebp(self, src_path: 'Path', tmp_path: 'Path', dst_path: 'Path'):
		self.log.info(f"Recoding {tmp_path.name!r} -> {dst_path.name!r}...")
		args = ['-quiet', '-preset', 'picture', '-hint', 'picture',
						'-q', '100', '-m', '6', '-metadata', 'all']
		if self.is_jpeg_source(src_path.name):
			args.extend(('-noalpha', '-f', '0', '-sharp_yuv'))
		else:
			args.extend(('-exact', '-alpha_q', '100', '-alpha_filter', 'best', '-lossless', '-z', '9'))
		args.extend(('-o', dst_path, '--', tmp_path))
		proc = await create_subprocess_exec(
			self.path_cwebp, *args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess.IDLE_PRIORITY_CLASS)
		proc_text = await self.process_communicate(proc)
		if proc.returncode == 0:
			self.log.info(f"Recoded {tmp_path.name!r} -> {dst_path.name!r}: {proc_text}")
			return True
		else:
			self.log.error(f"Failed to recode {tmp_path.name!r} -> {dst_path.name!r} ({proc.returncode}): {proc_text}")
			return False

	async def do_recode_webpinfo(self, dst_path: 'Path'):
		self.log.debug(f"Checking {dst_path.name!r}...")
		proc = await create_subprocess_exec(
			self.path_webpinfo, '-quiet', dst_path,
			stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
		proc_text = await self.process_communicate(proc)
		if proc.returncode == 0:
			self.log.debug(f"Checked {dst_path.name!r}: {proc_text}")
			return True
		else:
			self.log.error(f"WEBP error {dst_path.name!r} ({proc.returncode}): {proc_text}")
			return False

	async def do_recode_rollback(self, tmp_path: 'Path', src_path: 'Path'):
		try:
			self.log.debug(f"Rolling back {tmp_path.name!r} -> {src_path.name!r}...")
			await asyncio.to_thread(os.rename, tmp_path, src_path)
		except OSError as exc:
			self.log.error(f"Failed to rollback {tmp_path.name!r}: {exc}", exc_info=exc)

	async def do_recode_delete(self, tmp_path: 'Path', src_path: 'Path'):
		try:
			if self.delete_mode == 2:
				self.log.debug(f"Deleting {tmp_path.name!r} permanently...")
				await asyncio.to_thread(os.unlink, tmp_path)
			elif self.delete_mode == 1:
				self.log.debug(f"Deleting {tmp_path.name!r} to trash...")
				await asyncio.to_thread(send2trash.send2trash, tmp_path)
			elif self.delete_mode == 0:
				await self.do_recode_rollback(tmp_path, src_path)
		except OSError as exc:
			self.log.error(f"Failed to finalize (delete) {tmp_path.name!r}: {exc}", exc_info=exc)

	async def do_recode(self, src_path: 'Path'):
		tmp_path = src_path.with_stem(src_path.stem + '.tmp')
		dst_path = src_path.with_suffix('.webp')
		self.log.debug(f"Recoding {src_path!r} -> {dst_path!r}...")
		if not (stat_result := await self.do_recode_precheck(src_path)):
			return
		if not await self.do_recode_prepare(src_path, tmp_path, stat_result):
			self.log.info(f"Re-queueing {str(src_path)!r}...")
			self.recode_queue.put_nowait(src_path)
			return
		ok_cwebp = await self.do_recode_cwebp(src_path, tmp_path, dst_path)
		ok_webpinfo = await self.do_recode_webpinfo(dst_path)
		if ok_cwebp and ok_webpinfo:
			await self.do_recode_delete(tmp_path, src_path)
		else:
			await self.do_recode_rollback(tmp_path, src_path)
		self.log.debug(f"Recoded {src_path!r} -> {dst_path!r}.")

	async def recoding_loop(self):
		counter = 0
		while True:
			src_path = await self.recode_queue.get()
			counter += 1
			self.log.info(f"Recoding (i={counter}, q={self.recode_queue.qsize()}) {str(src_path)!r}...")
			await self.recode_pool.submit(self.do_recode(src_path), name=f'recode-{counter}')

	async def async_main(self):
		await self.test_generic_exe('cwebp.exe', [str(self.path_cwebp), '-version'])
		await self.test_generic_exe('webpinfo.exe', [str(self.path_webpinfo), '-version'])
		self.loop.create_task(self.recoding_loop(), name='recoding_loop')
		await self.setup_observer()
		self.loop.create_task(self.scan_watchpaths(), name='scan_watchpaths')

	def main(self):
		self.setup_log()
		self.setup_priority()
		self.loop = asyncio.get_event_loop()
		self.loop.create_task(self.async_main(), name='async_main')
		self.loop.run_forever()


if __name__ == '__main__':
	Main().main()
