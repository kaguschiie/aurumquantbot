@echo off
chcp 65001 >nul
cd /d "%~dp0"
title KriptoQuantBot - exe olusturuluyor
rem Kullanim: exe_olustur.bat          -> derle, masaustu kisayolu, uygulamayi ac
rem           exe_olustur.bat yayin    -> sadece derle (guncelleme_yayinla.bat cagirir)
if not exist surum.txt echo dev> surum.txt
set DIST=dist
if /i "%~1"=="yayin" set DIST=dist_yayin
rem yayin derlemesi ayri klasore: calisan uygulamanin (dist) dosyalari kilitli olsa da derlenir
echo [1/3] Paketler kontrol ediliyor...
python -m pip install --disable-pip-version-check -q --no-warn-script-location -r requirements.txt pyinstaller
if errorlevel 1 goto hata
echo [2/3] Uygulama paketleniyor (birkac dakika surer)...
python -c "import app; app.make_icon('icon.ico')"
python -m PyInstaller --noconfirm --clean --windowed --name KriptoQuantBot --icon icon.ico ^
  --add-data "ui.html;." --add-data "lw.js;." --add-data "surum.txt;." --add-data "portfoy_model.joblib;." --add-data "sunucu_adresi.txt;." --hidden-import pystray._win32 ^
  --collect-submodules webview --collect-submodules anthropic --collect-submodules sklearn ^
  --hidden-import bot --hidden-import agent --hidden-import chat --hidden-import teknik --hidden-import quant --hidden-import telegram_bot ^
  --hidden-import dersler --hidden-import makro --hidden-import portfoy --hidden-import kontrolor --hidden-import zincir --hidden-import guncelle --hidden-import sunucu ^
  --hidden-import gecmis --hidden-import komite --hidden-import hafiza --hidden-import gunluk --hidden-import montecarlo ^
  --distpath %DIST% --workpath build_%DIST% app.py
if errorlevel 1 goto hata
if /i "%~1"=="yayin" exit /b 0
echo [3/3] Masaustu kisayolu olusturuluyor...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\KriptoQuantBot.lnk');$s.TargetPath='%~dp0dist\KriptoQuantBot\KriptoQuantBot.exe';$s.WorkingDirectory='%~dp0dist\KriptoQuantBot';$s.Save()"
echo.
echo Hazir: masaustundeki KriptoQuantBot kisayolu veya dist\KriptoQuantBot\KriptoQuantBot.exe
start "" "%~dp0dist\KriptoQuantBot\KriptoQuantBot.exe"
exit /b 0
:hata
echo Bir hata olustu. Yukaridaki mesaja bakin.
if /i not "%~1"=="yayin" pause
exit /b 1
