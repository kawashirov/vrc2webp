# nuitka-project: --onefile
# nuitka-project: --windows-company-name=kawashirov
# nuitka-project: --windows-product-name=vrc2webp
# nuitka-project: --windows-file-version=0.1.1.0
# nuitka-project: --windows-product-version=0.1.1.0
# nuitka-project: --include-package=vrc2webp
# nuitka-project: --include-package-data=vrc2webp
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

if __name__ == '__main__':
	from . import main

	main.Main().main()
