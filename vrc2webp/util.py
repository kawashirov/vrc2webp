import argparse
import asyncio
import logging
import os
import typing
import sys

from pathlib import Path

if typing.TYPE_CHECKING:
	from collections.abc import Coroutine

APP_DEBUG = bool(hasattr(sys, 'gettrace') and sys.gettrace())

LOG = logging.getLogger('vrc2webp')


def set_debug(value):
	global APP_DEBUG
	APP_DEBUG = value


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
