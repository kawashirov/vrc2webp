import subprocess
import sys
import signal

if __name__ == '__main__':
	from vrc2webp.version import APP_VERION

	url = 'https://github.com/kawashirov/vrc2webp/'
	nofollow = [
		# Т.к. мы только на шындовс, можно выкинуть ненужное.
		'psutil._pslinux', 'psutil._psosx', 'psutil._psbsd', 'psutil._pssunos', 'psutil._psaix',
		'send2trash.plat_osx', 'send2trash.plat_gio',
		'watchdog.observers.inotify', 'watchdog.observers.fsevents', 'watchdog.observers.kqueue'
	]

	nuitka_cmd = [
		sys.executable,
		'-m', 'nuitka',
		'--warn-implicit-exceptions',
		'--warn-unusual-code',
		'--show-progress',
		'--show-modules',
		'--windows-company-name=kawashirov',
		'--windows-product-name=vrc2webp',
		f'--windows-file-description={url}',
		f'--windows-file-version={APP_VERION}',
		f'--windows-product-version={APP_VERION}',
		'--include-package=vrc2webp',
		'--include-package-data=vrc2webp',
		'--python-flag=-OO',
		'--onefile-tempdir-spec=%TEMP%\\vrc2webp_%PID%_%TIME%',
		'--windows-icon-from-ico=logo\\logo.ico',
		*(f'--nofollow-import-to={m}' for m in nofollow),
		'--onefile', 'vrc2webp',
		'-o', 'vrc2webp.exe'
	]

	with subprocess.Popen(nuitka_cmd, stdin=subprocess.DEVNULL) as proc:
		while proc.poll() is None:
			try:
				proc.wait()
			except KeyboardInterrupt:
				proc.send_signal(signal.SIGBREAK)
