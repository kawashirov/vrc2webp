import asyncio
import logging
import typing
from pathlib import Path

import watchdog.observers
import watchdog.events

from . import util
from .util import is_debug, log_path
from . import recode

if typing.TYPE_CHECKING:
	from .main import Main

LOG = logging.getLogger('vrc2webp')


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

		path_stat = await util.safe_stat(path)
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
			abs_part_stat = await util.safe_stat(abs_part)
			if not self.main.check_common(abs_part, abs_part_stat):
				return False
			if not self.main.check_directory(abs_part, abs_part_stat):
				return False

		# Если до сих пор никаких фильтров не отлетело, то можно смело планировать.
		if is_debug():
			LOG.debug(f"File {log_path(path)} is acceptable.")
		entry = recode.RecodeEntry(self.main, path)
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
			if path := await util.path_safe_resolve(path):
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
