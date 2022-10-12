import asyncio
import collections
import logging
import os
import random
import typing
import stat

from pathlib import Path

from .util import is_debug, log_path, log_exc
from . import recode

if typing.TYPE_CHECKING:
	from .main import Main

LOG = logging.getLogger('vrc2webp')


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
			entry = recode.RecodeEntry(self.main, path)
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
