import asyncio
import functools
import heapq
import logging
import os
import time
import typing
import subprocess
from pathlib import Path
from asyncio.subprocess import create_subprocess_exec

import psutil
import send2trash

from . import util
from .util import is_debug, log_path, log_exc

if typing.TYPE_CHECKING:
	from .main import Main

LOG = logging.getLogger('vrc2webp')


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

		self.dst_stat = await util.safe_stat(self.dst_path, not_found_ok=True)
		if self.dst_stat:
			LOG.error(f"Destination file {self.log_dst_path} already exists ({util.sizeof_fmt(self.dst_stat.st_size)})!")
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
		self.webpinfo_text = await util.process_communicate(proc)
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
		self.dst_stat = await util.safe_stat(self.dst_path, not_found_ok=False)
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
		self.src_stat = await util.safe_stat(self.src_path)
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
