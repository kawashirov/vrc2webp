.\venv\Scripts\activate.ps1
python -O -m nuitka --warn-unusual-code --show-progress --enable-console --onefile --include-data-dir=assets=assets main.py
